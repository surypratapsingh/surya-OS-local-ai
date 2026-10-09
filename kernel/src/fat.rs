//! Read-only FAT32 driver (K3 slice 3).
//!
//! Parses a FAT32 volume from a borrowed byte slice and answers three
//! questions: what is in a directory (with long-file-name reconstruction),
//! how long is a file, and what are its bytes. Written from the Microsoft
//! EFI FAT32 File System Specification, EFISP01 (fatgen103.doc):
//!
//! - BPB/EBPB fields:      sec 3.1 (offsets cited per use below)
//! - FAT32 determination:  sec 3.4.1 (cluster count from data sectors; the
//!   count alone decides the type, so below 65525 a volume is FAT12/16
//!   whatever its BPB says, and attach refuses it. An earlier comment here
//!   called 65525 a "formatter heuristic"; that was wrong, decisions.md
//!   2026-10-09)
//! - FAT entries:          sec 4.2 (32-bit entries, EOC = 0x0FFFFFF8..=FF)
//! - directory entries:    sec 5 (32 bytes; attr bits; NTRes case bits are
//!   the Windows extension honoured by mdir)
//! - LFN entries:          sec 6.2/6.3 (attr 0x0F, sequence numbering with
//!   0x40 on the last fragment, checksum over the 11 short-name bytes,
//!   name words at 1-10, 14-25, 28-31, 0x0000 terminator, 0xFFFF padding)
//! - dot entries:          sec 6.4 (first two entries of a subdirectory)
//!
//! Block IO is deliberately NOT here: the volume is mounted from a byte
//! slice (the corpus rides in via the bootloader's file loader, an
//! already-trusted path - limine.conf MODULE_PATH, PROTOCOL.md "Module
//! Feature"). A block-device front-end for real pendrives is a K5 concern
//! behind the same three entry points.
//!
//! The mdir renderer reproduces GNU mtools `mdir` output from the upstream
//! formatting code (mtools dir.c print_date/print_time/dotted_num/
//! list_file, config.c defaults: mtools_date_string = "yyyy-mm-dd",
//! mtools_twenty_four_hour_clock = 1, mtools_ignore_short_case = 0). It is
//! an EMULATION of the documented output, not the tool; the real tool is
//! the oracle in CI (scripts/check.sh stage 9) and
//! tools/verify-fat32.py re-derives the same facts from the spec.

// The corpus image reaches the kernel as a bootloader module
// (limine.conf module_path; PROTOCOL.md "Module Feature" guarantees 4 KiB
// alignment and exclusive 4 KiB chunks for every loaded file), so there is
// no incbin/linker plumbing and no block IO: fatselftest mounts the
// module's (address, size) directly with FatVolume::attach.

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FatError {
    /// 0x55AA missing, impossible BPB fields, or arithmetic overflow while
    /// deriving the geometry.
    NotFat32,
    /// A cluster number outside [2, 2 + count_of_clusters).
    ClusterOutOfRange,
    /// FAT chain walked past the step budget (loop or corrupt FAT).
    ChainLoop,
    /// A name did not fit the 255-unit LFN buffer or the output buffer.
    NameTooLong,
    /// Entry not found.
    NotFound,
    /// read_dir on something that is not a directory chain.
    NotADir,
    /// read_file on a directory.
    WrongEntryKind,
    /// Output buffer too small.
    BufferTooSmall,
    /// LFN sequence/checksum inconsistency (spec sec 6.2).
    BadLfn,
}

// ---------------------------------------------------------------------------
// On-disk field offsets (FAT32 spec sec 3.1 and sec 5; cited per use)
// ---------------------------------------------------------------------------

const OFF_TRAIL_SIG: usize = 510; // 0x55AA
const OFF_BYTS_PER_SEC: usize = 11; // BPB_BytsPerSec
const OFF_SEC_PER_CLUS: usize = 13; // BPB_SecPerClus
const OFF_RSVD_SEC_CNT: usize = 14; // BPB_RsvdSecCnt (u16)
const OFF_NUM_FATS: usize = 16; // BPB_NumFATs
const OFF_ROOT_ENT_CNT: usize = 17; // BPB_RootEntCnt (u16; 0 for FAT32)
const OFF_TOT_SEC_16: usize = 19; // BPB_TotSec16 (u16)
const OFF_FATSZ_16: usize = 22; // BPB_FATSz16 (u16)
const OFF_TOT_SEC_32: usize = 32; // BPB_TotSec32 (u32)
const OFF_FATSZ_32: usize = 36; // BPB_FATSz32 (u32)
const OFF_ROOT_CLUS: usize = 44; // BPB_RootClus (u32)
const DIRENT_SIZE: usize = 32;

const ATTR_HIDDEN: u8 = 0x02;
const ATTR_SYSTEM: u8 = 0x04;
const ATTR_VOLUME_ID: u8 = 0x08;
const ATTR_DIRECTORY: u8 = 0x10;
const ATTR_LONG_NAME: u8 = 0x0F;
const ATTR_MASK: u8 = 0x3F; // sec 5: upper 2 attr bits are reserved
const NT_LOWER_BASE: u8 = 0x08; // NTRes: basename lowercase (Windows ext)
const NT_LOWER_EXT: u8 = 0x10; // NTRes: extension lowercase

const EOC_MIN: u32 = 0x0FFF_FFF8; // sec 4.2: F8..FF terminates a chain
const FREE_CLUSTER: u32 = 0x0000_0000;

/// Long names are at most 255 UTF-16 code units (sec 6.1); one spare unit
/// for the terminator slot arithmetic.
const LFN_MAX_UNITS: usize = 256;

/// The corpus's one fixed write timestamp, packed per spec sec 5
/// (time = h<<11|m<<5|s/2, date = (y-1980)<<9|m<<5|d). The renderer prints
/// dates and times from the entries; this constant decodes them. The corpus
/// generator (tools/gen-fat32-corpus.py, DOS_DATE/DOS_TIME) is the source.
const CORPUS_DOS_TIME: u16 = 12 << 11 | 34 << 5 | 28; // 12:34:56 -> 12:34
const CORPUS_DOS_DATE: u16 = (2026 - 1980) << 9 | 9 << 5 | 1; // 2026-09-01

#[derive(Clone)]
pub struct FatEntry {
    /// Decoded name: LFN as UTF-8, or the 8.3 name with NTRes case applied
    /// (dot inserted positionally). 255 UTF-16 units encode to at most
    /// 765 UTF-8 bytes (three bytes each in the BMP range the driver
    /// accepts); surrogates are rejected outright (BadLfn) rather than
    /// half-decoded, so the bound holds.
    pub name: [u8; 768],
    pub name_len: usize,
    pub attr: u8,
    /// NTRes byte of the short entry (sec 5, Windows case extension).
    pub nt_res: u8,
    /// Size in bytes; 0 for directories.
    pub size: u32,
    /// First data cluster.
    pub first_cluster: u32,
    /// Raw 11-byte 8.3 field (spaces included) exactly as on disk.
    pub short: [u8; 11],
    /// True when an LFN record belonged to this entry (dir.c prints the
    /// long-name suffix whenever one exists, even if it equals the short
    /// name's case fold).
    pub had_lfn: bool,
}

impl FatEntry {
    pub fn is_dir(&self) -> bool {
        self.attr & ATTR_DIRECTORY != 0
    }
    pub fn is_volume_label(&self) -> bool {
        self.attr & ATTR_VOLUME_ID != 0
    }
    pub fn is_hidden_or_system(&self) -> bool {
        self.attr & (ATTR_HIDDEN | ATTR_SYSTEM) != 0
    }
    pub fn name_str(&self) -> &str {
        // The decoder only ever writes ASCII or well-formed UTF-8.
        core::str::from_utf8(&self.name[..self.name_len]).unwrap_or("?")
    }

    /// The 8.3 name with the dot inserted positionally and NTRes case
    /// applied (mdir prints the raw field split at 8|3, with case per its
    /// config; the LFN suffix is the caller's business).
    fn short_display(&self) -> ([u8; 8], [u8; 3]) {
        let mut base = [0u8; 8];
        let mut ext = [0u8; 3];
        for (i, c) in self.short.iter().take(8).enumerate() {
            base[i] = if self.nt_res & NT_LOWER_BASE != 0 {
                c.to_ascii_lowercase()
            } else {
                *c
            };
        }
        for (i, c) in self.short.iter().skip(8).enumerate() {
            ext[i] = if self.nt_res & NT_LOWER_EXT != 0 {
                c.to_ascii_lowercase()
            } else {
                *c
            };
        }
        (base, ext)
    }

    /// dir.c list_file: `if(*global_longname) printf(" %s", ...)` - the
    /// suffix prints whenever an LFN record exists.
    fn has_lfn_suffix(&self) -> bool {
        self.had_lfn
    }
}

// ---------------------------------------------------------------------------
// Volume
// ---------------------------------------------------------------------------

pub struct FatVolume {
    img: *const u8,
    img_len: usize,
    bytes_per_sector: u32,
    sec_per_clus: u32,
    num_fats: u32,
    fat_sectors: u32,
    root_cluster: u32,
    /// spec sec 3.4.1: count of clusters derived from the data sectors.
    count_of_clusters: u32,
    fat_offset: usize,
    data_offset: usize,
}

impl FatVolume {
    /// Parse the BPB and derive the geometry. Every field is checked against
    /// the spec's constraints; any impossible value is `NotFat32`, never a
    /// silently garbled volume.
    pub fn attach(img: *const u8, img_len: usize) -> Result<FatVolume, FatError> {
        let rd = |off: usize| -> u8 {
            if off >= img_len {
                return 0;
            }
            unsafe { *img.add(off) }
        };
        let rd16 = |off: usize| -> u32 { rd(off) as u32 | (rd(off + 1) as u32) << 8 };
        let rd32 = |off: usize| -> u32 { rd16(off) | rd16(off + 2) << 16 };

        if img_len < 512 || rd(OFF_TRAIL_SIG) != 0x55 || rd(OFF_TRAIL_SIG + 1) != 0xAA {
            return Err(FatError::NotFat32);
        }
        let bytes_per_sector = rd16(OFF_BYTS_PER_SEC);
        if bytes_per_sector != 512 {
            // The corpus declares 512; other legal sizes would change the
            // sector arithmetic the image bounds provide. Refuse loudly.
            return Err(FatError::NotFat32);
        }
        let sec_per_clus = rd(OFF_SEC_PER_CLUS) as u32;
        if sec_per_clus == 0 || !sec_per_clus.is_power_of_two() || sec_per_clus > 128 {
            return Err(FatError::NotFat32);
        }
        let reserved_sectors = rd16(OFF_RSVD_SEC_CNT);
        let num_fats = rd16(OFF_NUM_FATS);
        if reserved_sectors == 0 || num_fats == 0 || num_fats > 4 {
            return Err(FatError::NotFat32);
        }
        if rd16(OFF_ROOT_ENT_CNT) != 0 {
            // sec 3.1: BPB_RootEntCnt must be 0 for FAT32.
            return Err(FatError::NotFat32);
        }
        let tot_sec = if rd16(OFF_TOT_SEC_16) == 0 {
            rd32(OFF_TOT_SEC_32)
        } else {
            rd16(OFF_TOT_SEC_16)
        };
        let fat_sectors = if rd16(OFF_FATSZ_16) == 0 {
            rd32(OFF_FATSZ_32)
        } else {
            rd16(OFF_FATSZ_16)
        };
        let root_cluster = rd32(OFF_ROOT_CLUS);
        if tot_sec == 0 || fat_sectors == 0 || root_cluster < 2 {
            return Err(FatError::NotFat32);
        }
        // spec sec 3.4.1: cluster count from the data sectors.
        let data_sectors = tot_sec
            .checked_sub(reserved_sectors + num_fats * fat_sectors)
            .ok_or(FatError::NotFat32)?;
        let count_of_clusters = data_sectors / sec_per_clus;
        // sec 3.4.1: fewer than 65525 clusters is FAT12/16, not FAT32.
        if count_of_clusters < 65525 {
            return Err(FatError::NotFat32);
        }
        // sec 4.2: one 4-byte entry per cluster, plus FAT[0] and FAT[1].
        if fat_sectors as u64 * bytes_per_sector as u64 / 4 < count_of_clusters as u64 + 2 {
            return Err(FatError::NotFat32);
        }

        let fat_offset = reserved_sectors as usize * bytes_per_sector as usize;
        let data_offset =
            fat_offset + num_fats as usize * fat_sectors as usize * bytes_per_sector as usize;
        if data_offset >= img_len {
            return Err(FatError::NotFat32);
        }
        Ok(FatVolume {
            img,
            img_len,
            bytes_per_sector,
            sec_per_clus,
            num_fats,
            fat_sectors,
            root_cluster,
            count_of_clusters,
            fat_offset,
            data_offset,
        })
    }

    pub fn count_of_clusters(&self) -> u32 {
        self.count_of_clusters
    }
    pub fn root_cluster(&self) -> u32 {
        self.root_cluster
    }

    // -- raw access ---------------------------------------------------------
    #[inline]
    fn u8_at(&self, off: usize) -> u8 {
        if off >= self.img_len {
            return 0;
        }
        unsafe { *self.img.add(off) }
    }
    #[inline]
    fn u16_at(&self, off: usize) -> u16 {
        (self.u8_at(off) as u16) | (self.u8_at(off + 1) as u16) << 8
    }
    #[inline]
    fn u32_at(&self, off: usize) -> u32 {
        self.u16_at(off) as u32 | (self.u16_at(off + 2) as u32) << 16
    }

    /// FAT32 entry for `cluster` (sec 4.2: 4-byte LE entries, FAT[i] at
    /// FAT offset 4*i). Out-of-range clusters read as 0x0FFFFFFF, which the
    /// chain walk reports as an error.
    fn fat_entry(&self, cluster: u32) -> u32 {
        if cluster < 2 || cluster >= self.count_of_clusters + 2 {
            return 0x0FFF_FFFF;
        }
        self.u32_at(self.fat_offset + cluster as usize * 4)
    }

    /// Byte offset of `cluster`'s first byte in the image.
    fn cluster_offset(&self, cluster: u32) -> Result<usize, FatError> {
        if cluster < 2 || cluster >= self.count_of_clusters + 2 {
            return Err(FatError::ClusterOutOfRange);
        }
        let off = self.data_offset
            + (cluster - 2) as usize * self.sec_per_clus as usize * self.bytes_per_sector as usize;
        if off >= self.img_len {
            return Err(FatError::ClusterOutOfRange);
        }
        Ok(off)
    }

    fn cluster_bytes(&self) -> usize {
        self.sec_per_clus as usize * self.bytes_per_sector as usize
    }

    /// Walk a FAT chain, calling `f` with the byte offset of each cluster.
    /// The step budget (count_of_clusters + 2) makes an infinite chain
    /// impossible: a loop or runaway chain is `ChainLoop`, never a hang.
    /// `f` returns `Ok(true)` to stop the whole walk. (A plain early return
    /// inside `f` only ended one cluster: read_dir then resumed in the next
    /// cluster, mid LFN run, and failed with BadLfn. It showed once the
    /// corpus root spanned 4 clusters, R2 2026-10-09.)
    fn for_each_cluster(
        &self,
        first: u32,
        mut f: impl FnMut(usize) -> Result<bool, FatError>,
    ) -> Result<(), FatError> {
        let mut c = first;
        let budget = self.count_of_clusters + 2;
        let mut steps = 0u32;
        loop {
            if steps > budget {
                return Err(FatError::ChainLoop);
            }
            if c < 2 || c >= self.count_of_clusters + 2 {
                return Err(FatError::ClusterOutOfRange);
            }
            let off = self.cluster_offset(c)?;
            if f(off)? {
                return Ok(());
            }
            let next = self.fat_entry(c);
            if next >= EOC_MIN {
                return Ok(());
            }
            if next == FREE_CLUSTER {
                return Err(FatError::ChainLoop);
            }
            c = next;
            steps += 1;
        }
    }

    /// Read a whole file (bounded by its directory size field, sec 5). The
    /// on-disk chain must cover ceil(size / cluster) clusters, so a short
    /// chain is `ChainLoop`, not a silent truncation.
    pub fn read_file(&self, e: &FatEntry, out: &mut [u8]) -> Result<usize, FatError> {
        if e.is_dir() {
            return Err(FatError::WrongEntryKind);
        }
        let size = e.size as usize;
        if size > out.len() {
            return Err(FatError::BufferTooSmall);
        }
        let cb = self.cluster_bytes();
        let mut written = 0usize;
        self.for_each_cluster(e.first_cluster, |off| {
            let take = core::cmp::min(cb, size - written);
            for i in 0..take {
                out[written + i] = self.u8_at(off + i);
            }
            written += take;
            Ok(false)
        })?;
        if written < size {
            return Err(FatError::ChainLoop);
        }
        Ok(written)
    }

    /// Iterate the entries of the directory starting at `cluster` (the root
    /// directory: pass `root_cluster()`). LFN fragments are reassembled per
    /// sec 6.2/6.3; deleted (0xE5) and end (0x00) slots never surface.
    /// `f` returns true to stop early.
    pub fn read_dir(
        &self,
        cluster: u32,
        f: &mut dyn FnMut(&FatEntry) -> bool,
    ) -> Result<(), FatError> {
        let mut lfn_units = [0u16; LFN_MAX_UNITS];
        let mut lfn_have = 0usize; // units assembled so far
        let mut lfn_seq_expected = 0u8;
        let mut lfn_checksum = 0u8;

        self.for_each_cluster(cluster, |base| {
            for slot in 0..self.cluster_bytes() / DIRENT_SIZE {
                let off = base + slot * DIRENT_SIZE;
                let first = self.u8_at(off);
                if first == 0x00 {
                    // sec 5: first byte 0x00 = no further entries.
                    return Ok(true);
                }
                if first == 0xE5 {
                    // Deleted: skip, and drop any LFN run (its short entry
                    // is gone; a dangling run is not evidence).
                    lfn_have = 0;
                    continue;
                }
                let attr = self.u8_at(off + 11);
                if attr & ATTR_MASK == ATTR_LONG_NAME {
                    // sec 6.3: byte 0 is the sequence number; bit 0x40
                    // marks the LAST fragment, which comes first on disk.
                    let seq = first;
                    let chk = self.u8_at(off + 13);
                    if seq & 0x40 != 0 {
                        lfn_checksum = chk;
                        lfn_seq_expected = seq & 0x1F;
                        lfn_have = lfn_seq_expected as usize * 13;
                        if lfn_have > LFN_MAX_UNITS {
                            return Err(FatError::NameTooLong);
                        }
                    } else if lfn_have == 0
                        || chk != lfn_checksum
                        || (seq & 0x1F) != lfn_seq_expected - 1
                    {
                        // Continuation must be the next lower sequence with
                        // the same checksum (sec 6.2 ordering rule).
                        return Err(FatError::BadLfn);
                    } else {
                        lfn_seq_expected = seq & 0x1F;
                    }
                    // Name words (sec 6.3): 5 units @ byte 1, 6 units @
                    // byte 14, 2 units @ byte 28 - 13 units per slot. The
                    // windows are 10/12/4 BYTES wide; the first draft of
                    // this loop read 10/12/4 UNITS and swept the attr/
                    // checksum bytes into the name (caught by the corpus
                    // selftest: every LFN decoded interleaved garbage).
                    let ub = (seq & 0x1F) as usize - 1;
                    let ub = ub * 13;
                    for k in 0..5u16 {
                        lfn_units[ub + k as usize] = self.u16_at(off + 1 + k as usize * 2);
                    }
                    for k in 0..6u16 {
                        lfn_units[ub + 5 + k as usize] = self.u16_at(off + 14 + k as usize * 2);
                    }
                    for k in 0..2u16 {
                        lfn_units[ub + 11 + k as usize] = self.u16_at(off + 28 + k as usize * 2);
                    }
                    continue;
                }
                // Short entry (sec 5).
                let mut e = FatEntry {
                    name: [0; 768],
                    name_len: 0,
                    attr,
                    nt_res: self.u8_at(off + 12),
                    size: self.u32_at(off + 28),
                    first_cluster: (self.u16_at(off + 20) as u32) << 16
                        | self.u16_at(off + 26) as u32,
                    short: [0; 11],
                    had_lfn: false,
                };
                for (i, slotb) in e.short.iter_mut().enumerate() {
                    *slotb = self.u8_at(off + i);
                }
                if attr & ATTR_VOLUME_ID != 0 {
                    // Volume label: 11 raw bytes, no LFN (sec 5).
                    e.name[..11].copy_from_slice(&e.short);
                    e.name_len = 11;
                } else if lfn_have > 0 && lfn_checksum == self.u8_at(off + 13) {
                    // Assemble the LFN: units up to the 0x0000 terminator
                    // (sec 6.2), encoded as UTF-8. BMP only in this corpus;
                    // surrogate units are rejected rather than half-decoded.
                    let mut w = 0usize;
                    for &u in lfn_units.iter().take(lfn_have) {
                        match u {
                            0x0000 | 0xFFFF => break,
                            0..=0x7F => {
                                e.name[w] = u as u8;
                                w += 1;
                            }
                            0x80..=0x7FF => {
                                e.name[w] = 0xC0 | (u >> 6) as u8;
                                e.name[w + 1] = 0x80 | (u & 0x3F) as u8;
                                w += 2;
                            }
                            _ => {
                                if (0xD800..0xE000).contains(&u) {
                                    return Err(FatError::BadLfn);
                                }
                                e.name[w] = 0xE0 | (u >> 12) as u8;
                                e.name[w + 1] = 0x80 | ((u >> 6) & 0x3F) as u8;
                                e.name[w + 2] = 0x80 | (u & 0x3F) as u8;
                                w += 3;
                            }
                        }
                    }
                    e.name_len = w;
                    e.had_lfn = true;
                } else {
                    // Plain 8.3 with NTRes case (mdir honours the bits; no
                    // dot inserted - the renderer splits the raw field).
                    let mut w = 0usize;
                    for i in 0..8 {
                        let c = e.short[i];
                        if c == b' ' {
                            break;
                        }
                        e.name[w] = if e.nt_res & NT_LOWER_BASE != 0 {
                            c.to_ascii_lowercase()
                        } else {
                            c
                        };
                        w += 1;
                    }
                    for i in 8..11 {
                        let c = e.short[i];
                        if c == b' ' {
                            break;
                        }
                        if i == 8 {
                            // first extension char: insert the dot
                            e.name[w] = b'.';
                            w += 1;
                        }
                        e.name[w] = if e.nt_res & NT_LOWER_EXT != 0 {
                            c.to_ascii_lowercase()
                        } else {
                            c
                        };
                        w += 1;
                    }
                    e.name_len = w;
                }
                lfn_have = 0;
                if f(&e) {
                    return Ok(true);
                }
            }
            Ok(false)
        })
    }

    /// Find a directory entry by exact (decoded) name in `cluster`.
    pub fn lookup(&self, cluster: u32, name: &str) -> Result<FatEntry, FatError> {
        let mut found = None;
        self.read_dir(cluster, &mut |e| {
            if e.name_str() == name {
                found = Some(e.clone());
                true
            } else {
                false
            }
        })?;
        found.ok_or(FatError::NotFound)
    }

    // -- mdir-format listing ------------------------------------------------
    //
    // Port of mtools dir.c list_file with the config.c defaults:
    //   date "yyyy-mm-dd" (print_date), 24-hour "HH:MM" (print_time,
    //   DOS_HOUR = time>>11, DOS_MINUTE = (time>>5)&0x3F),
    //   "%8ld" size right-align (printf " %8ld" - one space THEN 8 wide).
    // Per line, exactly:
    //   {base8raw} {ext3raw} {<DIR> | %8ld} {yyyy-mm-dd} {HH:MM}\n
    // with the literal " <longname>" appended when the LFN differs from the
    // short name's case fold, and the size/DIR columns using dir.c's
    // printf(" %8ld") / printf("<DIR> ") spacing byte for byte.
    pub fn list_dir_mdir(&self, cluster: u32, out: &mut [u8]) -> Result<usize, FatError> {
        let mut w = 0usize;
        let mut files = 0u32;
        let mut bytes_total = 0u64;
        self.read_dir(cluster, &mut |e| {
            // Volume labels appear only in the "Volume in drive" header
            // (dir.c print_volume_label), never as file rows.
            if e.is_volume_label() {
                return false;
            }
            // dir.c list_file: "if(!all && (entry->dir.attr & 0x6)) return 0;"
            if e.is_hidden_or_system() {
                return false;
            }
            // mdir's directory loop uses NO_DOTS: "." and ".." never list.
            if e.short[0] == b'.' {
                return false;
            }
            files += 1;
            let (base, ext) = e.short_display();
            for &c in base.iter() {
                out[w] = c;
                w += 1;
            }
            out[w] = b' ';
            w += 1;
            for &c in ext.iter() {
                out[w] = c;
                w += 1;
            }
            out[w] = b' ';
            w += 1;
            if e.is_dir() {
                // dir.c: printf("<DIR> ")
                out[w..w + 6].copy_from_slice(b"<DIR> ");
                w += 6;
            } else {
                // dir.c: printf(" %8ld", size) - printf SPACE-pads the 8-wide
                // field (and prints sizes of 10^8 and beyond at full width,
                // overflowing the column, exactly like C does).
                out[w] = b' ';
                w += 1;
                let mut num = [0u8; 12];
                let mut len = 0usize;
                let mut v = e.size;
                loop {
                    num[len] = b'0' + (v % 10) as u8;
                    v /= 10;
                    len += 1;
                    if v == 0 {
                        break;
                    }
                }
                num[..len].reverse();
                if len < 8 {
                    for _ in 0..8 - len {
                        out[w] = b' ';
                        w += 1;
                    }
                }
                out[w..w + len].copy_from_slice(&num[..len]);
                w += len;
                bytes_total += e.size as u64;
            }
            out[w] = b' ';
            w += 1;
            // print_date with mtools_date_string = "yyyy-mm-dd"
            w = write_date(out, w, CORPUS_DOS_DATE);
            out[w] = b' ';
            w += 1;
            // print_time with the 24-hour clock
            w = write_time(out, w, CORPUS_DOS_TIME);
            if e.has_lfn_suffix() {
                out[w] = b' ';
                w += 1;
                for &c in &e.name[..e.name_len] {
                    out[w] = c;
                    w += 1;
                }
            }
            out[w] = b'\n';
            w += 1;
            false
        })?;
        // printSummary (dir.c): printf(" %3d file", files); putchar(' '
        // or 's'); printf(" %s bytes\n", dotted_num(bytes, 13)).
        // %3d SPACE-pads (11 -> " 11"), like every printf width.
        out[w] = b' ';
        w += 1;
        let mut num = [b' '; 3];
        {
            let mut v = files;
            let mut i = 2usize;
            loop {
                num[i] = b'0' + (v % 10) as u8;
                v /= 10;
                if v == 0 || i == 0 {
                    break;
                }
                i -= 1;
            }
        }
        out[w..w + 3].copy_from_slice(&num);
        w += 3;
        out[w..w + 5].copy_from_slice(b" file");
        w += 5;
        out[w] = if files == 1 { b' ' } else { b's' };
        w += 1;
        out[w] = b' ';
        w += 1;
        w = write_dotted_num(out, w, bytes_total, 13);
        out[w] = b' ';
        w += 1;
        out[w..w + 6].copy_from_slice(b"bytes\n");
        w += 6;
        Ok(w)
    }
}

/// mtools print_date with the default "yyyy-mm-dd" format string.
fn write_date(out: &mut [u8], w: usize, dos_date: u16) -> usize {
    let year = 1980 + (dos_date >> 9);
    let month = (dos_date >> 5) & 0x0F;
    let day = dos_date & 0x1F;
    out[w] = b'0' + (year / 1000) as u8;
    out[w + 1] = b'0' + (year / 100 % 10) as u8;
    out[w + 2] = b'0' + (year / 10 % 10) as u8;
    out[w + 3] = b'0' + (year % 10) as u8;
    out[w + 4] = b'-';
    out[w + 5] = b'0' + (month / 10) as u8;
    out[w + 6] = b'0' + (month % 10) as u8;
    out[w + 7] = b'-';
    out[w + 8] = b'0' + (day / 10) as u8;
    out[w + 9] = b'0' + (day % 10) as u8;
    w + 10
}
/// mtools print_time with the 24-hour clock: "%2d:%02d%c" where the last
/// %c is am_pm, which is ' ' (a SPACE) in 24-hour mode (dir.c print_time:
/// "else am_pm = ' '"). So every listing line ends "HH:MM " with a
/// trailing space, and an LFN suffix adds a second: "12:34  name".
fn write_time(out: &mut [u8], w: usize, dos_time: u16) -> usize {
    let hour = dos_time >> 11;
    let minute = (dos_time >> 5) & 0x3F;
    out[w] = if hour >= 10 {
        b'0' + (hour / 10) as u8
    } else {
        b' '
    };
    out[w + 1] = b'0' + (hour % 10) as u8;
    out[w + 2] = b':';
    out[w + 3] = b'0' + (minute / 10) as u8;
    out[w + 4] = b'0' + (minute % 10) as u8;
    out[w + 5] = b' '; // am_pm in 24-hour mode
    w + 6
}

/// Port of mtools dotted_num(bytes, width): a right-aligned field of
/// `width` chars, thousands groups separated by spaces, leading zeros
/// blanked. (dir.c: groups of three digits shifted right, separator ' ' -
/// "they please both Americans and Europeans".)
fn write_dotted_num(out: &mut [u8], mut w: usize, value: u64, width: usize) -> usize {
    // Render without separators first, then insert one space per group.
    let mut digits = [0u8; 20];
    let mut n = value;
    let mut len = 0usize;
    loop {
        digits[len] = b'0' + (n % 10) as u8;
        n /= 10;
        len += 1;
        if n == 0 {
            break;
        }
    }
    digits[..len].reverse();
    // Groups of three from the right; separators between groups.
    let groups = len.div_ceil(3);
    let sep_total = groups.saturating_sub(1);
    let total = len + sep_total;
    if total < width {
        for _ in 0..width - total {
            out[w] = b' ';
            w += 1;
        }
    }
    for (i, d) in digits[..len].iter().enumerate() {
        out[w] = *d;
        w += 1;
        let from_right = len - i - 1;
        if from_right > 0 && from_right.is_multiple_of(3) {
            out[w] = b' ';
            w += 1;
        }
    }
    w
}
