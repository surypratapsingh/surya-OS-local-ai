//! K4b: supervisor-side ELF64 loader — parse, validate, stage.
//!
//! Scope of this slice (docs/work-orders.md K4 "ELF64 loader"): read an
//! ELF64 image from a byte buffer, reject everything the policy below
//! forbids, and STAGE its PT_LOAD segments into the kernel-managed private
//! area at a fixed window. This slice does NOT create address spaces, does
//! NOT enter ring 3, and does NOT execute the image: `entry` is carried as
//! the raw e_entry value (documented loader policy requires it to land in a
//! file-backed segment) but nothing dereferences it. Own page tables, ring
//! 3 and execution are the next slice; staging here is into the kernel's
//! own PML4 because paging::map_page structurally refuses to write under
//! any other slot (paging.rs, map_inner's private-slot assert).
//!
//! Format reference: System V Application Binary Interface — AMD64
//! Architecture Processor Supplement ("gABI"), ch4.eheader.html (ELF
//! header, figure 1-4 field order) and ch5.pheader.html (program header,
//! figure 5-1; "p_memsz ... if it is smaller than p_filesz the loader is
//! undefined"; "p_align ... if set, p_vaddr shall be congruent to
//! p_offset modulo p_align"). Validation order below follows those rules
//! plus K4b loader policy, stated in `ElfError` order.
//!
//! Staging rule (documented, mirrored by tools/verify-elf64.py and by the
//! hand golden tables in elfselftest.rs): PT_LOAD segments sorted by
//! p_vaddr stage at PAGE_BASE + i*SLOT + (p_vaddr mod 4096), where
//! PAGE_BASE is the first free 2 MiB window in the kernel-managed private
//! area (this slice: PRIVATE_BASE + LOAD_WINDOW) and SLOT is 2 MiB. File
//! bytes [p_offset, p_offset+p_filesz) are copied, then zero-filled to
//! p_memsz. Exact p_vaddr placement is impossible under the kernel's own
//! PML4 by construction (map_page refuses foreign slots); the loader
//! preserves intra-image layout as per-segment page deltas and reports
//! each segment's staged base, delta and sizes in `StagedSegment` so any
//! consumer can reconstruct the intended vaddr layout from the evidence.

use crate::paging;
use crate::pmm;

/// First free window in the kernel-managed private area for staged loads.
/// memselftest uses offsets up to 96 MiB; 128 MiB leaves it clear.
pub const LOAD_WINDOW: u64 = 128 << 20;
/// Per-segment slot (2 MiB, page-aligned like every map_page consumer).
pub const SEGMENT_SLOT: u64 = 2 << 20;
/// Max segments a single image may stage (bound on map_page calls).
pub const MAX_SEGMENTS: usize = 8;

const MAGIC: [u8; 4] = [0x7F, b'E', b'L', b'F'];
const ELFCLASS64: u8 = 2;
const ELFDATA2LSB: u8 = 1;
const EV_CURRENT: u32 = 1;
const ET_EXEC: u16 = 2;
const EM_X86_64: u16 = 62;
const PT_LOAD: u32 = 1;
const PT_INTERP: u32 = 3;
const PAGE: u64 = 4096;

/// Offsets into the 64-byte ELF header (gABI figure 1-4).
const OFF_EI_CLASS: usize = 4;
const OFF_EI_DATA: usize = 5;
const OFF_E_VERSION: usize = 6;
const OFF_E_TYPE: usize = 16;
const OFF_E_MACHINE: usize = 18;
const OFF_E_VERSION32: usize = 20;
const OFF_E_ENTRY: usize = 24;
const OFF_E_PHOFF: usize = 32;
const OFF_E_PHENTSIZE: usize = 54;
const OFF_E_PHNUM: usize = 56;

/// Why an image was refused. Ordered to match the loader's validation
/// order; the gate's negative table names every variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ElfError {
    /// Buffer shorter than the 64-byte header.
    TooShort,
    /// \x7fELF magic mismatch.
    BadMagic,
    /// EI_CLASS != ELFCLASS64.
    NotElf64,
    /// EI_DATA != ELFDATA2LSB.
    NotLittleEndian,
    /// e_version != EV_CURRENT.
    BadVersion,
    /// e_type != ET_EXEC (loader policy: no PIE in this slice).
    NotExec,
    /// e_machine != EM_X86_64.
    NotX86_64,
    /// e_phentsize != 56 (sizeof Elf64_Phdr, gABI figure 5-1).
    BadPhentsize,
    /// e_phnum == 0.
    NoPhdrs,
    /// Program header table (or a segment's file range) outside the buffer.
    RangePastEof,
    /// p_memsz < p_filesz (gABI: undefined; loader refuses).
    MemszLtFilesz,
    /// p_align set and p_vaddr not congruent to p_offset mod p_align.
    BadAlign,
    /// More PT_LOAD segments than MAX_SEGMENTS.
    TooManySegments,
    /// PT_INTERP present (loader policy: no dynamic images in this slice).
    HasInterp,
    /// e_entry outside every PT_LOAD's file-backed range (loader policy:
    /// never stage an image that would start in zeros or BSS).
    EntryOutsideLoad,
    /// No PT_LOAD at all.
    NoLoadSegments,
}

impl ElfError {
    pub fn name(self) -> &'static str {
        match self {
            ElfError::TooShort => "too-short",
            ElfError::BadMagic => "bad-magic",
            ElfError::NotElf64 => "not-elf64",
            ElfError::NotLittleEndian => "not-le",
            ElfError::BadVersion => "bad-version",
            ElfError::NotExec => "not-exec",
            ElfError::NotX86_64 => "not-x86-64",
            ElfError::BadPhentsize => "bad-phentsize",
            ElfError::NoPhdrs => "no-phdrs",
            ElfError::RangePastEof => "range-past-eof",
            ElfError::MemszLtFilesz => "memsz-lt-filesz",
            ElfError::BadAlign => "bad-align",
            ElfError::TooManySegments => "too-many-segments",
            ElfError::HasInterp => "has-interp",
            ElfError::EntryOutsideLoad => "entry-outside-load",
            ElfError::NoLoadSegments => "no-load-segments",
        }
    }
}

/// One staged PT_LOAD segment (evidence, not an executable mapping
/// description): where it landed, what its documented layout is.
#[derive(Debug, Clone, Copy)]
pub struct StagedSegment {
    /// Staged page base (window + i * SEGMENT_SLOT).
    pub page_base: u64,
    /// p_vaddr mod 4096, preserved by the staging rule.
    pub delta: u64,
    /// File-backed bytes copied from the image.
    pub filesz: u64,
    /// Bytes present after staging (file bytes + zero fill).
    pub memsz: u64,
    /// The image's own p_vaddr, carried for reconstruction.
    pub vaddr: u64,
    /// p_flags (PF_X=1, PF_W=2, PF_R=4).
    pub flags: u32,
}

/// The per-image parse result carried by `parse`: raw e_entry, the load
/// segments in ascending p_vaddr, and how many entries are live.
pub type ParseTables = [(usize, u64, u64, u64, u64, u32); MAX_SEGMENTS];

/// Result of a successful load: raw e_entry (never called here) and the
/// per-segment staging evidence.
pub struct LoadedImage {
    pub entry: u64,
    pub segments: [Option<StagedSegment>; MAX_SEGMENTS],
    pub used: usize,
}

fn u16_at(b: &[u8], off: usize) -> u16 {
    u16::from_le_bytes([b[off], b[off + 1]])
}

fn u32_at(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]])
}

fn u64_at(b: &[u8], off: usize) -> u64 {
    let mut t = [0u8; 8];
    t.copy_from_slice(&b[off..off + 8]);
    u64::from_le_bytes(t)
}

/// Validate the image and return (e_entry, [(ph index, p_vaddr, p_offset,
/// p_filesz, p_memsz, p_flags)] in ascending p_vaddr). No memory is
/// touched. Pure function of the buffer.
pub fn parse(image: &[u8]) -> Result<(u64, ParseTables, usize), ElfError> {
    if image.len() < 64 {
        return Err(ElfError::TooShort);
    }
    if image[0..4] != MAGIC {
        return Err(ElfError::BadMagic);
    }
    if image[OFF_EI_CLASS] != ELFCLASS64 {
        return Err(ElfError::NotElf64);
    }
    if image[OFF_EI_DATA] != ELFDATA2LSB {
        return Err(ElfError::NotLittleEndian);
    }
    if image[OFF_E_VERSION] != 1 || u32_at(image, OFF_E_VERSION32) != EV_CURRENT {
        return Err(ElfError::BadVersion);
    }
    let e_type = u16_at(image, OFF_E_TYPE);
    if e_type != ET_EXEC {
        return Err(ElfError::NotExec);
    }
    if u16_at(image, OFF_E_MACHINE) != EM_X86_64 {
        return Err(ElfError::NotX86_64);
    }
    let e_entry = u64_at(image, OFF_E_ENTRY);
    let e_phoff = u64_at(image, OFF_E_PHOFF);
    let e_phentsize = u16_at(image, OFF_E_PHENTSIZE) as usize;
    let e_phnum = u16_at(image, OFF_E_PHNUM) as usize;

    if e_phentsize != 56 {
        return Err(ElfError::BadPhentsize);
    }
    if e_phnum == 0 {
        return Err(ElfError::NoPhdrs);
    }
    // Table bounds: e_phoff >= 64 (inside/past the header) and the whole
    // table inside the buffer.
    let table_end = e_phoff.checked_add((e_phnum as u64) * 56);
    if e_phoff < 64 || table_end.is_none_or(|end| end as usize > image.len()) {
        return Err(ElfError::RangePastEof);
    }

    let mut loads = [(0usize, 0u64, 0u64, 0u64, 0u64, 0u32); MAX_SEGMENTS];
    let mut n_load = 0usize;
    let mut entry_ok = false;
    for i in 0..e_phnum {
        let off = e_phoff as usize + i * 56;
        let p_type = u32_at(image, off);
        if p_type == PT_INTERP {
            return Err(ElfError::HasInterp);
        }
        if p_type != PT_LOAD {
            continue;
        }
        let p_flags = u32_at(image, off + 4);
        let p_offset = u64_at(image, off + 8);
        let p_vaddr = u64_at(image, off + 16);
        let p_filesz = u64_at(image, off + 32);
        let p_memsz = u64_at(image, off + 40);
        let p_align = u64_at(image, off + 48);

        let file_end = p_offset.checked_add(p_filesz);
        if file_end.is_none_or(|end| end as usize > image.len()) {
            return Err(ElfError::RangePastEof);
        }
        if p_memsz < p_filesz {
            return Err(ElfError::MemszLtFilesz);
        }
        if p_align > 1 && (p_align & (p_align - 1)) != 0 {
            return Err(ElfError::BadAlign);
        }
        if p_align >= PAGE && p_vaddr % p_align != p_offset % p_align {
            return Err(ElfError::BadAlign);
        }
        if e_entry >= p_vaddr && e_entry < p_vaddr + p_filesz {
            entry_ok = true;
        }
        if n_load == MAX_SEGMENTS {
            return Err(ElfError::TooManySegments);
        }
        loads[n_load] = (i, p_vaddr, p_offset, p_filesz, p_memsz, p_flags);
        n_load += 1;
    }
    if n_load == 0 {
        return Err(ElfError::NoLoadSegments);
    }
    if !entry_ok {
        return Err(ElfError::EntryOutsideLoad);
    }
    // Sort by p_vaddr (insertion sort; n <= 8).
    for a in 1..n_load {
        let key = loads[a];
        let mut b = a;
        while b > 0 && loads[b - 1].1 > key.1 {
            loads[b] = loads[b - 1];
            b -= 1;
        }
        loads[b] = key;
    }
    Ok((e_entry, loads, n_load))
}

/// Stage a validated image: for each PT_LOAD (sorted by p_vaddr), map the
/// segment's slot page into the kernel-managed private area and copy file
/// bytes + zero fill. Returns the evidence table. The image buffer must
/// stay valid for the duration of the call (it is read, not retained).
pub fn load(image: &[u8]) -> Result<LoadedImage, ElfError> {
    let (entry, loads, n) = parse(image)?;
    let window = crate::paging::private_base() + LOAD_WINDOW;
    let mut out = LoadedImage {
        entry,
        segments: [None; MAX_SEGMENTS],
        used: n,
    };
    for (i, &(_, vaddr, foff, filesz, memsz, flags)) in loads.iter().enumerate().take(n) {
        let page_base = window + (i as u64) * SEGMENT_SLOT;
        let delta = vaddr % PAGE;
        // One 4 KiB page per segment slot is mapped (segments in this
        // slice's fixtures are < 1 page); larger segments would need
        // consecutive pages, handled by the same loop below.
        let pages = memsz.div_ceil(PAGE);
        for p in 0..pages {
            let frame = pmm::alloc_frame().expect("elf: out of frames staging segment");
            pmm::zero_frame(frame);
            paging::map_page(page_base + p * PAGE, frame);
        }
        // Copy via the just-created mappings. The direct map would also
        // reach the frames, but copying through the staged address proves
        // the mappings themselves work (readback in the gate does the same).
        let dest = page_base + delta;
        for (k, &byte) in image[foff as usize..(foff + filesz) as usize]
            .iter()
            .enumerate()
        {
            unsafe {
                ((dest as usize + k) as *mut u8).write_volatile(byte);
            }
        }
        // Zero fill p_memsz - p_filesz (BSS tail), volatile so it cannot
        // be elided even though nothing reads it back here (the gate does).
        for k in filesz..memsz {
            unsafe {
                ((dest as usize + k as usize) as *mut u8).write_volatile(0);
            }
        }
        out.segments[i] = Some(StagedSegment {
            page_base,
            delta,
            filesz,
            memsz,
            vaddr,
            flags,
        });
    }
    Ok(out)
}
