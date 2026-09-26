//! 4-level paging for Nucleus (K3a).
//!
//! Format and traversal follow the SDM Vol. 3 Ch. 4 "4-Level Paging" model:
//! entries at each of the four levels (PML4 -> PDPT -> PD -> PT); a PTE's
//! bits 51:12 hold the next-level table's physical address; bit 0 = PRESENT,
//! bit 1 = R/W, bit 2 = U/S; a P- and PS-set entry at the PDPT level maps a
//! 1 GiB leaf and at the PD level a 2 MiB leaf (SDM Vol. 3 §4.5, which
//! describes the "If PS is 1" leaf cases for PDPTE and PDE).
//!
//! `init` builds the kernel's own PML4 while Limine's tables are still live:
//! every *present* Limine PML4 entry is copied by reference (same child
//! table), so all Limine-mapped regions survive our CR3 switch — the
//! higher-half kernel mapping, the direct map, and the framebuffer (which
//! QEMU places at 0xfd00000000, PML4 index 0, outside any other region).
//! The kernel then owns exactly one previously-unused PML4 slot (the highest
//! free one, found from the snapshot — never a slot the kernel or Limine
//! already occupies); mappings we create live under that slot's own fresh
//! PDPT, so no Limine table is ever written.
//!
//! The old CR3 physical address is read from CR3 itself at init time, not
//! from any Limine structure (those describe nothing about the page tables
//! Limine ran on). The CR3 switch happens in `activate`, after the exception
//! selftest, so a paging bug cannot take the selftest down with it.
//!
//! K3b adds guard pages: a stack's floor page is mapped not-present so an
//! overflow faults (#PF) instead of silently corrupting memory below. The
//! boot stack was mapped by Limine, so its translation path can run through
//! Limine's inherited tables - which this kernel never writes. The guard
//! machinery therefore unshares every level it must modify: the owning
//! PDPT/PD/PT is copied verbatim into a fresh frame and the kernel's own
//! upper level is re-pointed at the copy (copy-then-edit). Any 1 GiB or
//! 2 MiB leaf on the path is split into the next finer level first, which
//! preserves every other page. Limine's own tables are never written.

use crate::pmm;
use core::arch::asm;
use core::sync::atomic::{AtomicBool, Ordering};

pub const PAGE_SIZE: u64 = 4096;

const PTE_PRESENT: u64 = 1 << 0;
const PTE_WRITABLE: u64 = 1 << 1;
const PTE_HUGE: u64 = 1 << 7; // PS bit at PDPT/PD level
/// Bits 51:12 of a paging entry: the physical address field
/// (SDM Vol. 3 §4.5, "physical address of the 4-KByte ... page table").
const ADDR_MASK: u64 = 0x000F_FFFF_FFFF_F000;

static mut PML4: u64 = 0;
static mut PRIVATE_SLOT: u64 = 0; // PML4 index of the kernel-managed area
static mut ACTIVE: bool = false;
static LOCK: AtomicBool = AtomicBool::new(false);
/// PML4 slots whose PDPT has been copied down into a kernel-owned table by
/// the guard-page machinery (bit i = slot i). Once a slot is privatized its
/// PDPT entries may be edited directly; until then they belong to Limine.
static mut PRIVATIZED_SLOTS: [u64; 8] = [0; 8];

fn lock() {
    while LOCK.swap(true, Ordering::Acquire) {
        core::hint::spin_loop();
    }
}

fn unlock() {
    LOCK.store(false, Ordering::Release);
}

/// Current CR3 (physical), read straight from the register.
pub fn current_cr3() -> u64 {
    let v: u64;
    unsafe {
        asm!("mov {}, cr3", out(reg) v, options(nomem, nostack, preserves_flags));
    }
    v
}

/// Read the PML4 currently loaded in CR3 through the direct map and return a
/// bitmap of its present entries: 8 words, bit (i/64, i%64) = PML4 entry i
/// present (a u64 cannot hold all 512 bits).
fn snapshot_pml4_present() -> [u64; 8] {
    let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM feature required");
    let pml4_phys = current_cr3() & ADDR_MASK;
    let mut bitmap: [u64; 8] = [0; 8];
    for i in 0..512u64 {
        let e = unsafe { pmm::read_va(hhdm + pml4_phys + i * 8) };
        if e & PTE_PRESENT != 0 {
            bitmap[(i / 64) as usize] |= 1 << (i % 64);
        }
    }
    bitmap
}

/// Build the kernel PML4. Must run while Limine's tables are still live.
pub fn init() {
    assert!(pmm::ready(), "paging: pmm must init first");
    lock();
    let inherited = snapshot_pml4_present();
    assert!(
        inherited.iter().any(|w| *w != 0),
        "paging: Limine PML4 has no present entries - read the wrong CR3?"
    );

    let pml4 = pmm::alloc_frame().expect("paging: PML4 frame");
    pmm::zero_frame(pml4);

    // Inherit: point each Limine-present slot at Limine's own child table.
    // Limine's tables are never modified, only referenced.
    let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM required");
    let lim_pml4 = current_cr3() & ADDR_MASK;
    for i in 0..512u64 {
        if inherited[(i / 64) as usize] & (1 << (i % 64)) != 0 {
            unsafe {
                let child = pmm::read_va(hhdm + lim_pml4 + i * 8) & ADDR_MASK;
                pmm::write_va(hhdm + pml4 + i * 8, child | PTE_PRESENT | PTE_WRITABLE);
            }
        }
    }

    // The kernel-managed area: the highest PML4 slot Limine does NOT use.
    // Scan down from 510 (511 is the canonical-sign top slot; keep clear of
    // anything inherited). One fresh zeroed PDPT lives there; mappings we
    // create later hang under it, so Limine's trees are never written.
    let mut private: Option<u64> = None;
    for i in (0..=510u64).rev() {
        if inherited[(i / 64) as usize] & (1 << (i % 64)) == 0 {
            private = Some(i);
            break;
        }
    }
    let slot = private.expect("paging: no free PML4 slot for the kernel-managed area");
    let pdpt = pmm::alloc_frame().expect("paging: private PDPT frame");
    pmm::zero_frame(pdpt);
    unsafe {
        pmm::write_va(hhdm + pml4 + slot * 8, pdpt | PTE_PRESENT | PTE_WRITABLE);
    }

    unsafe {
        PML4 = pml4;
        PRIVATE_SLOT = slot;
    }
    unlock();
    let inherited_count: u32 = inherited.iter().map(|w| w.count_ones()).sum();
    sprintln!(
        "paging:      own PML4 at phys {:#x}; {} Limine PML4 entries inherited; \
         kernel-managed area in PML4 slot {}",
        pml4,
        inherited_count,
        slot
    );
}

/// The physical address of the kernel PML4 (what `activate` puts in CR3).
pub fn kernel_pml4() -> u64 {
    unsafe { PML4 }
}

/// Canonical base virtual address of the kernel-managed area (its PML4 slot
/// << 39, sign-extended per SDM Vol. 3 §4.5 canonical-address form).
pub fn private_base() -> u64 {
    let slot = unsafe { PRIVATE_SLOT };
    let raw = slot << 39;
    if slot >= 256 {
        raw | 0xFFFF_0000_0000_0000
    } else {
        raw
    }
}

/// Map one 4 KiB page `vpage` -> `ppage` (both page-aligned), allocating
/// intermediate tables on demand from the PMM. The walk only ever writes
/// tables that hang under the kernel-managed PML4 slot.
pub fn map_page(vpage: u64, ppage: u64) {
    assert!(pmm::ready(), "paging: pmm must init first");
    assert!(
        vpage.is_multiple_of(PAGE_SIZE) && ppage.is_multiple_of(PAGE_SIZE),
        "paging: map_page misaligned"
    );
    lock();
    unsafe {
        map_inner(vpage, ppage, PTE_PRESENT | PTE_WRITABLE, false);
    }
    unlock();
}

/// Map one 2 MiB page `vpage` -> `ppage` (both 2 MiB aligned) via a PD-level
/// PS=1 leaf (SDM Vol. 3 §4.5 PDE "If PS is 1" case).
pub fn map_huge_2m(vpage: u64, ppage: u64) {
    const TWO_M: u64 = 2 << 20;
    assert!(pmm::ready(), "paging: pmm must init first");
    assert!(
        vpage.is_multiple_of(TWO_M) && ppage.is_multiple_of(TWO_M),
        "paging: map_huge_2m misaligned"
    );
    lock();
    unsafe {
        map_inner(vpage, ppage, PTE_PRESENT | PTE_WRITABLE | PTE_HUGE, true);
    }
    unlock();
}

unsafe fn map_inner(vpage: u64, ppage: u64, flags: u64, huge: bool) {
    let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM required");
    let pml4 = PML4;
    let i_pml4 = (vpage >> 39) & 0x1FF;
    let i_pdpt = (vpage >> 30) & 0x1FF;
    let i_pd = (vpage >> 21) & 0x1FF;
    let i_pt = (vpage >> 12) & 0x1FF;

    // Refuse to write under any PML4 slot we did not create. This is what
    // makes "never touch a Limine table" structural, not aspirational.
    let private_slot = PRIVATE_SLOT;
    assert!(
        i_pml4 == private_slot,
        "paging: refusing to map under foreign PML4 slot {} (private slot {})",
        i_pml4,
        private_slot
    );

    let pml4e = pmm::read_va(hhdm + pml4 + i_pml4 * 8);
    assert!(
        pml4e & PTE_PRESENT != 0,
        "paging: private slot missing its PDPT"
    );
    let pdpt = pml4e & ADDR_MASK;

    if huge {
        // A PD holds 512 PS=1 leaves, so many 2 MiB pages share one PDPT
        // entry. Only a 1 GiB leaf (PS set at PDPT level) is incompatible.
        let pdpte = pmm::read_va(hhdm + pdpt + i_pdpt * 8);
        assert!(
            pdpte & PTE_HUGE == 0,
            "paging: 2 MiB map under an existing 1 GiB leaf"
        );
        let pd = if pdpte & PTE_PRESENT == 0 {
            let t = pmm::alloc_frame().expect("paging: out of frames for PD");
            pmm::zero_frame(t);
            pmm::write_va(hhdm + pdpt + i_pdpt * 8, t | PTE_PRESENT | PTE_WRITABLE);
            t
        } else {
            pdpte & ADDR_MASK
        };
        let existing = pmm::read_va(hhdm + pd + i_pd * 8);
        assert!(
            existing & PTE_PRESENT == 0,
            "paging: 2 MiB map over an existing PD entry ({:#x})",
            existing
        );
        pmm::write_va(hhdm + pd + i_pd * 8, ppage | flags);
    } else {
        let pdpte = pmm::read_va(hhdm + pdpt + i_pdpt * 8);
        if pdpte & PTE_PRESENT == 0 {
            let pd = pmm::alloc_frame().expect("paging: out of frames for PD");
            pmm::zero_frame(pd);
            pmm::write_va(hhdm + pdpt + i_pdpt * 8, pd | PTE_PRESENT | PTE_WRITABLE);
        } else if pdpte & PTE_HUGE != 0 {
            panic!("paging: 4 KiB map over an existing 1 GiB leaf");
        }
        let pd = pmm::read_va(hhdm + pdpt + i_pdpt * 8) & ADDR_MASK;

        let pde = pmm::read_va(hhdm + pd + i_pd * 8);
        if pde & PTE_PRESENT == 0 {
            let pt = pmm::alloc_frame().expect("paging: out of frames for PT");
            pmm::zero_frame(pt);
            pmm::write_va(hhdm + pd + i_pd * 8, pt | PTE_PRESENT | PTE_WRITABLE);
        } else if pde & PTE_HUGE != 0 {
            panic!("paging: 4 KiB map under an existing 2 MiB leaf");
        }
        let pt = pmm::read_va(hhdm + pd + i_pd * 8) & ADDR_MASK;
        pmm::write_va(hhdm + pt + i_pt * 8, ppage | flags);
    }
}

/// Remove a 2 MiB mapping created by `map_huge_2m` and invalidate the TLB
/// entry for the page (SDM Vol. 3 §4.10.3: software must use INVLPG or a CR3
/// reload after clearing a present mapping, or a stale translation can
/// survive in the TLB). The INVLPG effective address is the page's own.
pub fn unmap_2m(vpage: u64) {
    const TWO_M: u64 = 2 << 20;
    assert!(vpage.is_multiple_of(TWO_M), "paging: unmap_2m misaligned");
    lock();
    unsafe {
        let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM required");
        let i_pml4 = (vpage >> 39) & 0x1FF;
        let i_pdpt = (vpage >> 30) & 0x1FF;
        let i_pd = (vpage >> 21) & 0x1FF;
        let private_slot = PRIVATE_SLOT;
        assert!(
            i_pml4 == private_slot,
            "paging: refusing to unmap under foreign PML4 slot {}",
            i_pml4
        );
        let pdpt = pmm::read_va(hhdm + PML4 + i_pml4 * 8) & ADDR_MASK;
        let pd = pmm::read_va(hhdm + pdpt + i_pdpt * 8) & ADDR_MASK;
        let old = pmm::read_va(hhdm + pd + i_pd * 8);
        assert!(
            old & PTE_PRESENT != 0,
            "paging: unmap_2m of a not-present page"
        );
        assert!(old & PTE_HUGE != 0, "paging: unmap_2m of a non-leaf entry");
        pmm::write_va(hhdm + pd + i_pd * 8, 0);
        // If that cleared the PD's last entry, return the PD itself and the
        // PDPTE slot to the free pool, so map/unmap pairs net to zero frames.
        let mut empty = true;
        for w in 0..512u64 {
            if pmm::read_va(hhdm + pd + w * 8) != 0 {
                empty = false;
                break;
            }
        }
        if empty {
            pmm::free_frame(pd);
            pmm::write_va(hhdm + pdpt + i_pdpt * 8, 0);
        }
        if ACTIVE {
            // Intel-syntax memory operand: INVLPG takes the effective
            // address of the mapped page (SDM Vol. 3 §4.10.4.3).
            asm!(
                "invlpg [{0}]",
                in(reg) vpage,
                options(nostack, preserves_flags)
            );
        }
    }
    unlock();
}

/// Software page-table walk (SDM Vol. 3 §4.5 order: PML4E -> PDPTE -> PDE ->
/// PTE), honouring 1 GiB and 2 MiB leaves. Returns the physical address `va`
/// translates to, or `None` for non-canonical `va` or a not-present walk.
/// Reads the kernel PML4 before activation and live CR3 after.
pub fn translate(va: u64) -> Option<u64> {
    // Canonical form: bits 63:47 must all equal bit 47 (SDM Vol. 3 §4.5,
    // "Canonical Addressing" - with 4-level paging bits 63:47 are sign bits).
    if !canonical(va) {
        return None;
    }
    lock();
    let out = unsafe { translate_inner(va) };
    unlock();
    out
}

fn canonical(va: u64) -> bool {
    let hi = (va >> 47) & 0x1_FFFF;
    hi == 0 || hi == 0x1_FFFF
}

unsafe fn translate_inner(va: u64) -> Option<u64> {
    let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM required");
    let root = if ACTIVE {
        current_cr3() & ADDR_MASK
    } else {
        PML4
    };
    let i_pml4 = (va >> 39) & 0x1FF;
    let i_pdpt = (va >> 30) & 0x1FF;
    let i_pd = (va >> 21) & 0x1FF;
    let i_pt = (va >> 12) & 0x1FF;
    let off = va & 0xFFF;

    let pml4e = pmm::read_va(hhdm + root + i_pml4 * 8);
    if pml4e & PTE_PRESENT == 0 {
        return None;
    }
    let pdpt = pml4e & ADDR_MASK;
    let pdpte = pmm::read_va(hhdm + pdpt + i_pdpt * 8);
    if pdpte & PTE_PRESENT == 0 {
        return None;
    }
    if pdpte & PTE_HUGE != 0 {
        // 1 GiB leaf: physical base in bits 51:30 (SDM Vol. 3 §4.5 PDPTE).
        return Some((pdpte & 0x000F_FFFF_C000_0000) + (va & 0x3FFF_FFFF));
    }
    let pd = pdpte & ADDR_MASK;
    let pde = pmm::read_va(hhdm + pd + i_pd * 8);
    if pde & PTE_PRESENT == 0 {
        return None;
    }
    if pde & PTE_HUGE != 0 {
        // 2 MiB leaf: physical base in bits 51:21 (SDM Vol. 3 §4.5 PDE).
        return Some((pde & 0x000F_FFFF_FFE0_0000) + (va & 0x1F_FFFF));
    }
    let pt = pde & ADDR_MASK;
    let pte = pmm::read_va(hhdm + pt + i_pt * 8);
    if pte & PTE_PRESENT == 0 {
        return None;
    }
    Some((pte & ADDR_MASK) + off)
}

// ---------------------------------------------------------------------------
// K3b: guard pages.
//
// The boot stack was mapped by Limine, so its translation path may run
// entirely through inherited tables. Every edit below therefore COPY-THEN-
// EDITS: the owning table at each level is copied verbatim into a fresh
// frame and the level above is re-pointed at the copy (under the write
// lock). Limine's own tables are never written, and no other mapping loses
// its translation: verbatim copies preserve every sibling entry, and leaf
// splits re-express exactly the same physical window at finer granularity.
// ---------------------------------------------------------------------------

/// Read the live PML4 (before activate: the kernel PML4; after: CR3) via HHDM.
/// Caller must hold the lock.
unsafe fn root_phys() -> u64 {
    if ACTIVE {
        current_cr3() & ADDR_MASK
    } else {
        PML4
    }
}

/// The entry `e` points at the table we want to edit, but that table lives in
/// territory we never write. Copy it verbatim into a fresh zeroed frame and
/// return the copy. Caller holds the lock.
unsafe fn copy_table(hhdm: u64, e: u64) -> u64 {
    let src = e & ADDR_MASK;
    let dst = pmm::alloc_frame().expect("paging: out of frames copying a page table");
    pmm::zero_frame(dst);
    for w in 0..512u64 {
        let v = pmm::read_va(hhdm + src + w * 8);
        if v != 0 {
            pmm::write_va(hhdm + dst + w * 8, v);
        }
    }
    dst
}

/// Split a 1 GiB leaf at PDPT level into 512 2 MiB PD entries covering
/// exactly the same window (SDM Vol. 3 §4.5: 1 GiB base = bits 51:30, so the
/// window's 2 MiB step is (leaf & 0x000F_FFFF_C000_0000) + k * 2 MiB).
/// Caller holds the lock.
unsafe fn split_1g(hhdm: u64, owner_table: u64, idx: u64, leaf: u64) {
    let pd = pmm::alloc_frame().expect("paging: out of frames splitting 1 GiB leaf");
    pmm::zero_frame(pd);
    let base = leaf & 0x000F_FFFF_C000_0000;
    let rw = leaf & PTE_WRITABLE;
    let nx = leaf & (1 << 63);
    for k in 0..512u64 {
        pmm::write_va(
            hhdm + pd + k * 8,
            (base + (k << 21)) | PTE_PRESENT | PTE_HUGE | rw | nx,
        );
    }
    pmm::write_va(
        hhdm + owner_table + idx * 8,
        pd | PTE_PRESENT | PTE_WRITABLE,
    );
}

/// Split a 2 MiB leaf at PD level into 512 4 KiB PT entries covering exactly
/// the same window (SDM Vol. 3 §4.5: 2 MiB base = bits 51:21, so the window's
/// 4 KiB step is (leaf & 0x000F_FFFF_FFE0_0000) + k * 4 KiB).
/// Caller holds the lock.
unsafe fn split_2m(hhdm: u64, owner_table: u64, idx: u64, leaf: u64) {
    let pt = pmm::alloc_frame().expect("paging: out of frames splitting 2 MiB leaf");
    pmm::zero_frame(pt);
    let base = leaf & 0x000F_FFFF_FFE0_0000;
    let rw = leaf & PTE_WRITABLE;
    let nx = leaf & (1 << 63);
    for k in 0..512u64 {
        pmm::write_va(
            hhdm + pt + k * 8,
            (base + (k << 12)) | PTE_PRESENT | rw | nx,
        );
    }
    pmm::write_va(
        hhdm + owner_table + idx * 8,
        pt | PTE_PRESENT | PTE_WRITABLE,
    );
}

/// (PML4, PDPT, PD, PT) indices of `va` (SDM Vol. 3 §4.5 address bits per
/// level). Helper for the walk functions below.
unsafe fn path_indices(va: u64) -> (u64, u64, u64, u64) {
    (
        (va >> 39) & 0x1FF,
        (va >> 30) & 0x1FF,
        (va >> 21) & 0x1FF,
        (va >> 12) & 0x1FF,
    )
}

/// Make the translation path of `va` kernel-owned at every level, copying
/// inherited tables down (and splitting huge leaves) as needed. After this
/// returns, the PTE that governs `va`'s page may be edited in place. Caller
/// holds the lock; caller still must software-verify before the CPU re-walks.
unsafe fn privatize_path(va: u64) {
    let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM required");
    let root = root_phys();
    let (i_pml4, i_pdpt, i_pd, _i_pt) = path_indices(va);

    // PML4 entry -> PDPT. If the PDPT was inherited from Limine, copy it and
    // re-point our own PML4 entry at the copy.
    let pml4e = pmm::read_va(hhdm + root + i_pml4 * 8);
    assert!(
        pml4e & PTE_PRESENT != 0,
        "paging: privatize_path on an unmapped va {:#x}",
        va
    );
    let privatized = {
        let (w, b) = ((i_pml4 / 64) as usize, i_pml4 % 64);
        PRIVATIZED_SLOTS[w] & (1 << b) != 0
    };
    let pdpt = if privatized {
        pml4e & ADDR_MASK
    } else {
        let c = copy_table(hhdm, pml4e);
        pmm::write_va(hhdm + root + i_pml4 * 8, c | PTE_PRESENT | PTE_WRITABLE);
        let (w, b) = ((i_pml4 / 64) as usize, i_pml4 % 64);
        PRIVATIZED_SLOTS[w] |= 1 << b;
        c
    };

    // PDPT entry -> PD. Inherited (present, non-huge) or a 1 GiB leaf: both
    // must be replaced with kernel-owned state before we edit below it.
    let mut pdpte = pmm::read_va(hhdm + pdpt + i_pdpt * 8);
    assert!(
        pdpte & PTE_PRESENT != 0,
        "paging: privatize_path on a not-present PDPT entry"
    );
    if pdpte & PTE_HUGE != 0 {
        split_1g(hhdm, pdpt, i_pdpt, pdpte);
        pdpte = pmm::read_va(hhdm + pdpt + i_pdpt * 8);
    }
    let pd = if privatized {
        // The PDPT is ours; entries below may be edited in place.
        if pdpte & PTE_PRESENT == 0 {
            panic!("paging: privatize_path on a not-present PDPT entry");
        }
        pdpte & ADDR_MASK
    } else {
        let c = copy_table(hhdm, pdpte);
        pmm::write_va(hhdm + pdpt + i_pdpt * 8, c | PTE_PRESENT | PTE_WRITABLE);
        c
    };

    // PD entry -> PT. Inherited present non-huge: copy. 2 MiB leaf: split.
    let mut pde = pmm::read_va(hhdm + pd + i_pd * 8);
    assert!(
        pde & PTE_PRESENT != 0,
        "paging: privatize_path on a not-present PD entry"
    );
    if pde & PTE_HUGE != 0 {
        split_2m(hhdm, pd, i_pd, pde);
        pde = pmm::read_va(hhdm + pd + i_pd * 8);
    }
    if !privatized {
        let c = copy_table(hhdm, pde);
        pmm::write_va(hhdm + pd + i_pd * 8, c | PTE_PRESENT | PTE_WRITABLE);
    }
}

/// Software-verify the path we just privatized and edited: the guard `va`
/// itself must walk to not-present, and every OTHER page of the surrounding
/// region must translate exactly as the pre-edit walk recorded. Any other
/// result is a corruption bug; refusing to TLB-flush on failure is what keeps
/// the CPU's view coherent with the software walk.
fn verify_path_equivalence(pre: &[(u64, Option<u64>)], guard_va: u64) {
    for (probe_va, want) in pre {
        // translate_inner, not translate: the caller already holds the
        // paging spinlock, and translate() would take it again (deadlock).
        let got = unsafe { translate_inner(*probe_va) };
        let ok = if *probe_va == guard_va {
            got.is_none()
        } else {
            got == *want
        };
        assert!(
            ok,
            "paging: guard-page edit corrupted a translation: va {:#x} was {:?}, now {:?}",
            probe_va, want, got
        );
    }
}

/// Full TLB flush: a CR3 reload invalidates non-global translations
/// (SDM Vol. 3 §4.10.4.1), and toggling CR4.PGE around it also evicts the
/// global ones (SDM Vol. 3 §4.10.2.2 "operations that invalidate ... all
/// TLB entries: a write to CR4 that modifies the PGE flag"). Guard pages
/// cannot rely on INVLPG alone: the overwritten entry may be a global one
/// inherited from Limine's mapping.
pub fn tlb_flush_all() {
    unsafe {
        let mut cr4: u64;
        asm!("mov {}, cr4", out(reg) cr4, options(nomem, nostack, preserves_flags));
        let pge = cr4 & (1 << 7);
        if pge != 0 {
            asm!(
                "mov cr4, {0}",
                in(reg) cr4 & !(1 << 7),
                options(nostack, preserves_flags)
            );
        }
        let cr3 = current_cr3();
        asm!("mov cr3, {0}", in(reg) cr3, options(nostack, preserves_flags));
        if pge != 0 {
            asm!(
                "mov cr4, {0}",
                in(reg) cr4,
                options(nostack, preserves_flags)
            );
        }
    }
}

/// Map the 4 KiB page containing `va` not-present (a guard page). Returns
/// false and changes nothing when `va`'s page is already not present.
///
/// The path through inherited Limine tables is privatized first (copy then
/// edit), any huge leaf on the path is split, and the whole edit is checked
/// with the software walk BEFORE the TLB is flushed - the CPU never sees an
/// unverified state.
pub fn set_page_not_present(va: u64) -> bool {
    let page = va & !0xFFF;
    lock();
    let result = unsafe {
        let hhdm = crate::limine::hhdm_offset().expect("paging: HHDM required");
        let root = root_phys();
        let (i_pml4, i_pdpt, i_pd, i_pt) = path_indices(va);

        let pml4e = pmm::read_va(hhdm + root + i_pml4 * 8);
        if pml4e & PTE_PRESENT == 0 {
            unlock();
            return false;
        }
        let pdpt = pml4e & ADDR_MASK;
        let pdpte = pmm::read_va(hhdm + pdpt + i_pdpt * 8);
        if pdpte & PTE_PRESENT == 0 {
            unlock();
            return false;
        }
        if pdpte & PTE_HUGE != 0 {
            // 1 GiB leaf covering the page: it is present by definition.
            privatize_path(va);
        } else {
            let pd = pdpte & ADDR_MASK;
            let pde = pmm::read_va(hhdm + pd + i_pd * 8);
            if pde & PTE_PRESENT == 0 {
                unlock();
                return false;
            }
            if pde & PTE_HUGE == 0 {
                let pt = pde & ADDR_MASK;
                let pte = pmm::read_va(hhdm + pt + i_pt * 8);
                if pte & PTE_PRESENT == 0 {
                    unlock();
                    return false;
                }
            }
        }

        // Capture the region's pre-edit translations (the oracle for "only
        // the guard page changed"), then make the path kernel-owned.
        let region_base = (va & !0x1F_FFFF) & !0xFFF;
        let mut pre: [(u64, Option<u64>); 10] = [(0, None); 10];
        let probes = [
            region_base,
            region_base + 0x1000,
            region_base + 0x2000,
            region_base + 0x1F_D000,
            region_base + 0x1F_E000,
            region_base + 0x1F_F000,
            va & !0xFFF,
            va + 0x1000,
            va + 0x2000,
            va + 0x3000,
        ];
        // Pre-edit walk uses translate_inner: we hold the lock (translate()
        // would re-take it and deadlock), and the CR3/register state it
        // consults is exactly what translate() would read.
        for (slot, p) in pre.iter_mut().zip(probes.iter()) {
            *slot = (*p, translate_inner(*p));
        }
        if translate_inner(page).is_none() {
            unlock();
            return false;
        }

        privatize_path(va);
        // Re-read the path: after privatization every level is kernel-owned
        // and any huge leaf has been split into a PT.
        let root2 = root_phys();
        let pml4e2 = pmm::read_va(hhdm + root2 + i_pml4 * 8);
        let pdpt2 = pml4e2 & ADDR_MASK;
        let pde2 = pmm::read_va(hhdm + pdpt2 + i_pdpt * 8);
        let pd2 = pde2 & ADDR_MASK;
        let pte2 = pmm::read_va(hhdm + pd2 + i_pd * 8);
        let pt2 = pte2 & ADDR_MASK;
        let old = pmm::read_va(hhdm + pt2 + i_pt * 8);
        assert!(
            old & PTE_PRESENT != 0 && old & PTE_HUGE == 0,
            "paging: post-privatize entry is not a 4 KiB PTE ({:#x})",
            old
        );
        pmm::write_va(hhdm + pt2 + i_pt * 8, old & !PTE_PRESENT);

        // Software-verify BEFORE any TLB invalidation; on any disagreement
        // the assert fires with the machine still on the pre-edit mappings.
        verify_path_equivalence(&pre, page);

        if ACTIVE {
            tlb_flush_all();
        }
        true
    };
    unlock();
    result
}

/// Switch CR3 to the kernel PML4. Runs after the exception selftest; on
/// success the direct map, kernel mapping, and framebuffer keep working
/// (the boot-report lines after this prove it in every QEMU run).
pub fn activate() {
    assert!(pmm::ready(), "paging: pmm must init first");
    let target = unsafe { PML4 };
    assert!(target != 0, "paging: init must run before activate");
    unsafe {
        asm!(
            "mov cr3, {0}",
            in(reg) target,
            options(nostack, preserves_flags)
        );
        ACTIVE = true;
    }
    sprintln!(
        "paging:      CR3 switched to the kernel PML4 ({:#x})",
        target
    );
}

/// True once `activate` has switched CR3.
pub fn active() -> bool {
    unsafe { ACTIVE }
}
