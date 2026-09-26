//! K3 memory selftest (mem gate).
//!
//! Runs in five phases around the CR3 switch, and is honest about which
//! oracle each phase uses:
//!
//!   phase 1  frame allocator, Limine tables still live - PMM state checked
//!            against the Limine memory map (an independent source: the map
//!            comes from the bootloader, not from pmm.rs).
//!   phase 2  page tables BEFORE activation - only the software walk
//!            (`paging::translate`) can be consulted; the CPU cannot see
//!            the new mappings yet. Nothing here claims hardware behaviour.
//!   phase 3  kernel heap - boundary-tag invariants re-derived from raw
//!            window bytes (`heap::walk_ok`) plus a deterministic churn
//!            workload with content verification.
//!   phase 4  AFTER `paging::activate` - checks the CPU can only pass if
//!            the switch really preserved every inherited region: direct
//!            map round-trip, kernel-managed mappings writable, 2 MiB leaf
//!            translated and writable, framebuffer still backed per the
//!            memory map, an exception gate still dispatching, and the
//!            frame count returning exactly to its starting value.
//!   phase 5  stack guard pages (K3 done-when). `install_boot_guard` runs
//!            on every boot: the page below the boot stack's lower window
//!            is mapped not-present, so an overflow faults instead of
//!            silently corrupting memory. Under `novatest`, phase 5 then
//!            overflows the real stack deliberately and asserts the CPU
//!            delivered #PF ON the guard page: vector 14, CR2 = guard page
//!            offset 0xFF8, error code 0x2 (not-present write), the last
//!            legal store exactly at the last legal address, and the
//!            physical page below the guard byte-identical to a pre-probe
//!            copy (nothing written below).
//!
//! Counts are printed as "mem gate: N checks passed, M failed"; any failure
//! makes main exit 35 (distinct from the success exit 33), so a broken
//! memory layer can never masquerade as a passing boot.

use crate::{heap, idt, limine, paging, pmm};
use core::arch::asm;

/// Set by main from the boot-time RSP before pmm::init: the LOWER and UPPER
/// 2 MiB windows of the boot stack (Limine was asked for 4 MiB, so the stack
/// spans two windows; rsp at kmain sits in the upper one).
pub static mut BOOT_STACK_LO: u64 = 0;
pub static mut BOOT_STACK_HI: u64 = 0;

/// Set by `install_boot_guard` (after the CR3 switch): the not-present
/// guard page below the boot stack, and the lowest page the floor probe
/// reached (reporting only - see `stack_floor`).
pub static mut GUARD_PAGE: u64 = 0;
pub static mut STACK_FLOOR: u64 = 0;

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;
/// Phase-2 probe frames, freed by phase 4 after the CPU-side checks.
static mut PHASE2_P1: u64 = 0;
static mut PHASE2_P2: u64 = 0;
/// Free-frame count at phase-1 entry. The honest baseline: paging::init
/// permanently consumes the PML4 + private-PDPT frames, so the allocator's
/// resting count is this value, NOT frames_usable().
static mut BASELINE_FREE: u64 = 0;
/// Test mappings live ABOVE the heap window (which owns [base, base+32MiB)
/// exclusively - map_chunk asserts nothing else is mapped there).
const TEST_OFF: u64 = 32 << 20;

fn check(ok: bool, name: &str, detail: &str) {
    unsafe {
        if ok {
            PASS += 1;
            sprintln!("  ok    {}", name);
        } else {
            FAIL += 1;
            sprintln!("  FAIL  {} - {}", name, detail);
        }
    }
}

/// Read RSP (main uses it to locate the boot stack window).
pub fn read_rsp() -> u64 {
    let v: u64;
    unsafe {
        asm!("mov {}, rsp", out(reg) v, options(nomem, nostack, preserves_flags));
    }
    v
}

/// Phase 1: frame allocator against the Limine memory map.
pub fn phase1() {
    sprintln!("mem gate:   phase 1 - frame allocator (Limine tables still live)");

    let usable = pmm::frames_usable();
    unsafe { BASELINE_FREE = pmm::frames_free() };
    check(
        usable > 0,
        "pmm chained a nonzero frame count from the memory map",
        "frames_usable() == 0",
    );

    let f1 = pmm::alloc_frame();
    check(
        matches!(f1, Some(f) if f != 0 && f & 0xFFF == 0),
        "alloc_frame yields a nonzero page-aligned frame",
        "alloc_frame() returned None or a misaligned frame",
    );
    if let Some(f) = f1 {
        pmm::free_frame(f);
    }

    // The boot-stack windows must not be handout-eligible. Oracle: the
    // Limine memory map itself - the stack lives in a bootloader-reclaimable
    // region and pmm chains only USABLE ones, so membership would mean a bug.
    let (lo, hi) = unsafe { (BOOT_STACK_LO, BOOT_STACK_HI) };
    let mut in_usable = false;
    limine::for_each_memmap_entry(|e| {
        if e.ty == limine::MEMMAP_USABLE {
            for w in [lo, hi] {
                if w != 0 && w >= e.base && w < e.base + e.length {
                    in_usable = true;
                }
            }
        }
    });
    check(
        (lo == 0 && hi == 0) || !in_usable,
        "boot-stack windows lie in no USABLE region (not handout-eligible)",
        "a boot-stack 2 MiB window intersects a USABLE memmap entry",
    );

    // Bounded behavioural probe: pop 64 frames, none may be a boot-stack
    // window base or the frames inside those windows, restore in reverse so
    // the LIFO list is byte-identical.
    let mut got: [u64; 64] = [0; 64];
    let mut n = 0u64;
    let mut saw_stack = false;
    for g in got.iter_mut() {
        match pmm::alloc_frame() {
            Some(f) => {
                if (f >= lo && f < lo + (2 << 20)) || (f >= hi && f < hi + (2 << 20)) {
                    saw_stack = true;
                }
                *g = f;
                n += 1;
            }
            None => break,
        }
    }
    for i in (0..n).rev() {
        pmm::free_frame(got[i as usize]);
    }
    check(
        !saw_stack,
        "64-frame probe never returns a frame inside the boot-stack windows",
        "alloc_frame handed out a frame the boot stack occupies",
    );
    check(
        pmm::frames_free() == unsafe { BASELINE_FREE },
        "free-frame count restored exactly after the probe round-trip",
        "count drift across the alloc/free round-trip",
    );

    // Distinctness: 8 allocations must be 8 different frames.
    let mut fs = [0u64; 8];
    let mut distinct = true;
    for slot in fs.iter_mut() {
        match pmm::alloc_frame() {
            Some(f) => *slot = f,
            None => distinct = false,
        }
    }
    for i in 0..8 {
        for j in (i + 1)..8 {
            if fs[i] != 0 && fs[i] == fs[j] {
                distinct = false;
            }
        }
    }
    for i in (0..8).rev() {
        if fs[i] != 0 {
            pmm::free_frame(fs[i]);
        }
    }
    check(
        distinct,
        "8 allocations yield 8 distinct frames",
        "duplicate frame handed out",
    );

    // A 2 MiB run must come back aligned and, once freed, freeable again.
    let run = pmm::alloc_run_2m();
    let run_ok = matches!(run, Some(r) if r != 0 && r % (2 << 20) == 0);
    check(
        run_ok,
        "alloc_run_2m yields a 2 MiB-aligned contiguous run",
        "run None or misaligned",
    );
    if let Some(r) = run {
        pmm::free_run_2m(r);
        let again = pmm::alloc_run_2m();
        check(
            again == run,
            "freed 2 MiB run is immediately findable again (coalesced back)",
            "second alloc_run_2m did not return the same run",
        );
        if let Some(r2) = again {
            pmm::free_run_2m(r2);
        }
    }
    check(
        pmm::frames_free() == unsafe { BASELINE_FREE },
        "frame count exactly restored after all phase-1 traffic",
        "count drift at end of phase 1",
    );
}

/// Phase 2: page tables before activation (software-walk oracle only).
pub fn phase2() {
    sprintln!("mem gate:   phase 2 - page tables (software walk, pre-switch)");

    // Two kernel-managed mappings, kept allocated until phase 4 uses them
    // as CPU-visible round-trip targets.
    let p1 = pmm::alloc_frame().expect("phase2: frame 1");
    let p2 = pmm::alloc_frame().expect("phase2: frame 2");
    let base = paging::private_base();
    let v1 = base + TEST_OFF;
    let v2 = v1 + 0x1000;
    paging::map_page(v1, p1);
    paging::map_page(v2, p2);

    check(
        paging::translate(v1) == Some(p1) && paging::translate(v1 + 0x123) == Some(p1 + 0x123),
        "map_page walk: va translates to the chosen frame, offset preserved",
        "translate(v1) or translate(v1+0x123) disagreed with the mapping",
    );

    check(
        paging::translate(v2) == Some(p2),
        "second 4 KiB mapping is independent of the first",
        "translate(v2) != p2",
    );

    check(
        paging::translate(base + (30 << 20)).is_none(),
        "uncommitted window address walks to not-present",
        "translate() returned a mapping for uncommitted space",
    );

    check(
        paging::translate(0x0000_8000_0000_0000).is_none(),
        "non-canonical address is rejected by the walk",
        "translate() accepted a non-canonical va",
    );

    // The probe frames stay allocated until phase 4 has used the mappings
    // for CPU-side checks; phase 4 frees them and does its own net-zero
    // accounting from a snapshot taken after that.
    unsafe {
        PHASE2_P1 = p1;
        PHASE2_P2 = p2;
    }
}

/// Phase 3: kernel heap (runs through the direct map, pre-activation).
pub fn phase3() {
    sprintln!("mem gate:   phase 3 - kernel heap (boundary-tag allocator)");

    let a = heap::alloc(1024);
    check(
        matches!(a, Some(p) if (p as u64) & 0xF == 0),
        "alloc returns a 16-byte-aligned payload",
        "payload pointer misaligned or None",
    );

    let zeroed = match a {
        Some(p) => {
            let mut ok = true;
            unsafe {
                for i in 0..1024usize {
                    if *p.add(i) != 0 {
                        ok = false;
                        break;
                    }
                }
            }
            ok
        }
        None => false,
    };
    check(
        zeroed,
        "alloc zero-fills the payload",
        "nonzero bytes in a fresh block",
    );
    if let Some(p) = a {
        heap::free(p);
    }

    let (ops, blocks) = heap::stress(1000);
    check(
        ops > 1000 && blocks > 0,
        "deterministic churn workload passed with a clean invariant walk",
        "stress reported inconsistent allocator state",
    );
    sprintln!(
        "  info  stress: {} ops, {} blocks in final chain",
        ops,
        blocks
    );

    // 3 MiB cannot fit in one committed 2 MiB chunk: forces lazy commit,
    // left coalescing, and a split.
    let big_size: u64 = 3 << 20;
    let big = heap::alloc(big_size);
    let big_ok = match big {
        Some(p) => unsafe {
            *p = 0xAA;
            *p.add((big_size - 1) as usize) = 0x55;
            *p == 0xAA && *p.add((big_size - 1) as usize) == 0x55
        },
        None => false,
    };
    check(
        big_ok,
        "3 MiB allocation spans committed chunks (grow + coalesce)",
        "large alloc failed or content did not stick",
    );
    if let Some(p) = big {
        heap::free(p);
    }
    let after = heap::walk_ok();
    check(
        after > 0,
        "invariant walk clean after the large alloc/free",
        "walk_ok failed",
    );
    sprintln!(
        "  info  final heap walk: {} blocks, all invariants hold",
        after
    );
}

/// Phase 4: after `paging::activate` - the CPU is the oracle now.
pub fn phase4() {
    sprintln!("mem gate:   phase 4 - after the CR3 switch (CPU-visible)");

    let pml4 = paging::kernel_pml4();
    check(
        paging::current_cr3() == pml4,
        "CR3 holds the kernel PML4",
        "current CR3 != paging::kernel_pml4()",
    );

    // Direct map still live: CPU round-trip on a fresh frame via HHDM.
    let f = pmm::alloc_frame().expect("phase4: frame");
    let hv = unsafe { hv_from(f) };
    unsafe {
        pmm::write_va(hv, 0x5A5A_A5A5_1234_5678);
        check(
            pmm::read_va(hv) == 0x5A5A_A5A5_1234_5678,
            "direct-map round-trip on a fresh frame survives the switch",
            "CPU read-back through HHDM disagreed",
        );
    }

    // Phase-2 mappings are now CPU-visible: write through the managed va,
    // read back through the direct map - two different paths to one frame.
    let p1 = unsafe { PHASE2_P1 };
    let p2 = unsafe { PHASE2_P2 };
    let base = paging::private_base();
    let v1 = base + TEST_OFF;
    let v2 = v1 + 0x1000;
    unsafe {
        (v1 as *mut u64).write_volatile(0x0B00_B10C_D00D_0001);
        (v2 as *mut u64).write_volatile(0x0000_0042_4F4F_5453u64);
        let r1 = pmm::read_va(hv_from(p1));
        let r2 = pmm::read_va(hv_from(p2));
        check(
            r1 == 0x0B00_B10C_D00D_0001 && r2 == 0x0000_0042_4F4F_5453,
            "kernel-managed 4 KiB mappings are CPU-writable and translate",
            "managed va writes did not reach the frames through the CPU",
        );
        check(
            paging::translate(v1) == Some(p1) && paging::translate(v2) == Some(p2),
            "software walk still agrees with the CPU on both managed vas",
            "walk/CU disagreement after the switch",
        );
    }
    // Both probe frames and the direct-map test frame go home; the snapshot
    // for the net-zero check is taken after all pre-existing traffic.
    pmm::free_frame(f);
    pmm::free_frame(p1);
    pmm::free_frame(p2);
    let s4 = pmm::frames_free();

    // 2 MiB leaf: map, touch the far end through the CPU, translate, then
    // unmap (with TLB invalidation) and return the run to the PMM. The test
    // address is above the heap window so the allocator can never be using it.
    let run = pmm::alloc_run_2m().expect("phase4: 2 MiB run");
    let big_va = base + (64 << 20);
    paging::map_huge_2m(big_va, run);
    let far = big_va + 0x1F_FF00;
    let huge_data = unsafe {
        (far as *mut u64).write_volatile(0x0000_0048_454C_4C4Fu64);
        pmm::read_va(hv_from(run + 0x1F_FF00)) == 0x0000_0048_454C_4C4F
    };
    let got_pa = paging::translate(far);
    check(
        huge_data,
        "2 MiB leaf: far-end CPU write visible through the direct map",
        "write/read-back via HHDM disagreed (data path broken)",
    );
    check(
        got_pa == Some(run + 0x1F_FF00),
        "2 MiB leaf: software walk resolves the far end to the run offset",
        "translate(far) disagreed with the mapping",
    );
    paging::unmap_2m(big_va);
    pmm::free_run_2m(run);

    // The framebuffer Limine mapped must still be backed - and its backing
    // must be the framebuffer's own physical region per the memory map
    // (independent oracle), not just "some" translation.
    let fb_ok = match limine::first_framebuffer() {
        Some(info) => match paging::translate(info.address) {
            Some(pa) => {
                sprintln!("  info  fb va {:#x} -> pa {:#x}", info.address, pa);
                let mut in_fb_region = false;
                limine::for_each_memmap_entry(|e| {
                    if e.ty == limine::MEMMAP_FRAMEBUFFER && pa >= e.base && pa < e.base + e.length
                    {
                        in_fb_region = true;
                    }
                });
                in_fb_region
            }
            None => {
                sprintln!(
                    "  info  fb va {:#x} -> translate returned None",
                    info.address
                );
                false
            }
        },
        None => false,
    };
    check(
        fb_ok,
        "framebuffer mapping survives the switch and translates into the FB region",
        "framebuffer va not translating into a FRAMEBUFFER memmap region",
    );

    // Uncommitted heap-window space still reads as not-present after the
    // switch (phase 3 has committed only the chunks the workload needed).
    check(
        paging::translate(base + (30 << 20)).is_none(),
        "uncommitted window address still not-present post-switch",
        "phantom mapping after activation",
    );

    // The exception machinery must still dispatch through its real gate:
    // int 0x17 (a reserved vector with no native trigger) must be recorded.
    // Resume mode is required - in halt mode the handler reports and stops,
    // which is exactly what it should do outside a test.
    idt::set_resume_mode(true);
    idt::int_dispatch(0x17);
    idt::set_resume_mode(false);
    check(
        idt::int_seen(0x17),
        "exception gate still dispatches on the new CR3 (int 0x17 recorded)",
        "int_dispatch(0x17) was not recorded by the handler",
    );

    check(
        pmm::frames_free() == s4,
        "huge-page map/unmap round-trip nets to zero frames",
        "frame count did not return to the post-cleanup snapshot",
    );
}

unsafe fn hv_from(phys: u64) -> u64 {
    limine::hhdm_offset().expect("hhdm") + phys
}

// ---------------------------------------------------------------------------
// K3b: boot-stack guard page + the deliberate-overflow probe.
// ---------------------------------------------------------------------------

/// Lowest page of the boot stack's mapped region that the probe reaches.
/// Walks DOWN from `start` while each page translates contiguously below
/// the one above (same 4 KiB shift); stops early at the first not-present
/// page or a discontiguity. Bounded at 512 pages (2 MiB): if contiguity
/// holds that far, the true floor is lower still, and the probe reports the
/// lowest page it reached - a conservative floor. The point of the walk is
/// the log line, not a check: it proves the region below the guard is real,
/// contiguously mapped memory, i.e. an unguarded overflow would have
/// corrupted it silently.
fn stack_floor(start: u64) -> u64 {
    let mut va = start;
    let mut expected: Option<u64> = None;
    for _ in 0..512 {
        match paging::translate(va) {
            None => return va,
            Some(pa) => {
                if let Some(exp) = expected {
                    if pa + 0x1000 != exp {
                        return va;
                    }
                }
                expected = Some(pa);
            }
        }
        va -= 0x1000;
    }
    va
}

/// Map the boot stack's floor page not-present, on every boot. Runs after
/// `paging::activate` and `heap::init` (the guard edit needs the CPU off
/// Limine's original CR3 view before we start re-pointing tables).
///
/// The guard goes directly below the lower 2 MiB window of the requested
/// 4 MiB stack - a well-defined boundary: any descent below the requested
/// stack region now faults. The owning tables may be inherited from Limine
/// (the stack was mapped by the bootloader); `paging::set_page_not_present`
/// copies then edits them, so Limine's tables are never written.
/// Installation failure is a boot failure, not a selftest check: this
/// kernel does not run with an unguarded stack.
pub fn install_boot_guard() {
    let (lo, hi) = unsafe { (BOOT_STACK_LO, BOOT_STACK_HI) };
    assert!(
        lo != 0 && hi != 0,
        "boot guard: stack windows were not captured at boot"
    );
    let floor = stack_floor(lo);
    let guard = lo - 0x1000;
    if !paging::set_page_not_present(guard) {
        panic!(
            "boot guard: guard page {:#x} is already not-present (stack window capture wrong?)",
            guard
        );
    }
    unsafe {
        GUARD_PAGE = guard;
        STACK_FLOOR = floor;
    }
    let mapped_below = ((guard - floor) >> 12) + 1;
    sprintln!(
        "guard:      boot stack: floor probe reached va {:#x} ({} contiguously mapped pages below the rsp window); guard page {:#x} is now not-present",
        floor, mapped_below, guard
    );
}

/// Phase 5: stack guard pages - the K3 done-when gate ("a deliberate stack
/// overflow faults on the guard page rather than corrupting memory"). The
/// guard was installed at boot; here the real stack is overflowed on
/// purpose, one page per step, until the store faults. Every claim is then
/// pinned: WHICH vector, WHICH address, WHAT error code, WHERE the last
/// legal store landed, and that the physical page below the guard is
/// byte-identical to its pre-probe copy.
pub fn phase5() {
    sprintln!("mem gate:   phase 5 - stack guard pages (deliberate overflow)");

    let win = unsafe { BOOT_STACK_LO }; // lowest page of the stack's lower window
    let guard = unsafe { GUARD_PAGE };
    let floor = unsafe { STACK_FLOOR };
    let _ = floor; // reporting only (install line); phase 5 pins cr2 instead

    // (a) The guard page is not present - software-walk oracle.
    check(
        paging::translate(guard).is_none(),
        "guard page walks to not-present (software oracle)",
        "translate(guard) returned a mapping",
    );

    // (b) The guard is exactly one page: the page above it - the stack
    // window base - still translates. Its pa also anchors the corruption
    // oracle below (the guard page physically sits one frame below it:
    // pre-split the window was one contiguous leaf, and the split preserved
    // the window verbatim).
    let above = paging::translate(guard + 0x1000);
    check(
        above.is_some(),
        "page above the guard still translates (guard is exactly one page)",
        "translate(guard + 0x1000) returned None",
    );
    let pa_win = above.expect("phase5: stack window base must translate after guard install");
    let guard_pa = pa_win - 0x1000;

    // Corruption oracle: a full copy of the physical page below the guard,
    // taken before the probe and compared byte-for-byte after. Equality
    // proves the CPU wrote nothing there during the overflow. The page's
    // prior content is whatever Limine left in bootloader-reclaimable
    // memory - the check deliberately does NOT assume zeros.
    //
    // The copy goes through a kernel-managed ALIAS in the private PML4 slot,
    // NOT through the HHDM: for a Limine-stack guard the HHDM alias of
    // guard_pa is numerically the same VA as the guard itself (the stack
    // lives inside the direct map), which is not-present by design - the
    // first version of this check faulted on exactly that (read #PF, code
    // 0x0, halt mode). The alias maps the same physical frame at an unused
    // kernel-managed va; it costs one PT frame and stays for the session,
    // so the frame-count baseline below is taken after it exists.
    let alias_va = paging::private_base() + (96 << 20);
    paging::map_page(alias_va, guard_pa);
    paging::tlb_flush_all();
    let mut before = [0u64; 512];
    unsafe {
        for (i, slot) in before.iter_mut().enumerate() {
            *slot = ((alias_va + (i as u64) * 8) as *const u64).read_volatile();
        }
    }

    // Pre-fill the window base page (the descent's first victim) so the
    // evidence is legible: 0xC3 fill below 0xFF0; the probe's first store
    // lands the 0xC1 marker at [0xFF8], the page's last legal qword.
    unsafe {
        for off in (0..0xFF0u64).step_by(8) {
            ((win + off) as *mut u64).write_volatile(0xC3C3_C3C3_C3C3_C3C3);
        }
    }

    let s5 = pmm::frames_free(); // after the alias mapping; the probe itself must net zero

    // The overflow probe. Each step: real rsp parked in r12 (callee-saved -
    // the #PF handler preserves it; caller-saved registers are clobbered
    // across the resume, which asm! already declares for every register it
    // does not bind), rsp set to the trial address, then the trigger store
    // `mov dword ptr [rsp], eax` - encoding 89 04 24, THREE bytes, matching
    // ADVANCE_TABLE[14] so the resume lands after it. The next page down is
    // tried until a store faults; the handler (resume mode) records the
    // event and iretqs back here with rsp = trial.
    //
    // The faulting delivery runs the REAL gate path: IDT gate 14 -> IST3
    // (gdt.rs/idt.rs K3b wiring) - the first hardware delivery through an
    // IST in this kernel, exercised exactly because the interrupted stack
    // is the one being broken.
    idt::set_resume_mode(true);
    let mut faulted = false;
    let mut fault_code = 0u64;
    let mut fault_cr2 = 0u64;
    let mut rsp = win + 0xFF8;
    for _ in 0..4096 {
        let trial = rsp;
        unsafe {
            asm!(
                "mov r12, rsp",
                "mov rsp, r15",
                "mov eax, 0xC1C1C1C1",
                "mov dword ptr [rsp], eax",
                "mov rsp, r12",
                in("r15") trial,
                out("rax") _,
                inout("r12") rsp => _,
            );
        }
        if let Some((code, cr2)) = idt::native_details(14) {
            faulted = true;
            fault_code = code;
            fault_cr2 = cr2;
            idt::native_seen(14);
            break;
        }
        rsp -= 0x1000;
    }
    idt::set_resume_mode(false);
    sprintln!(
        "  info  overflow fault: vec=14 cr2={:#x} code={:#x} (guard page {:#x})",
        fault_cr2,
        fault_code,
        guard
    );

    check(
        faulted,
        "deliberate stack overflow delivered #PF (vector 14)",
        "descent hit its 4096-page bound without a recorded #PF",
    );

    // ON the guard page: cr2 is the attempted store address. The descent
    // stores at page offset 0xFF8, so a guard hit is exactly guard + 0xFF8
    // - one faulting instruction that would have written the guard page's
    // last qword had the page been present.
    check(
        faulted && fault_cr2 == guard + 0xFF8,
        "#PF landed ON the guard page (cr2 == guard + 0xFF8)",
        "cr2 was not the guard page's last qword address",
    );

    // Error code (SDM Vol. 3 Ch. 6, page-fault error code bits):
    // bit 0 (P) = 0 -> caused by a not-present page (the guard),
    // bit 1 (W/R) = 1 -> caused by a write. Ring-0 access to a plain
    // not-present page is exactly code 0x2.
    check(
        faulted && fault_code == 0x2,
        "#PF error code 0x2: not-present page, caused by a write",
        "error code did not match the not-present-write shape",
    );

    // The last SUCCESSFUL store landed on the last legal address: the
    // window page's fill is intact below 0xFF0 and [0xFF8] holds the
    // pushed marker (read back through the CPU - the page is still mapped).
    // The marker is a DWORD store (the 3-byte trigger pins its width), so
    // only the low half of the [0xFF8] qword is expected to hold 0xC1C1C1C1.
    let tail_ok = unsafe {
        let mut ok = ((win + 0xFF8) as *const u32).read_volatile() == 0xC1C1_C1C1;
        for off in (0..0xFF0u64).step_by(8) {
            if ((win + off) as *const u64).read_volatile() != 0xC3C3_C3C3_C3C3_C3C3 {
                ok = false;
                break;
            }
        }
        ok
    };
    check(
        tail_ok,
        "last successful store landed exactly at the stack's last legal address",
        "window page fill/marker disagreed with the expected descent",
    );

    // Nothing below the guard changed: the physical page under the faulting
    // address is byte-identical to the pre-probe copy (read via the direct
    // map - an independent path from the faulting va).
    let mut no_corruption = true;
    unsafe {
        for (i, want) in before.iter().enumerate() {
            if ((alias_va + (i as u64) * 8) as *const u64).read_volatile() != *want {
                no_corruption = false;
                break;
            }
        }
    }
    check(
        no_corruption,
        "physical page below the guard is byte-identical after the overflow (no corruption)",
        "a byte changed in the page below the guard",
    );

    // And the guard machinery itself is frame-neutral.
    check(
        pmm::frames_free() == s5,
        "guard-page probe nets to zero frames",
        "frame count drifted across phase 5",
    );
}

/// Print the summary and hand the counts to main.
pub fn summary() -> (u32, u32) {
    let (p, f) = unsafe { (PASS, FAIL) };
    sprintln!("mem gate:   {} checks passed, {} failed", p, f);
    (p, f)
}
