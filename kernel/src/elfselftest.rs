//! K4b ELF64 loader gate (elf gate).
//!
//! Oracle layering (AGENTS.md rule 1 — no check compares the loader to
//! itself):
//!
//! - Hand golden tables: SEG0/SEG1 and the byte counts come from the
//!   DOCUMENTED fixture layout in tools/gen-elf64-fixture.py's header and
//!   the gABI field offsets, written out by hand — not from elf.rs.
//! - The negative table names, for every fixture in
//!   kernel/fixtures/negative/, the exact ElfError variant it must produce;
//!   a generic "any error" would let a bad-magic image be misreported as
//!   memsz-lt-filesz and still pass.
//! - The staged-bytes readback is diffed against a DIGEST COMMITTED FROM
//!   THE HOST ORACLE (tools/verify-elf64.py's own parse of the fixture);
//!   the gate recomputes the digest with its OWN sha256 (FIPS 180-4
//!   compression written from the standard, shared with no other module),
//!   so neither side derives the expected value from the other's code.
//! - One done-when evidence line: staged layout preserves intra-image
//!   layout as page deltas (seg1 delta = p_vaddr low bits, bases differ by
//!   exactly SEGMENT_SLOT).
//!
//! The image bytes are embedded at build time from the committed fixtures
//! (include_bytes!), so the gate and the host oracle read the SAME bytes
//! that tools/test-elf64.sh hands around.

use crate::elf::{self, ElfError};
use crate::paging;

static mut PASS: u32 = 0;
static mut FAIL: u32 = 0;

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

/// no_std formatting: a fixed stack buffer + core::fmt::Write, exposed as
/// a macro so the buffer lives at the call site while `check` runs.
struct BufWriter<'a> {
    buf: &'a mut [u8],
    n: usize,
}

impl core::fmt::Write for BufWriter<'_> {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        for &b in s.as_bytes() {
            if self.n < self.buf.len() {
                self.buf[self.n] = b;
                self.n += 1;
            }
        }
        Ok(())
    }
}

/// check with an already-built detail string.
fn check_buf(ok: bool, name: &str, detail: &[u8], dlen: usize) {
    let d = core::str::from_utf8(&detail[..dlen]).unwrap_or("?");
    check(ok, name, d);
}

macro_rules! checkf {
    ($ok:expr, $name:expr, $($arg:tt)*) => {{
        let ok: bool = $ok;
        let mut buf = [0u8; 160];
        let n;
        {
            let mut w = BufWriter { buf: &mut buf, n: 0 };
            use core::fmt::Write as _;
            let _ = core::write!(w, $($arg)*);
            n = w.n;
        }
        check_buf(ok, $name, &buf, n);
    }};
}

// ---------------------------------------------------------------------------
// Hand golden tables (from the documented layout + gABI, not from elf.rs)
// ---------------------------------------------------------------------------

/// Documented layout (tools/gen-elf64-fixture.py header): two PT_LOADs,
/// segment 0 R+X at p_vaddr 0x400100 (filesz 0x8, delta 0x100), segment 1
/// R+W at p_vaddr 0x400108 (filesz 0x10, memsz 0x18, delta 0x108); entry
/// 0x400100 sits in the R+X segment. Both p_align are 0x1000 and congruent
/// to their p_offset (0x100, 0x108) per the gABI congruence rule.
const SEG0_VADDR: u64 = 0x400100;
const SEG0_FILESZ: u64 = 0x8;
const SEG1_VADDR: u64 = 0x400108;
const SEG1_FILESZ: u64 = 0x10;
const SEG1_MEMSZ: u64 = 0x18;
const SEG1_DELTA: u64 = 0x108;
const ENTRY: u64 = 0x400100;

/// Code payload at file offset 0x100 (8 bytes), documented pattern; the
/// R+X segment is exactly this payload (p_offset 0x100, filesz 0x8).
const CODE_FIRST: u8 = 0x10;
const CODE_LAST: u8 = 0x17;
/// Data payload at file offset 0x108 (16 bytes), documented pattern; the
/// R+W segment is this payload plus an 8-byte BSS tail (memsz 0x18).
const DATA_FIRST: u8 = 0xA0;
const DATA_LAST: u8 = 0xAF;

/// (fixture name, expected error). Every negative fixture must be refused
/// with exactly this variant.
const NEGATIVE_EXPECT: &[(&str, ElfError)] = &[
    ("bad-magic", ElfError::BadMagic),
    ("ei-class32", ElfError::NotElf64),
    ("ei-data-be", ElfError::NotLittleEndian),
    ("et-dyn", ElfError::NotExec),
    ("phnum-zero", ElfError::NoPhdrs),
    ("phentsize-wrong", ElfError::BadPhentsize),
    ("memsz-lt-filesz", ElfError::MemszLtFilesz),
    ("offset-oob", ElfError::RangePastEof),
    ("interp-present", ElfError::HasInterp),
    ("entry-oob", ElfError::EntryOutsideLoad),
];

/// Host-oracle digest over the staged region (seg0 0x8 bytes + seg1 0x18
/// bytes, concatenated in segment order), committed from
/// tools/verify-elf64.py's run against the committed fixture. Re-derived
/// by tools/test-elf64.sh on every run; this constant is CHECKED, not
/// trusted: if the fixture or the oracle's rule drifts, the harness fails
/// before the kernel gate can agree with a stale value.
const STAGED_SHA256: &str = "7feb498a250f5c0c4c8e867d1f56e202e0190d40d230fad3512eb3943e406ff3";
const STAGED_LEN: u64 = 0x8 + 0x18;

// ---------------------------------------------------------------------------
// The gate's own sha256 (FIPS 180-4; independent implementation)
// ---------------------------------------------------------------------------

mod tinysha {
    const K: [u32; 64] = [
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
        0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
        0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
        0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
        0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
        0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
        0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
        0xc67178f2,
    ];

    /// One compression step over a 64-byte block (FIPS 180-4 §6.2.2).
    fn compress(h: &mut [u32; 8], block: &[u8]) {
        let mut w = [0u32; 64];
        for (i, wc) in block.chunks(4).enumerate() {
            w[i] = u32::from_be_bytes([wc[0], wc[1], wc[2], wc[3]]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let (mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh) =
            (h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7]);
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ (!e & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        h[0] = h[0].wrapping_add(a);
        h[1] = h[1].wrapping_add(b);
        h[2] = h[2].wrapping_add(c);
        h[3] = h[3].wrapping_add(d);
        h[4] = h[4].wrapping_add(e);
        h[5] = h[5].wrapping_add(f);
        h[6] = h[6].wrapping_add(g);
        h[7] = h[7].wrapping_add(hh);
    }

    /// sha256 over a short buffer (staged regions here are < 4 KiB, so the
    /// message and its padding stream through a fixed 128-byte block pair;
    /// two blocks cover up to 119 message bytes, the gate digests <= 40).
    pub fn digest(data: &[u8]) -> [u8; 32] {
        debug_assert!(data.len() <= 119, "tinysha: fixed-pad bound");
        let mut blocks = [0u8; 128];
        blocks[..data.len()].copy_from_slice(data);
        blocks[data.len()] = 0x80;
        let bitlen = ((data.len() as u64) * 8).to_be_bytes();
        // Total length L: pad to 56 mod 64, then 8 length bytes.
        let total = if data.len() + 1 + 8 <= 64 { 64 } else { 128 };
        blocks[total - 8..total].copy_from_slice(&bitlen);
        let mut h: [u32; 8] = [
            0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
            0x5be0cd19,
        ];
        compress(&mut h, &blocks[..64]);
        if total == 128 {
            compress(&mut h, &blocks[64..128]);
        }
        let mut out = [0u8; 32];
        for (i, v) in h.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&v.to_be_bytes());
        }
        out
    }
}

fn hex(bytes: &[u8]) -> ([u8; 64], usize) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = [0u8; 64];
    for (i, &b) in bytes.iter().enumerate() {
        out[i * 2] = HEX[(b >> 4) as usize];
        out[i * 2 + 1] = HEX[(b & 0xF) as usize];
    }
    (out, bytes.len() * 2)
}

/// Read the staged region (seg0 bytes then seg1 bytes, in segment order,
/// at their staged deltas) and hash it — the same region definition the
/// host oracle hashes.
fn staged_digest(seg0: &elf::StagedSegment, seg1: &elf::StagedSegment) -> ([u8; 32], u64) {
    let mut buf = [0u8; 128];
    let mut n = 0usize;
    let d0 = seg0.page_base + seg0.delta;
    let d1 = seg1.page_base + seg1.delta;
    for k in 0..seg0.memsz as usize {
        buf[n] = unsafe { ((d0 as usize + k) as *const u8).read_volatile() };
        n += 1;
    }
    for k in 0..seg1.memsz as usize {
        buf[n] = unsafe { ((d1 as usize + k) as *const u8).read_volatile() };
        n += 1;
    }
    (tinysha::digest(&buf[..n]), n as u64)
}

pub fn run() {
    sprintln!("elf gate: ELF64 loader (K4b)");
    let mini: &[u8] = include_bytes!("../fixtures/elf64-mini");

    // -- parse golden tables -------------------------------------------------
    match elf::parse(mini) {
        Ok((entry, segs, n)) => {
            checkf!(
                entry == ENTRY,
                "parse: e_entry",
                "got {:#x}, want {:#x}",
                entry,
                ENTRY
            );
            checkf!(n == 2, "parse: two PT_LOAD segments", "got {}", n);
            let (_, v0, _, f0, m0, fl0) = segs[0];
            checkf!(
                v0 == SEG0_VADDR && f0 == SEG0_FILESZ && m0 == SEG0_FILESZ,
                "parse: seg0 vaddr/filesz/memsz match the documented layout",
                "got {:#x}/{:#x}/{:#x}",
                v0,
                f0,
                m0
            );
            checkf!(fl0 == 5, "parse: seg0 flags R+X (5)", "got {}", fl0);
            let (_, v1, _, f1, m1, fl1) = segs[1];
            checkf!(
                v1 == SEG1_VADDR && f1 == SEG1_FILESZ && m1 == SEG1_MEMSZ,
                "parse: seg1 vaddr/filesz/memsz match the documented layout",
                "got {:#x}/{:#x}/{:#x}",
                v1,
                f1,
                m1
            );
            checkf!(fl1 == 6, "parse: seg1 flags R+W (6)", "got {}", fl1);
        }
        Err(e) => check(false, "parse: valid fixture accepted", e.name()),
    }

    // -- negatives: each fixture must fail with its named variant ------------
    for (name, want) in NEGATIVE_EXPECT {
        let bytes: &[u8] = match *name {
            "bad-magic" => include_bytes!("../fixtures/negative/bad-magic.elf"),
            "ei-class32" => include_bytes!("../fixtures/negative/ei-class32.elf"),
            "ei-data-be" => include_bytes!("../fixtures/negative/ei-data-be.elf"),
            "et-dyn" => include_bytes!("../fixtures/negative/et-dyn.elf"),
            "phnum-zero" => include_bytes!("../fixtures/negative/phnum-zero.elf"),
            "phentsize-wrong" => include_bytes!("../fixtures/negative/phentsize-wrong.elf"),
            "memsz-lt-filesz" => include_bytes!("../fixtures/negative/memsz-lt-filesz.elf"),
            "offset-oob" => include_bytes!("../fixtures/negative/offset-oob.elf"),
            "interp-present" => include_bytes!("../fixtures/negative/interp-present.elf"),
            "entry-oob" => include_bytes!("../fixtures/negative/entry-oob.elf"),
            _ => continue,
        };
        let got = elf::parse(bytes).err();
        let ok = got == Some(*want);
        let mut nbuf = [0u8; 96];
        let nn;
        {
            let mut w = BufWriter {
                buf: &mut nbuf,
                n: 0,
            };
            use core::fmt::Write as _;
            let _ = core::write!(w, "negative: {} refused as {}", name, want.name());
            nn = w.n;
        }
        let name_s = core::str::from_utf8(&nbuf[..nn]).unwrap_or("?");
        match got {
            Some(e) => checkf!(ok, name_s, "got {}, want {}", e.name(), want.name()),
            None => checkf!(false, name_s, "image was ACCEPTED"),
        }
    }

    // -- load + staged readback ----------------------------------------------
    match elf::load(mini) {
        Ok(img) => {
            let seg0 = img.segments[0].expect("seg0 staged");
            let seg1 = img.segments[1].expect("seg1 staged");
            checkf!(
                img.used == 2,
                "load: both segments staged",
                "used {}",
                img.used
            );
            let window = paging::private_base() + elf::LOAD_WINDOW;
            checkf!(
                seg0.page_base == window,
                "load: seg0 lands at the documented window",
                "got {:#x}, want {:#x}",
                seg0.page_base,
                window
            );
            checkf!(
                seg0.delta == 0x100,
                "load: seg0 delta is the documented 0x100",
                "got {:#x}",
                seg0.delta
            );
            checkf!(
                seg1.page_base == window + elf::SEGMENT_SLOT,
                "load: seg1 one 2 MiB slot above seg0",
                "got {:#x}",
                seg1.page_base
            );
            checkf!(
                seg1.delta == SEG1_DELTA,
                "load: seg1 delta is the documented 0x108",
                "got {:#x}",
                seg1.delta
            );

            // Byte-level readback of the payloads at their staged deltas.
            let d0 = seg0.page_base + seg0.delta;
            let d1 = seg1.page_base + seg1.delta;
            let mut ok_code = true;
            for (k, &b) in mini[0x100..0x108].iter().enumerate() {
                let got = unsafe { ((d0 as usize + k) as *const u8).read_volatile() };
                ok_code &= got == b;
            }
            checkf!(
                ok_code,
                "load: code payload byte-exact at staged delta",
                "first {:#x} last {:#x}",
                CODE_FIRST,
                CODE_LAST
            );
            let mut ok_data = true;
            for (k, &b) in mini[0x108..0x118].iter().enumerate() {
                let got = unsafe { ((d1 as usize + k) as *const u8).read_volatile() };
                ok_data &= got == b;
            }
            checkf!(
                ok_data,
                "load: data payload byte-exact at staged delta",
                "first {:#x} last {:#x}",
                DATA_FIRST,
                DATA_LAST
            );
            // BSS tail zeroed (documented zero-fill rule).
            let mut ok_bss = true;
            for k in SEG1_FILESZ..SEG1_MEMSZ {
                let got = unsafe { ((d1 as usize + k as usize) as *const u8).read_volatile() };
                ok_bss &= got == 0;
            }
            checkf!(
                ok_bss,
                "load: BSS tail zero-filled to p_memsz",
                "bytes {:#x}..{:#x}",
                SEG1_FILESZ,
                SEG1_MEMSZ
            );

            // The staged pages really resolve through the kernel tables.
            check(
                paging::translate(d0).is_some() && paging::translate(d1).is_some(),
                "load: staged pages resolve via paging::translate",
                "",
            );

            // Host-committed digest over the staged region, recomputed by
            // the gate's own sha256.
            let (digest, len) = staged_digest(&seg0, &seg1);
            checkf!(
                len == STAGED_LEN,
                "load: staged length matches the host oracle",
                "got {}, want {}",
                len,
                STAGED_LEN
            );
            let (hx, hl) = hex(&digest);
            let want = STAGED_SHA256.as_bytes();
            let mut got_hex = [0u8; 65];
            got_hex[..hl].copy_from_slice(&hx[..hl]);
            got_hex[hl] = 0;
            let got_str = core::str::from_utf8(&got_hex[..hl]).unwrap_or("?");
            checkf!(
                hx[..hl] == want[..hl],
                "load: staged-region sha256 equals the host oracle's digest",
                "got {}, want {}",
                got_str,
                STAGED_SHA256
            );

            // Done-when evidence line for the harness.
            sprintln!(
                "ELF_KERNEL_PROBE layout seg0_base {:#x} seg0_delta {:#x} seg1_base {:#x} seg1_delta {:#x} entry {:#x}",
                seg0.page_base, seg0.delta, seg1.page_base, seg1.delta, img.entry
            );
            sprintln!("ELF_KERNEL_PROBE verdict staged_sha256 {}", got_str);
        }
        Err(e) => check(false, "load: valid fixture loads", e.name()),
    }

    let (p, f) = summary();
    sprintln!("elf gate: {} passed, {} failed", p, f);
}

pub fn summary() -> (u32, u32) {
    unsafe { (PASS, FAIL) }
}
