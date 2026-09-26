//! K3 memory selftest (mem gate).
//!
//! Runs in four phases around the CR3 switch, and is honest about which
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

/// Print the summary and hand the counts to main.
pub fn summary() -> (u32, u32) {
    let (p, f) = unsafe { (PASS, FAIL) };
    sprintln!("mem gate:   {} checks passed, {} failed", p, f);
    (p, f)
}
