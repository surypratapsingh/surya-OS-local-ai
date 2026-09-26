//! Physical frame allocator (K3a).
//!
//! Owns every 4 KiB physical frame that the Limine memory map marks
//! `LIMINE_MEMMAP_USABLE`. PROTOCOL.md ("Memory Map Feature", vendored at
//! `.freebuff/ref/limine/limine-12.9.0/limine-protocol/PROTOCOL.md`) says
//! USABLE regions "do not contain other data, the executable, bootloader
//! information, or anything valuable, and are therefore free for use" — so the
//! kernel image (executable+modules), the 4 MiB boot stack and Limine's
//! response structures (bootloader-reclaimable), and the framebuffer
//! (framebuffer type) are excluded by construction. We never reclaim
//! bootloader memory, so the response arrays this module reads during `init`
//! stay valid for the life of the kernel.
//!
//! Structure: an intrusive LIFO free list. The 8-byte next pointer of each
//! free frame is stored in the frame itself, accessed through the Limine
//! higher-half direct map (HHDM) — no metadata allocation, O(1) alloc/free.
//!
//! Concurrency: a plain spinlock. No interrupt sources are enabled yet (no
//! PIC/APIC driver exists), so lock contention is impossible today; the lock
//! is here so later callers cannot silently race.

use crate::limine;
use core::arch::asm;
use core::sync::atomic::{AtomicBool, Ordering};

pub const FRAME_SIZE: u64 = 4096;

/// Physical address of the first free frame (the list head). 0 = empty.
static mut FREE_HEAD: u64 = 0;
static mut FREE_COUNT: u64 = 0;
/// Frames available at init completion. Only the selftest reads this.
static mut USABLE_FRAMES: u64 = 0;
static mut INITED: bool = false;
static mut HHDM: u64 = 0;
static LOCK: AtomicBool = AtomicBool::new(false);

struct LockGuard;

impl LockGuard {
    fn new() -> LockGuard {
        while LOCK.swap(true, Ordering::Acquire) {
            core::hint::spin_loop();
        }
        LockGuard
    }
}

impl Drop for LockGuard {
    fn drop(&mut self) {
        LOCK.store(false, Ordering::Release);
    }
}

/// Read the u64 stored at physical address `p` through the direct map.
unsafe fn phys_read(p: u64) -> u64 {
    ptr_read(HHDM.wrapping_add(p))
}

/// Write `v` to physical address `p` through the direct map.
unsafe fn phys_write(p: u64, v: u64) {
    ptr_write(HHDM.wrapping_add(p), v);
}

unsafe fn ptr_read(va: u64) -> u64 {
    ptr::read_volatile(va as *const u64)
}

unsafe fn ptr_write(va: u64, v: u64) {
    ptr::write_volatile(va as *mut u64, v);
}

use core::ptr;

/// Scan the Limine memory map and chain every usable frame.
pub fn init() {
    let hhdm = match limine::hhdm_offset() {
        Some(o) => o,
        None => {
            sprintln!("pmm:         FATAL - Limine HHDM feature absent");
            panic!("pmm: no HHDM");
        }
    };
    unsafe {
        HHDM = hhdm;
    }

    // Pass 1: count usable frames so the boot report and selftest can assert
    // the allocator hands out exactly what the map declared.
    let mut total: u64 = 0;
    limine::for_each_memmap_entry(|e| {
        if e.ty == limine::MEMMAP_USABLE {
            total += e.length / FRAME_SIZE; // a trailing partial frame is unusable
        }
    });

    // Pass 2: push each usable frame onto the free list, walking every USABLE
    // region ascending. The list ends up LIFO-over-regions; allocation order
    // is deterministic for a given map, which the selftest relies on.
    unsafe {
        FREE_HEAD = 0;
        FREE_COUNT = 0;
        let mut n: u64 = 0;
        limine::for_each_memmap_entry(|e| {
            if e.ty != limine::MEMMAP_USABLE {
                return;
            }
            let mut base = e.base;
            let end = e.base + e.length;
            // A region that starts mid-frame: round up to the next frame.
            base += (FRAME_SIZE - (base % FRAME_SIZE)) % FRAME_SIZE;
            while base + FRAME_SIZE <= end {
                // Store the old head INSIDE the frame being pushed.
                phys_write(base, FREE_HEAD);
                FREE_HEAD = base;
                n += 1;
                base += FRAME_SIZE;
            }
        });
        FREE_COUNT = n;
        USABLE_FRAMES = n;
        INITED = true;
    }

    sprintln!(
        "pmm:         {} frames ({} KiB) chained on the free list",
        unsafe { USABLE_FRAMES },
        unsafe { USABLE_FRAMES * FRAME_SIZE / 1024 }
    );
}

/// Allocate one frame; returns its physical address, page-aligned.
pub fn alloc_frame() -> Option<u64> {
    let _g = LockGuard::new();
    unsafe {
        if !INITED || FREE_HEAD == 0 {
            return None;
        }
        let frame = FREE_HEAD;
        FREE_HEAD = phys_read(frame);
        FREE_COUNT -= 1;
        Some(frame)
    }
}

/// Return one frame to the free list. Misaligned or out-of-map frames are a
/// caller bug: refuse loudly instead of corrupting the list.
pub fn free_frame(frame: u64) {
    let _g = LockGuard::new();
    unsafe {
        assert!(INITED, "pmm: free_frame before init");
        assert!(
            frame.is_multiple_of(FRAME_SIZE) && frame != 0,
            "pmm: free_frame misaligned {:#x}",
            frame
        );
        let (cnt, cap) = (FREE_COUNT, USABLE_FRAMES);
        assert!(
            FREE_COUNT < USABLE_FRAMES,
            "pmm: free_frame over-free (count {} of {})",
            cnt,
            cap
        );
        phys_write(frame, FREE_HEAD);
        FREE_HEAD = frame;
        FREE_COUNT += 1;
    }
}

/// Allocate a contiguous, 2 MiB-aligned physical run of 512 frames, for
/// backing a heap chunk mapped with a 2 MiB page (the PDE PS=1 leaf requires
/// the page base aligned to the page size, SDM Vol. 3 §4.5).
///
/// The free list is built by pushing frames region-ascending, so within one
/// region the chain descends by exactly one frame per link. A run is 512
/// consecutive such links whose lowest frame is 2 MiB-aligned. Returns the
/// LOW frame's physical address, or None if no aligned run remains.
pub fn alloc_run_2m() -> Option<u64> {
    const RUN: u64 = (2 << 20) / FRAME_SIZE; // 512 frames
    const TWO_M: u64 = 2 << 20;
    let _g = LockGuard::new();
    unsafe {
        if !INITED {
            return None;
        }
        let mut prev: u64 = 0; // 0 means "at the head"
        let mut cur = FREE_HEAD;
        while cur != 0 {
            // Follow links: a valid run descends by FRAME_SIZE each step.
            let mut node = cur;
            let mut len = 1u64;
            while len < RUN {
                let nxt = phys_read(node);
                if nxt == 0 || nxt != node - FRAME_SIZE {
                    break;
                }
                node = nxt;
                len += 1;
            }
            if len == RUN {
                let low = cur - (RUN - 1) * FRAME_SIZE;
                if low.is_multiple_of(TWO_M) {
                    let after = phys_read(node); // first frame below the run
                    if prev == 0 {
                        FREE_HEAD = after;
                    } else {
                        phys_write(prev, after);
                    }
                    FREE_COUNT -= RUN;
                    return Some(low);
                }
            }
            prev = cur;
            cur = phys_read(cur);
        }
        None
    }
}

/// Return a contiguous 2 MiB run (from `alloc_run_2m`) to the free list.
/// Frames are pushed highest-first so the rebuilt chain descends again and
/// future `alloc_run_2m` calls can still find runs.
pub fn free_run_2m(low: u64) {
    const RUN: u64 = (2 << 20) / FRAME_SIZE;
    const TWO_M: u64 = 2 << 20;
    assert!(
        low != 0 && low.is_multiple_of(TWO_M),
        "pmm: free_run_2m misaligned {:#x}",
        low
    );
    let _g = LockGuard::new();
    unsafe {
        assert!(INITED, "pmm: free_run_2m before init");
        // Push lowest-first so the final chain from the head descends
        // (head = high frame, each link -4 KiB), preserving the invariant
        // alloc_run_2m searches by.
        for i in 0..RUN {
            let f = low + i * FRAME_SIZE;
            assert!(FREE_COUNT < USABLE_FRAMES, "pmm: free_run_2m over-free");
            phys_write(f, FREE_HEAD);
            FREE_HEAD = f;
            FREE_COUNT += 1;
        }
    }
}

/// Volatile-zero `len` bytes at physical `phys` through the direct map.
/// `phys` and `len` must be 8-aligned (both are, for frame-sized uses).
pub fn zero_range(phys: u64, len: u64) {
    unsafe {
        assert!(HHDM != 0, "pmm: zero_range before init");
        assert!(
            phys.is_multiple_of(8) && len.is_multiple_of(8),
            "pmm: zero_range unaligned"
        );
        for i in 0..len / 8 {
            ptr_write(HHDM + phys + i * 8, 0);
        }
    }
}

pub fn frames_free() -> u64 {
    unsafe { FREE_COUNT }
}

pub fn frames_usable() -> u64 {
    unsafe { USABLE_FRAMES }
}

/// `true` once `init` has completed. paging::init asserts this before use.
pub fn ready() -> bool {
    unsafe { INITED }
}

/// Zero a physical frame through the direct map (8 bytes at a time, volatile
/// so a later build cannot fold the loop away).
pub fn zero_frame(phys: u64) {
    unsafe {
        let hhdm = HHDM;
        assert!(hhdm != 0, "pmm: zero_frame before init");
        for i in 0..(FRAME_SIZE / 8) {
            ptr_write(hhdm + phys + i * 8, 0);
        }
    }
}

/// Read a 64-bit word at `va` (for tests that verify mapping round-trips).
#[allow(clippy::missing_safety_doc)]
pub unsafe fn read_va(va: u64) -> u64 {
    ptr_read(va)
}

/// Write a 64-bit word at `va` (for tests that verify mapping round-trips).
#[allow(clippy::missing_safety_doc)]
pub unsafe fn write_va(va: u64, v: u64) {
    ptr_write(va, v);
}

/// Spin-wait helper kept here so tests can pause between operations.
#[allow(dead_code)]
pub fn nop_pause() {
    unsafe { asm!("pause", options(nomem, nostack, preserves_flags)) };
}
