//! K3 FAT32 selftest (fat gate).
//!
//! The oracle situation is stated plainly:
//!
//! - The corpus image is built by tools/gen-fat32-corpus.py from the FAT32
//!   spec (section citations in that file) and is byte-deterministic
//!   (regenerated and compared in CI before the mdir diff runs).
//! - The expectations below mirror tools/fat32-corpus-manifest.json, which
//!   the generator writes from its own CASES table. tools/verify-fat32.py
//!   re-derives the same facts from the image independently (spec walk, no
//!   code shared with the generator), and CI's fsck.fat + mdir stage is the
//!   external-oracle layer: fsck validates the image, and real mdir's
//!   listing is diffed against the FATLIST blocks this boot emits.
//! - No check below compares the driver against the writer via the same
//!   code path; the layered oracles are what make a shared misconception
//!   visible.

use crate::fat::{FatError, FatVolume};
// sprintln! comes from #[macro_use] mod serial in main.rs (crate root; it
// is a macro, not a module item, so it must not be path-imported).

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

fn mount() -> Option<FatVolume> {
    // The corpus arrives as a bootloader module (limine.conf module_path;
    // PROTOCOL.md "Module Feature" - address is 4 KiB aligned and the
    // memory is exclusively ours). No block IO, no incbin.
    let m = crate::limine::first_module_matching("fat32-corpus.img")?;
    // Sanity bound only: the corpus is ~32.75 MiB (>= 65525 clusters makes
    // a FAT32 volume) and the ESP that carries it is 39 MiB.
    if m.address == 0 || m.size == 0 || m.size > 64 << 20 {
        return None;
    }
    match FatVolume::attach(m.address as *const u8, m.size as usize) {
        Ok(v) => Some(v),
        Err(e) => {
            sprintln!(
                "        module: addr={:#x} size={:#x} path={:?} attach err: {:?}",
                m.address,
                m.size,
                core::str::from_utf8(&m.path[..m.path_len]).unwrap_or("?"),
                e
            );
            let p = m.address as *const u8;
            unsafe {
                sprintln!(
                    "        first 16 bytes: {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x} {:02x}",
                    *p, *p.add(1), *p.add(2), *p.add(3),
                    *p.add(4), *p.add(5), *p.add(6), *p.add(7),
                    *p.add(8), *p.add(9), *p.add(10), *p.add(11),
                    *p.add(12), *p.add(13), *p.add(14), *p.add(15)
                );
            }
            None
        }
    }
}

/// Render one directory's mdir-format listing into `buf`; returns the
/// used length.
fn listing(v: &FatVolume, cluster: u32, buf: &mut [u8]) -> Result<usize, FatError> {
    v.list_dir_mdir(cluster, buf)
}

/// no_std helper: does `hay` contain `needle`?
fn contains(hay: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() || hay.len() < needle.len() {
        return false;
    }
    (0..=hay.len() - needle.len()).any(|i| &hay[i..i + needle.len()] == needle)
}

/// The i-th '\n'-terminated line of `buf`.
fn line(buf: &[u8], i: usize) -> Option<&[u8]> {
    buf.split(|&b| b == b'\n').nth(i)
}

/// True if `buf` contains a line exactly equal to `want`.
fn has_line(buf: &[u8], want: &[u8]) -> bool {
    buf.split(|&b| b == b'\n').any(|l| l == want)
}

pub fn run() {
    sprintln!("fat gate:   mounting the embedded FAT32 corpus (limine module)");

    let v = match mount() {
        Some(v) => v,
        None => {
            check(false, "corpus image mounts", "FatVolume::attach failed");
            return;
        }
    };

    // -- mount + geometry (derived from the BPB, not from the generator) ----
    // 67064 total - 32 reserved - 2*516 FAT = 66000 data sectors / 1 per
    // cluster = 66000 clusters. (The fixture had 1014 clusters until
    // 2026-10-09: too few for FAT32, so attach now refuses it. R2.)
    check(
        v.count_of_clusters() == 66000,
        "BPB cluster count derives to 66000 (spec sec 3.4.1 arithmetic)",
        "count_of_clusters != 66000",
    );
    check(
        v.root_cluster() == 2,
        "BPB_RootClus is 2 as declared",
        "root cluster != 2",
    );

    // -- root directory surface --------------------------------------------
    let mut names: [[u8; 768]; 16] = [[0; 768]; 16];
    let mut name_lens = [0usize; 16];
    let mut attrs = [0u8; 16];
    let mut sizes = [0u32; 16];
    let mut n = 0usize;
    let root = v.root_cluster();
    let mut rd_ok = true;
    v.read_dir(root, &mut |e| {
        if n < 16 {
            names[n][..e.name_len].copy_from_slice(&e.name[..e.name_len]);
            name_lens[n] = e.name_len;
            attrs[n] = e.attr;
            sizes[n] = e.size;
            n += 1;
        }
        false
    })
    .unwrap_or_else(|_| rd_ok = false);
    check(
        rd_ok,
        "root directory walks without error",
        "read_dir(root) failed",
    );
    check(
        n == 13,
        "root holds exactly 13 entries (label, 9 files, 3 dirs)",
        "entry count mismatch",
    );

    // Expected order from tools/fat32-corpus-manifest.json (the generator's
    // CASES, in on-disk insertion order). Cases 01 and 09 have no LFN, so
    // they decode to their NTRes/upper 8.3 forms.
    let expect13: &[&str] = &[
        "NOVAVOL    ", // label: raw 11-byte field, no LFN (spec sec 5)
        "SIMPLE.TXT",
        "hello world.txt",
        "NoVA.File.v2.txt",
        "a-very-long-filename-indeed.bin",
        "this-filename-requires-three-lfn-directory-slots.txt",
        "secret plan.txt",
        "uñí öde.txt",
        "UPPER.TXT",
        "lower.txt",
        "projects",
        "empty directory",
        "many entries",
    ];
    let mut order_ok = true;
    for (i, want) in expect13.iter().enumerate() {
        let got = core::str::from_utf8(&names[i][..name_lens[i]]).unwrap_or("?");
        if got != *want {
            order_ok = false;
            sprintln!("        entry {}: got {:?}, want {:?}", i, got, want);
        }
    }
    check(
        order_ok,
        "root entries decode in on-disk order with exact names (LFN + 8.3 + NTRes)",
        "name/order mismatch (see log lines above)",
    );

    // Volume label surfaced with attr 0x08 (spec sec 5).
    check(
        attrs[0] & 0x08 != 0 && &names[0][..7] == b"NOVAVOL",
        "volume label entry surfaces with attr VOLUME_ID (raw 11-byte field)",
        "label missing or wrong attr",
    );

    // Sizes straight from the manifest (indexes into the order above).
    let want_sizes: [(usize, u32); 8] = [
        (1, 16),
        (2, 22),
        (3, 16),
        (4, 768),
        (5, 5120),
        (6, 7),
        (7, 8),
        (9, 12),
    ];
    let mut sizes_ok = true;
    for (idx, want) in want_sizes {
        if sizes[idx] != want {
            sizes_ok = false;
        }
    }
    check(
        sizes_ok,
        "file sizes match the manifest (16/22/16/768/5120/7/8/12)",
        "size mismatch",
    );

    // -- mdir-format listing of the root ------------------------------------
    let mut listbuf = [0u8; 8192];
    let root_list_len = match listing(&v, root, &mut listbuf) {
        Ok(n) => n,
        Err(_) => {
            check(false, "root listing renders", "list_dir_mdir failed");
            return;
        }
    };
    let root_list = &listbuf[..root_list_len];
    // 11 file lines (13 entries - label - hidden) + 1 summary line = 12;
    // split on '\n' yields 13 parts (12 + trailing empty).
    check(
        root_list.split(|&b| b == b'\n').count() == 13,
        "root listing has 11 file lines + summary (label and hidden excluded)",
        "line count mismatch",
    );
    check(
        !contains(root_list, b"secret plan.txt") && !contains(root_list, b"SECRET~1"),
        "hidden entry absent from the default listing (dir.c attr & 0x6 rule)",
        "hidden file leaked into the listing",
    );
    check(
        !contains(root_list, b"NOVAVOL"),
        "volume label not listed as a file row (print_volume_label rule)",
        "label leaked into the listing",
    );

    // Exact line probes. These strings are real mdir output, not renderer
    // output: SIMPLE, lower and the summary are copied from mdir on this
    // corpus (docs/logs/ci-37941316726-stage9.log). That run predates the
    // LFN checksum fix, so mdir printed no long names; the HELLOW~1 and
    // PROJECTS suffixes follow real lines with one ("2026~1 ... 2026
    // january notes" in the same log, "LIMINE~1 CON ... limine.conf" in
    // docs/logs/ci1-run-36001422533-green-stage7-8.log). Until 2026-10-09
    // these probes held the renderer's own guesses (one space between
    // date and time, "<DIR> ", a different summary), so they passed while
    // real mdir disagreed.
    check(
        has_line(
            root_list,
            b"HELLOW~1 TXT        22 2026-09-01  12:34  hello world.txt",
        ),
        "exact mdir line: raw short columns + %8ld + yyyy-mm-dd + HH:MM + LFN suffix",
        "no line matched the byte-exact expectation",
    );
    check(
        // mdir prints the RAW 8+3 fields split by a space (no trimming):
        // "lower   " + " " + "txt" + " " + " %8ld".
        has_line(root_list, b"lower    txt        12 2026-09-01  12:34 "),
        "NTRes 0x18 renders lowercase in the padded 8.3 columns, no LFN suffix",
        "no line matched the byte-exact expectation",
    );
    check(
        // "SIMPLE  " + " " + "TXT" + " " + "      16" (8-wide, space-padded).
        has_line(root_list, b"SIMPLE   TXT        16 2026-09-01  12:34 "),
        "plain 8.3 line has the raw padded short columns and no LFN suffix",
        "no line matched the byte-exact expectation",
    );
    check(
        // "PROJECTS" + " " + "   " (empty ext field) + " " + "<DIR>    ".
        has_line(root_list, b"PROJECTS     <DIR>     2026-09-01  12:34  projects"),
        "directory rows use the <DIR> column exactly like real mdir",
        "no line matched the byte-exact expectation",
    );
    // Summary: 11 files, bytes 16+22+16+768+5120+8+6+12 = 5968, dotted_num
    // width 13. Copied from real mdir (docs/logs/ci-37941316726-stage9.log).
    check(
        line(root_list, 11) == Some(&b"       11 files               5 968 bytes"[..]),
        "summary line uses mtools dotted_num (width 13, space separators)",
        "summary line mismatch",
    );

    // -- content reads -------------------------------------------------------
    let hello = match v.lookup(root, "hello world.txt") {
        Ok(e) => e,
        Err(_) => {
            check(false, "LFN lookup finds the entry", "lookup failed");
            return;
        }
    };
    let mut buf = [0u8; 64];
    match v.read_file(&hello, &mut buf) {
        Ok(got) => check(
            got == 22 && &buf[..got] == b"lowercase lfn content\n",
            "read_file returns the exact manifest content (LFN file)",
            "content mismatch",
        ),
        Err(_) => check(false, "read_file on the LFN file", "read failed"),
    }
    let multi = match v.lookup(root, "this-filename-requires-three-lfn-directory-slots.txt") {
        Ok(e) => e,
        Err(_) => {
            check(false, "multi-cluster LFN lookup", "lookup failed");
            return;
        }
    };
    let mut big = [0u8; 8192];
    match v.read_file(&multi, &mut big) {
        Ok(got) => {
            let pattern_ok = got == 5120
                && big[..got]
                    .iter()
                    .enumerate()
                    .all(|(i, b)| *b == (i % 256) as u8);
            check(
                pattern_ok,
                "10-cluster file reads byte-perfect across FAT chain links (range256)",
                "multi-cluster content mismatch",
            );
        }
        Err(_) => check(false, "read_file on the 5 KiB file", "read failed"),
    }

    // Error paths must be errors, never silent truncation.
    let mut small = [0u8; 4096];
    check(
        matches!(
            v.read_file(&multi, &mut small),
            Err(FatError::BufferTooSmall)
        ),
        "read_file refuses a too-small buffer (BufferTooSmall)",
        "wrong error or silent truncation",
    );
    check(
        matches!(v.lookup(root, "no such file.txt"), Err(FatError::NotFound)),
        "lookup of an absent name is NotFound",
        "wrong error",
    );

    // -- subdirectories ------------------------------------------------------
    let projects = match v.lookup(root, "projects") {
        Ok(e) => e,
        Err(_) => {
            check(false, "subdir lookup (projects)", "lookup failed");
            return;
        }
    };
    let sub = projects.first_cluster;
    let jan = match v.lookup(sub, "2026 january notes") {
        Ok(e) => e,
        Err(_) => {
            check(false, "nested dir lookup", "lookup failed");
            return;
        }
    };
    let deep = jan.first_cluster;
    let mut nb = [0u8; 64];
    match v.lookup(deep, "nested file with a long name.txt") {
        Ok(nested) => match v.read_file(&nested, &mut nb) {
            Ok(got) => check(
                got == 13 && &nb[..got] == b"deep content\n",
                "nested directory walk (2 levels) + LFN lookup + content read",
                "nested read mismatch",
            ),
            Err(_) => check(false, "nested read_file", "read failed"),
        },
        Err(_) => check(false, "deep file lookup", "lookup failed"),
    }

    // Dot entries per spec sec 6.4: "." points at the directory itself.
    let mut dots_ok = false;
    v.read_dir(deep, &mut |e| {
        if e.short[0] == b'.' && e.short[1] == b' ' {
            dots_ok = e.first_cluster == deep; // "." -> self
            true
        } else {
            false
        }
    })
    .ok();
    check(
        dots_ok,
        "dot entry \".\" carries the directory's own cluster (spec sec 6.4)",
        "dot entry wrong",
    );

    // -- many-entries directory (3-slot LFN names, 17-cluster directory) ----
    let many = match v.lookup(root, "many entries") {
        Ok(e) => e,
        Err(_) => {
            check(false, "many-entries dir lookup", "lookup failed");
            return;
        }
    };
    let mut count = 0u32;
    let mut many_ok = true;
    v.read_dir(many.first_cluster, &mut |e| {
        if e.short[0] == b'.' {
            return false;
        }
        // Names are "file-number-NN-with-padding.txt" in order.
        let name: &[u8] = &e.name;
        let prefix = b"file-number-";
        if !name.starts_with(prefix) {
            many_ok = false;
        } else {
            let nn = (name[12] - b'0') as u32 * 10 + (name[13] - b'0') as u32;
            if nn != count {
                many_ok = false;
            }
        }
        if e.size != 1 {
            many_ok = false;
        }
        count += 1;
        false
    })
    .ok();
    check(
        count == 64 && many_ok,
        "64 LFN files listed in order across a 17-cluster directory chain",
        "count or names mismatch",
    );

    // -- FATLIST marker blocks for the CI mdir diff --------------------------
    sprintln!("FATLIST /");
    for l in root_list.split(|&b| b == b'\n') {
        if !l.is_empty() {
            sprintln!("{}", core::str::from_utf8(l).unwrap_or("?"));
        }
    }
    let mut subbuf = [0u8; 2048];
    if let Ok(n) = listing(&v, sub, &mut subbuf) {
        sprintln!("FATLIST /projects");
        for l in subbuf[..n].split(|&b| b == b'\n') {
            if !l.is_empty() {
                sprintln!("{}", core::str::from_utf8(l).unwrap_or("?"));
            }
        }
    }
    let mut deepbuf = [0u8; 2048];
    if let Ok(n) = listing(&v, deep, &mut deepbuf) {
        sprintln!("FATLIST /projects/2026 january notes");
        for l in deepbuf[..n].split(|&b| b == b'\n') {
            if !l.is_empty() {
                sprintln!("{}", core::str::from_utf8(l).unwrap_or("?"));
            }
        }
    }
    // Bound marker so the stage-9 diff can cut blocks out of the serial
    // stream without relying on what follows the last one.
    sprintln!("FATLIST END");

    let (p, f) = unsafe { (PASS, FAIL) };
    sprintln!("fat gate:   {} checks passed, {} failed", p, f);
}

pub fn summary() -> (u32, u32) {
    unsafe { (PASS, FAIL) }
}
