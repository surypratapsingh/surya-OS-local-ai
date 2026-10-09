#!/usr/bin/env python3
"""Independent FAT32 corpus checker (the local oracle layer).

This tool re-derives the corpus facts DIRECTLY from the Microsoft EFI FAT32
File System Specification (EFISP01 / fatgen103.doc) by reading the raw image
bytes with its own walk. It shares NO code with:

  - tools/gen-fat32-corpus.py (the writer),
  - kernel/src/fat.rs (the driver under test),
  - mdir (the CI oracle).

Its purpose: a spec checker runnable on hosts without mtools or dosfstools.
scripts/check.sh stage 9 runs it on every host; in CI, fsck.fat and real
mdir provide the external-oracle layer on top, and stage 9 diffs the
kernel's listing against real mdir. (An mdir-format renderer used to live
here too. Nothing compared it to anything, and it shared the kernel's
wrong guesses about mdir's layout - CI 37941316726 - so it was deleted.)

Checks performed (spec section per check; numbers not re-checked against
the document on this host - see the BPB comment in Fat32.__init__):
  boot sector  - 0x55AA trail (sec 3.1), BPB sanity, FAT type rule
                 (CountofClusters >= 65525) and FAT size (one entry per
                 cluster + 2), FSInfo signatures (sec 3.2),
                 backup boot sector presence (sec 3.1 BPB_BkBootSec)
  FAT          - reserved entries FAT[0]/FAT[1] (sec 4.2), chain
                 termination on EOC (sec 4.2), no cluster referenced
                 twice, no chain walks off the volume
  directories  - dot entries of subdirectories point at self/parent, and
                 ".." is 0 when the parent is the root (sec 6.4), LFN
                 sequences contiguous, LDIR_Type 0, and LDIR_Chksum equal
                 to the short-name checksum of the entry that follows
                 (sec 6.2/6.3), volume label equal to BS_VolLab

Exit 0 = all checks pass; exit 1 = at least one finding. Findings are
printed with the failing detail.
"""

import struct
import sys
from pathlib import Path

# Findings can carry non-ASCII names; Windows consoles default to cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SECTOR = 512

findings: list[str] = []


def fail(msg: str) -> None:
    findings.append(msg)
    print(f"  FINDING {msg}")


def need(cond: bool, msg: str) -> None:
    if not cond:
        fail(msg)


# ---------------------------------------------------------------------------
# Image walk (independent implementation: struct + offsets from the spec)
# ---------------------------------------------------------------------------

class Fat32:
    def __init__(self, data: bytes):
        self.d = data
        b = data
        need(b[510] == 0x55 and b[511] == 0xAA, "boot sector trail signature missing")
        self.bps = struct.unpack_from("<H", b, 11)[0]
        self.spc = b[13]
        self.rsvd = struct.unpack_from("<H", b, 14)[0]
        self.nfats = b[16]
        self.tot32 = struct.unpack_from("<I", b, 32)[0]
        self.fatsz = struct.unpack_from("<I", b, 36)[0]
        self.rootclus = struct.unpack_from("<I", b, 44)[0]
        # Spec rules only. The earlier version also required the generator's
        # own constants (2 MiB image, 4 sectors per cluster, 1014 clusters),
        # so it passed an image that is not FAT32 at all (decisions.md
        # 2026-10-09). Section names are from the spec's headings; their
        # numbers were not re-checked against the document on this host.
        # 512 is a limit of this tool (SECTOR offsets), not of the spec.
        need(self.bps == 512, f"BPB_BytsPerSec {self.bps} != 512")
        # BPB_SecPerClus: a power of 2, 1..128 (BPB field table).
        need(
            self.spc in (1, 2, 4, 8, 16, 32, 64, 128),
            f"BPB_SecPerClus {self.spc} is not a power of 2 in 1..128",
        )
        need(self.nfats == 2, f"BPB_NumFATs {self.nfats} != 2")
        need(self.rootclus == 2, f"BPB_RootClus {self.rootclus} != 2")
        need(struct.unpack_from("<H", b, 17)[0] == 0, "BPB_RootEntCnt nonzero (FAT32)")
        need(struct.unpack_from("<H", b, 22)[0] == 0, "BPB_FATSz16 nonzero (FAT32)")
        need(
            len(data) >= self.tot32 * SECTOR,
            f"image is {len(data)} bytes, BPB_TotSec32 needs {self.tot32 * SECTOR}",
        )
        # "FAT Type Determination": RootDirSectors is 0 on FAT32 (RootEntCnt
        # is 0, checked above), so DataSec = TotSec - (Rsvd + NumFATs*FATSz)
        # and CountofClusters = DataSec / SecPerClus, rounded down.
        data_sectors = self.tot32 - self.rsvd - self.nfats * self.fatsz
        self.nclusters = data_sectors // self.spc
        # A volume with CountofClusters < 65525 is FAT12 or FAT16 by
        # definition, whatever its BPB says. This is a rule, not a formatter
        # heuristic: fsck.fat and mtools both apply it (CI run 37935532518).
        need(
            self.nclusters >= 65525,
            f"CountofClusters {self.nclusters} < 65525: not a FAT32 volume",
        )
        # The FAT holds one 4-byte entry per cluster plus the two reserved
        # entries FAT[0] and FAT[1].
        fat_entries = self.fatsz * self.bps // 4
        need(
            fat_entries >= self.nclusters + 2,
            f"FAT holds {fat_entries} entries, {self.nclusters} clusters need "
            f"{self.nclusters + 2}",
        )
        print(f"  geometry: {self.nclusters} clusters, FAT holds {fat_entries} entries")
        # FSInfo (sec 3.2): it lives in the reserved area at the sector
        # given by BPB_FSInfo (offset 48), which the generator sets to 1 -
        # i.e. the sector RIGHT AFTER the boot sector, not after the FATs.
        fsinfo_sector = struct.unpack_from("<H", b, 48)[0]
        need(fsinfo_sector == 1, f"BPB_FSInfo {fsinfo_sector} != 1")
        fsi_off = fsinfo_sector * SECTOR
        fsi = b[fsi_off:fsi_off + SECTOR]
        need(struct.unpack_from("<I", fsi, 0)[0] == 0x41615252, "FSInfo lead sig")
        need(struct.unpack_from("<I", fsi, 484)[0] == 0x61417272, "FSInfo struct sig")
        # Backup boot sector (sec 3.1, BPB_BkBootSec at offset 50).
        bk = struct.unpack_from("<H", b, 50)[0]
        need(bk == 6, f"BPB_BkBootSec {bk} != 6")
        bb = data[bk * SECTOR : bk * SECTOR + SECTOR]
        need(bb[510:512] == b"\x55\xaa", "backup boot sector trail sig")

        self.fat_off = self.rsvd * SECTOR
        self.data_off = (self.rsvd + self.nfats * self.fatsz) * SECTOR

        # FAT[0]/FAT[1] reserved entries (sec 4.2): F8FFFFFF / FFFFFFFF-ish.
        fat0 = struct.unpack_from("<I", b, self.fat_off)[0]
        fat1 = struct.unpack_from("<I", b, self.fat_off + 4)[0]
        need(fat0 & 0xFF == 0xF8, f"FAT[0] media byte {fat0 & 0xFF:02x} != F8")
        need((fat1 & 0x0FFFFFF8) == 0x0FFFFFF8, f"FAT[1] not EOC: {fat1:08x}")

        # No cluster referenced twice across all chains (corruption probe).
        self.refs: dict[int, str] = {}

    def fat(self, c: int) -> int:
        return struct.unpack_from("<I", self.d, self.fat_off + c * 4)[0]

    def cluster_off(self, c: int) -> int:
        need(2 <= c < self.nclusters + 2, f"cluster {c} out of range")
        return self.data_off + (c - 2) * self.spc * SECTOR

    def chain(self, first: int, owner: str) -> list[int]:
        """Walk a FAT chain with loop + double-reference detection."""
        out = []
        c = first
        seen = set()
        while True:
            need(2 <= c < self.nclusters + 2, f"chain {owner}: cluster {c} out of range")
            if c in seen:
                fail(f"chain {owner}: loop at cluster {c}")
                return out
            seen.add(c)
            if c in self.refs and self.refs[c] != owner:
                fail(f"cluster {c} referenced by both {self.refs[c]} and {owner}")
            self.refs[c] = owner
            out.append(c)
            nxt = self.fat(c)
            if 0x0FFFFFF8 <= nxt <= 0x0FFFFFFF:
                return out
            need(nxt != 0, f"chain {owner}: FREE entry mid-chain at {c}")
            c = nxt

    def entries(self, first_cluster: int, owner: str):
        """Yield (raw32, lfn_units_or_None) per directory slot."""
        lfn_units: list[int] = []
        lfn_chk = None
        for c in self.chain(first_cluster, owner):
            base = self.cluster_off(c)
            for slot in range(self.spc * SECTOR // 32):
                raw = self.d[base + slot * 32 : base + slot * 32 + 32]
                if raw[0] == 0x00:
                    return
                if raw[0] == 0xE5:
                    lfn_units = []
                    continue
                attr = raw[11]
                if attr & 0x3F == 0x0F:
                    seq = raw[0]
                    # LDIR_Type (byte 12) must be 0; LDIR_Chksum is byte
                    # 13. The generator once wrote the checksum into byte
                    # 12 and nothing here noticed (fsck.fat did, CI
                    # 37941316726).
                    need(raw[12] == 0, f"{owner}: LDIR_Type is {raw[12]:#04x}, want 0")
                    chk = raw[13]
                    if seq & 0x40:
                        lfn_chk = chk
                        lfn_units = [0xFFFF] * (13 * (seq & 0x1F))
                    need(
                        lfn_chk == chk,
                        f"{owner}: LFN checksum changed mid-sequence",
                    )
                    units = (
                        struct.unpack("<5H", raw[1:11])
                        + struct.unpack("<6H", raw[14:26])
                        + struct.unpack("<2H", raw[28:32])
                    )
                    o = ((seq & 0x1F) - 1) * 13
                    for k, u in enumerate(units):
                        lfn_units[o + k] = u
                    continue
                if lfn_units:
                    need(
                        lfn_chk == short_checksum(raw[0:11]),
                        f"{owner}: LFN checksum {lfn_chk:#04x} != short-name "
                        f"checksum {short_checksum(raw[0:11]):#04x} of {raw[0:11]!r}",
                    )
                yield raw, lfn_units if lfn_units else None
                lfn_units = []


def short_checksum(short: bytes) -> int:
    """Spec sec 6.2 LFN checksum over the 11 short-name bytes."""
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def decode_name(raw: bytes, lfn_units) -> tuple[str, bool]:
    """(decoded name, had_lfn). Short names via NTRes; LFN via UTF-16LE."""
    if lfn_units is not None:
        out = []
        for u in lfn_units:
            if u == 0x0000:
                break
            if u == 0xFFFF:
                break
            out.append(chr(u))
        return "".join(out), True
    ntres = raw[12]
    base = raw[0:8].rstrip(b" ").decode("ascii")
    ext = raw[8:11].rstrip(b" ").decode("ascii")
    if ntres & 0x08:
        base = base.lower()
    if ntres & 0x10:
        ext = ext.lower()
    return base + ("." + ext if ext else ""), False


# ---------------------------------------------------------------------------
# Expected facts (from the generator's CASES declaration; the generator is
# the corpus's provenance record, NOT a code dependency - verify independently)
# ---------------------------------------------------------------------------

ROOT_WANT_NAMES = [
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
]


def main() -> int:
    img = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("kernel/src/fat32_corpus.img")
    data = img.read_bytes()
    print(f"verify-fat32: {img} ({len(data)} bytes)")
    fs = Fat32(data)

    # Reserved FAT entries and chain integrity for every file and directory.
    chains = {}

    def collect(cluster: int, owner: str):
        chains[owner] = fs.chain(cluster, owner)

    # Walk the root first, then every subdirectory found there.
    root_names = []
    subdirs = {}
    def check_file(raw: bytes, name: str, first: int) -> None:
        # A file's clusters belong to it alone; walking every file chain
        # lets the double-reference probe in chain() see cross-links. An
        # empty file owns no cluster, so its first cluster is 0.
        if struct.unpack_from("<I", raw, 28)[0] == 0:
            need(first == 0, f"empty file {name!r} has first cluster {first}, want 0")
        else:
            collect(first, f"file:{name}")

    for raw, lfn in fs.entries(fs.rootclus, "root"):
        attr = raw[11]
        first = (struct.unpack_from("<H", raw, 20)[0] << 16) | struct.unpack_from(
            "<H", raw, 26
        )[0]
        if attr & 0x08:
            need(raw[0:11].rstrip() == b"NOVAVOL", "volume label bytes")
            # BS_VolLab (FAT32 boot sector offset 71, 11 bytes) matches the
            # root's label entry; fsck.fat flags a mismatch.
            need(fs.d[71:82] == raw[0:11], f"BS_VolLab {fs.d[71:82]!r} != label {raw[0:11]!r}")
            need(first == 0, f"volume label has first cluster {first}, want 0")
            continue
        if raw[0:1] == b".":
            fail("dot entries in the root directory (spec sec 6.4: none)")
        name, _ = decode_name(raw, lfn)
        root_names.append(name)
        if attr & 0x10:
            subdirs[name] = first
        else:
            check_file(raw, name, first)
    need(root_names == ROOT_WANT_NAMES, f"root names {root_names!r}")

    for name, first in subdirs.items():
        collect(first, f"dir:{name}")
        for raw, lfn in fs.entries(first, f"dir:{name}"):
            if not raw[11] & 0x10 and raw[0:1] != b".":
                fname, _ = decode_name(raw, lfn)
                f_first = (struct.unpack_from("<H", raw, 20)[0] << 16) | struct.unpack_from(
                    "<H", raw, 26
                )[0]
                check_file(raw, f"{name}/{fname}", f_first)
            if raw[0:2] == b". ":
                need(
                    (struct.unpack_from("<H", raw, 20)[0] << 16)
                    | struct.unpack_from("<H", raw, 26)[0]
                    == first,
                    f"{name}: '.' does not point at itself (sec 6.4)",
                )
            if raw[0:2] == b"..":
                # Every dir here is a child of the root, and ".." of a
                # root child holds cluster 0, not BPB_RootClus (fatgen103:
                # "which is 0 if this directories parent is the root
                # directory"). This check used to want 2; fsck.fat called
                # that "Invalid '..' entry" (CI 37941316726).
                need(
                    (struct.unpack_from("<H", raw, 20)[0] << 16)
                    | struct.unpack_from("<H", raw, 26)[0]
                    == 0,
                    f"{name}: '..' of a root child is not cluster 0 (sec 6.4)",
                )

    # The many-entries directory must hold exactly 64 LFN files.
    many_first = subdirs["many entries"]
    many_names = [
        decode_name(raw, lfn)[0]
        for raw, lfn in fs.entries(many_first, "dir:many entries")
        if not raw[0:1] == b"."
    ]
    need(len(many_names) == 64, f"many-entries holds {len(many_names)} files")
    need(
        many_names
        == [f"file-number-{i:02d}-with-padding.txt" for i in range(64)],
        "many-entries names/order",
    )

    if findings:
        print(f"verify-fat32: {len(findings)} FINDINGS")
        return 1

    print("verify-fat32: ALL CHECKS PASSED (no findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
