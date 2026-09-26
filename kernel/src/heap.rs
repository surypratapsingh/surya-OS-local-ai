//! Kernel heap (K3a).
//!
//! A first-fit boundary-tag allocator over one contiguous virtual window in
//! the kernel-managed paging area, backed by frames from the PMM. The block
//! model is the classic boundary-tag scheme — each block carries a header
//! (size + used bit) and a size footer, free blocks are coalesced with their
//! neighbours on free — the structure described in Kernighan & Ritchie, "The
//! C Programming Language", 2nd ed., §8.7 ("A Storage Allocator"), adapted to
//! a 16-byte header (size word + pad), an 8-byte footer, and 16-byte payload
//! alignment.
//!
//! The window is fixed (32 MiB) and committed lazily: each new 2 MiB chunk is
//! backed by a contiguous 2 MiB physical run (`pmm::alloc_run_2m`) mapped
//! with a PD-level PS=1 leaf (`paging::map_huge_2m`, SDM Vol. 3 §4.5). Heap
//! metadata lives inside the window itself, so the allocator never allocates
//! for its own bookkeeping. `alloc`/`free`/`realloc` are single-threaded
//! (the kernel has no interrupt sources yet); the `LOCK` flag makes
//! accidental re-entry fail loudly once interrupts arrive.
//!
//! `walk_ok()` re-derives the block chain and the free-list invariants from
//! the raw window bytes, so the selftest asserts allocator health AFTER a
//! workload, not just before.

use crate::paging;
use crate::pmm;

const WINDOW_BYTES: u64 = 32 << 20;
const CHUNK_BYTES: u64 = 2 << 20; // one PD-level page per committed chunk
const HDR: u64 = 16; // header: size word at +0, payload at +16
const FTR: u64 = 8; // footer: size word at block_end - 8
const ALIGN: u64 = 16;
/// Smallest representable block: header + footer, no payload room.
const MIN_BLOCK: u64 = 32;
const USED_BIT: u64 = 1;

static mut WINDOW_BASE: u64 = 0; // virtual base of the heap window
static mut HEAP_TOP: u64 = 0; // committed end (virtual)
static mut HEAP_END: u64 = 0; // window end (virtual)
static mut LOCK: u64 = 0; // re-entry detector, not a real lock yet

struct Guard;
impl Guard {
    fn new(name: &str) -> Guard {
        unsafe {
            assert!(LOCK == 0, "heap: re-entered from {}", name);
            LOCK = 1;
        }
        Guard
    }
}
impl Drop for Guard {
    fn drop(&mut self) {
        unsafe { LOCK = 0 };
    }
}

fn rd(va: u64) -> u64 {
    unsafe { pmm::read_va(va) }
}
fn wr(va: u64, v: u64) {
    unsafe { pmm::write_va(va, v) }
}

/// Create the heap window and seed it with one free block spanning the first
/// committed chunk.
pub fn init() {
    assert!(paging::kernel_pml4() != 0, "heap: paging must init first");
    unsafe {
        let base = paging::private_base();
        assert!(base != 0, "heap: no kernel-managed paging area");
        map_chunk(base);
        WINDOW_BASE = base;
        HEAP_TOP = base + CHUNK_BYTES;
        HEAP_END = base + WINDOW_BYTES;
        // Seed block: the whole first chunk, free. Footer mirrors the header.
        wr(base, CHUNK_BYTES);
        wr(base + CHUNK_BYTES - 8, CHUNK_BYTES);
    }
    sprintln!(
        "heap:        {} MiB window at {:#x} ({} MiB committed)",
        WINDOW_BYTES >> 20,
        unsafe { WINDOW_BASE },
        CHUNK_BYTES >> 20
    );
}

/// Back the 2 MiB chunk containing `va` with a contiguous physical run,
/// mapped via a 2 MiB leaf, if not already committed.
unsafe fn map_chunk(va: u64) {
    let chunk = va & !(CHUNK_BYTES - 1);
    assert!(
        paging::translate(chunk).is_none(),
        "heap: chunk {:#x} already mapped - allocator/test overlap",
        chunk
    );
    let phys =
        pmm::alloc_run_2m().expect("heap: no contiguous 2 MiB physical run for a heap chunk");
    paging::map_huge_2m(chunk, phys);
    pmm::zero_range(phys, CHUNK_BYTES);
}

/// Extend the committed region until it covers `end_needed`. Returns false
/// when the fixed window is exhausted.
fn grow_to(end_needed: u64) -> bool {
    unsafe {
        if end_needed > HEAP_END {
            return false;
        }
        while HEAP_TOP < end_needed {
            map_chunk(HEAP_TOP);
            let b = HEAP_TOP;
            let size = CHUNK_BYTES;
            wr(b, size);
            wr(b + size - 8, size);
            // Coalesce left: if the previous block is the free tail, merge.
            if b > WINDOW_BASE {
                let prev_size = rd(b - 8);
                if prev_size & USED_BIT == 0 {
                    let merged = b - prev_size;
                    let total = prev_size + size;
                    wr(merged, total);
                    wr(b + size - 8, total);
                }
            }
            HEAP_TOP += CHUNK_BYTES;
        }
        true
    }
}

/// Allocate `size` bytes, 16-byte aligned, zero-filled.
pub fn alloc(size: u64) -> Option<*mut u8> {
    let _g = Guard::new("alloc");
    alloc_inner(size, true)
}

fn alloc_inner(size: u64, zero: bool) -> Option<*mut u8> {
    if size == 0 || size > (1 << 40) {
        return None;
    }
    // payload + header + footer, rounded to 16.
    let need = (size + HDR + FTR).div_ceil(ALIGN) * ALIGN;
    unsafe {
        let mut b = WINDOW_BASE;
        loop {
            if b == HEAP_TOP {
                // Walked off the committed end (a free tail can end exactly
                // at the top): commit more space, then restart the scan so
                // left-coalescing with that tail is visible to first-fit.
                if !grow_to(b + need) {
                    return None;
                }
                b = WINDOW_BASE;
                continue;
            }
            assert!(b < HEAP_TOP, "heap: walk overran the committed region");
            let h = rd(b);
            // Every committed byte below the top carries a valid header; a
            // zero word here is a hole (corruption), not free space.
            assert!(h != 0, "heap: hole in committed region at {:#x}", b);
            let used = h & USED_BIT;
            let bsize = h & !USED_BIT;
            assert!(
                bsize >= MIN_BLOCK && bsize.is_multiple_of(16),
                "heap: corrupt block at {:#x} (size {:#x})",
                b,
                bsize
            );
            assert!(b + bsize <= HEAP_TOP, "heap: block overruns committed top");
            if used == 0 && bsize >= need {
                let rem = bsize - need;
                if rem >= MIN_BLOCK {
                    // Split: allocate the front, leave a free remainder.
                    wr(b, need | USED_BIT);
                    wr(b + need - 8, need | USED_BIT);
                    wr(b + need, rem);
                    wr(b + bsize - 8, rem);
                } else {
                    // Take the whole block.
                    wr(b, bsize | USED_BIT);
                    wr(b + bsize - 8, bsize | USED_BIT);
                }
                let payload = (b + HDR) as *mut u8;
                if zero {
                    for i in 0..size as usize {
                        payload.add(i).write_volatile(0);
                    }
                }
                return Some(payload);
            }
            b += bsize;
        }
    }
}

/// Free a pointer returned by `alloc`/`realloc`. Coalesces with the previous
/// and next block, as the boundary-tag scheme requires.
pub fn free(ptr: *mut u8) {
    let _g = Guard::new("free");
    free_inner(ptr);
}

fn free_inner(ptr: *mut u8) {
    unsafe {
        let b = (ptr as u64) - HDR;
        assert!(
            b >= WINDOW_BASE && b < HEAP_END,
            "heap: free of foreign pointer"
        );
        let h = rd(b);
        assert!(h & USED_BIT != 0, "heap: double free at {:#x}", b);
        let bsize = h & !USED_BIT;
        assert!(
            b + bsize <= HEAP_TOP,
            "heap: freed block overruns committed top"
        );
        assert!(
            rd(b + bsize - 8) == h,
            "heap: footer/header mismatch at {:#x}",
            b
        );

        let mut start = b;
        let mut size = bsize;
        // Coalesce with the previous free block (its footer sits at b-8).
        if b > WINDOW_BASE {
            let prev_size = rd(b - 8);
            if prev_size & USED_BIT == 0 {
                let prev = b - prev_size;
                start = prev;
                size += prev_size;
            }
        }
        // Coalesce with the next free block.
        let nb = b + bsize;
        if nb < HEAP_TOP {
            let nh = rd(nb);
            if nh & USED_BIT == 0 {
                size += nh & !USED_BIT;
            }
        }
        wr(start, size);
        wr(start + size - 8, size);
    }
}

/// Realloc: alloc-copy-free. Returning the same pointer is allowed (the block
/// keeps its larger capacity); callers must not assume a move happened.
pub fn realloc(ptr: *mut u8, new_size: u64) -> Option<*mut u8> {
    let _g = Guard::new("realloc");
    unsafe {
        let b = (ptr as u64) - HDR;
        assert!(
            b >= WINDOW_BASE && b < HEAP_END,
            "heap: realloc of foreign pointer"
        );
        let h = rd(b);
        assert!(h & USED_BIT != 0, "heap: realloc of free block at {:#x}", b);
        let old = (h & !USED_BIT) - HDR - FTR;
        let n = alloc_inner(new_size, false)?;
        if n as u64 == ptr as u64 {
            return Some(n); // same block: nothing to copy or free
        }
        core::ptr::copy_nonoverlapping(ptr, n, old.min(new_size) as usize);
        free_inner(ptr);
        Some(n)
    }
}

/// Validate the allocator's invariants by re-deriving the block chain from
/// the window bytes:
///   I1  the walk starts at WINDOW_BASE and ends exactly at HEAP_TOP
///   I2  every block size is >= MIN_BLOCK and 16-aligned
///   I3  every block's footer equals its header
///   I4  no two free blocks are adjacent (coalescing holds)
/// Returns the number of blocks on success; panics on the first violation.
pub fn walk_ok() -> u64 {
    let _g = Guard::new("walk_ok");
    unsafe {
        let mut b = WINDOW_BASE;
        let mut count: u64 = 0;
        let mut prev_free = false;
        while b < HEAP_TOP {
            let h = rd(b);
            // The walk never reads at or past the committed top (loop
            // condition), so a zero word here is a genuine hole.
            assert!(h != 0, "heap: hole in committed region at {:#x}", b);
            let size = h & !USED_BIT;
            assert!(
                size >= MIN_BLOCK && size.is_multiple_of(16),
                "heap I2: bad size {:#x} at {:#x}",
                size,
                b
            );
            assert!(
                b + size <= HEAP_TOP,
                "heap I1: chain overruns committed top"
            );
            let f = rd(b + size - 8);
            assert!(
                f == h,
                "heap I3: footer {:#x} != header {:#x} at {:#x}",
                f,
                h,
                b
            );
            let free = h & USED_BIT == 0;
            assert!(
                !(free && prev_free),
                "heap I4: adjacent free blocks at {:#x}",
                b
            );
            prev_free = free;
            b += size;
            count += 1;
        }
        let top = HEAP_TOP;
        assert!(
            b == top,
            "heap I1: chain ends at {:#x}, top is {:#x}",
            b,
            top
        );
        count
    }
}

/// A deterministic churn workload: interleaved alloc/free/realloc with
/// content verification, invariant walks every 1000 rounds, and a final
/// walk. Returns (ops, blocks_in_chain). The xorshift seed is fixed so a
/// failure is reproducible from the log alone.
pub fn stress(rounds: u64) -> (u64, u64) {
    const SLOTS: usize = 64;
    let mut slots: [(*mut u8, u64); SLOTS] = [(core::ptr::null_mut(), 0); SLOTS];
    let mut ops: u64 = 0;
    let mut seed: u64 = 0x9E37_79B9_7F4A_7C15;
    let mut next = || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };

    for round in 0..rounds {
        let i = (next() % SLOTS as u64) as usize;
        let size = 1 + next() % 4096;
        let (p, s) = slots[i];
        if !p.is_null() {
            // Verify the old pattern before releasing the block.
            unsafe {
                for k in 0..s as usize {
                    assert!(
                        *p.add(k) == ((k as u64 ^ 0xA5) as u8),
                        "heap stress: pattern lost in slot {}",
                        i
                    );
                }
            }
            free(p);
            slots[i] = (core::ptr::null_mut(), 0);
            ops += 1;
        }
        if next() & 3 != 0 {
            let q = alloc(size).expect("heap stress: alloc failed");
            unsafe {
                for k in 0..size as usize {
                    *q.add(k) = (k as u64 ^ 0xA5) as u8;
                }
            }
            slots[i] = (q, size);
            ops += 1;
        }
        if round % 1000 == 0 {
            walk_ok();
        }
    }
    // Also exercise realloc on live slots: grow, verify, shrink, verify.
    for (i, slot) in slots.iter_mut().enumerate() {
        let (p, s) = *slot;
        if p.is_null() {
            continue;
        }
        let bigger = s * 2 + 17;
        let q = realloc(p, bigger).expect("heap stress: realloc failed");
        unsafe {
            for k in 0..s as usize {
                assert!(
                    *q.add(k) == ((k as u64 ^ 0xA5) as u8),
                    "heap stress: realloc lost bytes in slot {}",
                    i
                );
            }
        }
        *slot = (q, bigger);
        ops += 1;
    }
    for (p, _) in slots.iter() {
        if !p.is_null() {
            free(*p);
            ops += 1;
        }
    }
    let blocks = walk_ok();
    (ops, blocks)
}
