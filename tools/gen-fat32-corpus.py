#!/usr/bin/env python3
"""Generate the K3 FAT32 corpus image + case manifest.

The image is a REAL FAT32 filesystem built from the on-disk layout in
Microsoft's specification:

    Microsoft EFI FAT32 File System Specification, EFISP01 (fatgen103.doc),
    "FAT: General Overview of On-Disk Format", version 1.03:
      - BPB/EBPB fields:            sec. 3.1 (BPB and Boot Sector layout)
      - cluster count >= 65525:     sec. 3.4.1 (what makes a volume FAT32)
      - reserved entries:           sec. 4.2 (FAT[0] = 0x0FFFFFF8 media
                                    descriptor, FAT[1] = 0x0FFFFFFF EOC)
      - chain termination:          sec. 4.2 (0x0FFFFFF8..0x0FFFFFFF = EOC)
      - directory entry 32 bytes:   sec. 5 (Short 8.3 Directory Entry)
      - attr bits 0x01..0x20,       sec. 5.1
        volume label 0x08,
        directory 0x10
      - LFN entries:                sec. 6.2-6.3 (attr = 0x0F, sequence
        numbers, checksum over the 11 short-name bytes, name chars in
        UTF-16LE over words 1-10, 14-25, 28-31, 0x0000 terminator,
        0xFFFF padding)
      - dot entries "." / "..":     sec. 6.4 (first two entries of every
        non-root directory)
      - free-cluster count:         sec. 5 (FSI_Free_Count = number of free
        clusters, possibly inexact)

The generator is DETERMINISTIC: same script version -> byte-identical image
(fixed timestamps, fixed content, no hash seed dependence, no dict-order
dependence on PYTHONHASHSEED).

Every directory case is declared in CASES; the manifest lists them with
their LFN names, sizes and the exact 8.3 names the spec's name rules
produce (verified against section 6.1 + the mdir listing at CI time).

The kernel never parses this script's structures: it mounts the committed
image through the same driver it will one day point at a block device,
and the oracle is mdir (CI) plus tools/verify-fat32.py (spec-derived,
independent of this script's writer code paths).
"""

import hashlib
import json
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Geometry (FAT32 spec section 3.1 field names)
# ---------------------------------------------------------------------------
SECTOR = 512
SEC_PER_CLUS = 1                  # smallest legal cluster = smallest image
BYTES_PER_CLUSTER = SEC_PER_CLUS * SECTOR
RESERVED_SECTORS = 32             # includes boot sector + FSInfo + backup
FATS = 2
# FAT type is decided by the cluster count alone: below 65525 a volume is
# FAT12/16 whatever its BPB says (spec "FAT Type Determination"; fsck.fat and
# mtools both enforce it). The first corpus had 1014 clusters and a 512-entry
# FAT, and its comments here called 65525 a "formatter heuristic" - wrong
# (decisions.md 2026-10-09). 66000 keeps clear of the boundary, which the
# spec warns some implementations get off by a few.
CLUSTER_COUNT = 66000
# One 4-byte entry per cluster plus FAT[0] and FAT[1], rounded up to sectors.
FAT_SECTORS = -(-(CLUSTER_COUNT + 2) * 4 // SECTOR)   # 516
ROOT_CLUSTER = 2

DATA_START = RESERVED_SECTORS + FATS * FAT_SECTORS
TOTAL_SECTORS = DATA_START + CLUSTER_COUNT * SEC_PER_CLUS  # 67064 (~32.75 MiB)
assert CLUSTER_COUNT >= 65525
assert FAT_SECTORS * SECTOR // 4 >= CLUSTER_COUNT + 2

# Fixed build timestamp (2026-09-01 12:34:56) -> DOS date/time fields.
DOS_DATE = (2026 - 1980) << 9 | 9 << 5 | 1          # yyyyymmddhhmmss style pack
DOS_TIME = 12 << 11 | 34 << 5 | (56 // 2)

EOC = 0x0FFFFFF8                    # spec sec 4.2: any of F8..FF terminates
FREE = 0x00000000

ATTR_RO, ATTR_HIDDEN, ATTR_SYS, ATTR_VOL, ATTR_DIR, ATTR_LFN = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x0F)


def short_name_checksum(short: bytes) -> int:
    """LFN checksum over the 11 short-name bytes (spec sec 6.2)."""
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def content_fields(content: bytes) -> dict:
    """Manifest fields describing a file's expected bytes.

    content_sha256 is the oracle for mtype pipelines in CI; small textual
    payloads are also embedded verbatim (content_hex) and range patterns are
    tagged content_pattern so the kernel can verify them without a hash.
    """
    fields: dict = {"content_sha256": hashlib.sha256(content).hexdigest()}
    if len(content) <= 64:
        try:
            fields["content"] = content.decode("utf-8")
        except UnicodeDecodeError:
            fields["content_hex"] = content.hex()
    elif content == bytes(range(256)) * (len(content) // 256) and len(content) % 256 == 0:
        fields["content_pattern"] = "range256"
    return fields


def pack_83(base: str, ext: str) -> bytes:
    """Space-padded, upper-cased 11-byte 8.3 field (spec sec 5, name field)."""
    b = base.upper().ljust(8).encode("ascii")
    e = ext.upper().ljust(3).encode("ascii")
    assert len(b) == 8 and len(e) == 3
    return b + e


def lfn_entries(name: str, short: bytes) -> list[bytes]:
    """LFN directory entries for `name`, newest-first (spec sec 6.2/6.3).

    Each entry: attr byte 11 = 0x0F, byte 12 = short-name checksum, words
    1-10 at byte 1, words 14-25 at byte 14, words 28-31 at byte 28; name
    UTF-16LE code units padded with 0xFFFF and terminated with 0x0000.
    """
    chk = short_name_checksum(short)
    # UTF-16LE code units; the on-disk sequence is name + 0x0000 + 0xFFFF pad.
    units = [ord(c) for c in name] + [0x0000]
    slots = (len(units) + 12) // 13
    while len(units) < slots * 13:
        units.append(0xFFFF)
    out = []
    for i in range(slots, 0, -1):
        seq = i if i < slots else (slots | 0x40)
        chunk = units[(i - 1) * 13: i * 13]
        e = bytearray(32)
        e = bytearray(32)
        e[0] = seq
        e[11] = ATTR_LFN
        e[12] = chk
        chars = chunk + [0xFFFF] * (13 - len(chunk))
        raw = struct.pack("<13H", *chars)  # 13 UTF-16 units = 26 bytes
        # word layout per spec sec 6.3: words 1-10, 14-25, 28-31
        e[1:11] = raw[0:10]
        e[14:26] = raw[10:22]
        e[28:32] = raw[22:26]
        out.append(bytes(e))
    return out


def dirent(short: bytes, attr: int, cluster: int, size: int,
           nt_reserved: bool = False) -> bytes:
    """One 32-byte short directory entry (spec sec 5).

    Offsets per the section 5 table:
      0..10   short name (11 bytes, space padded)
      11      attr
      12      NTRes (0x18 = lower-case base and ext, NT extension)
      20..21  first cluster high
      22..23  write time
      24..25  write date
      26..27  first cluster low
      28..31  file size
    """
    e = bytearray(32)
    assert len(short) <= 11, "8.3 field is 11 bytes"
    e[0:11] = short.ljust(11, b" ")  # ljust, NOT slice-assign: a short bytes
    # object would SHRINK the bytearray (bytearray(32)[0:10] = 10 bytes -> 31)
    e[11] = attr
    if nt_reserved:
        e[12] = 0x18  # basename+ext lowercase (NT extension; mdir honors it)
    struct.pack_into("<H", e, 20, (cluster >> 16) & 0xFFFF)  # first clus HI
    struct.pack_into("<H", e, 22, DOS_TIME)                  # write time
    struct.pack_into("<H", e, 24, DOS_DATE)                  # write date
    struct.pack_into("<H", e, 26, cluster & 0xFFFF)          # first clus LO
    struct.pack_into("<I", e, 28, size)
    return bytes(e)


class Volume:
    def __init__(self):
        self.img = bytearray(TOTAL_SECTORS * SECTOR)
        self.fat = bytearray(FAT_SECTORS * SECTOR)
        # Reserved entries (spec sec 4.2): FAT[0] carries the media
        # descriptor in its low byte (0xF8 = fixed disk), FAT[1] is EOC.
        # The first draft never wrote these - every cluster >= 2 worked,
        # but the volume was malformed and fsck.fat flags it.
        struct.pack_into("<I", self.fat, 0, 0x0FFFFFF8)
        struct.pack_into("<I", self.fat, 4, 0x0FFFFFFF)
        self.next_free = ROOT_CLUSTER
        self.dirs: dict[int, bytearray] = {}
        self.labels: list[bytes] = []

    # -- cluster allocation -------------------------------------------------
    def alloc_chain(self, nbytes: int) -> int:
        """Allocate a contiguous chain big enough for nbytes; return first.
        An empty file owns no cluster: first cluster 0. (Returning
        next_free without reserving it gave the label and SIMPLE.TXT the
        root directory's cluster.)"""
        assert nbytes >= 0
        if nbytes == 0:
            return 0
        nclus = (nbytes + BYTES_PER_CLUSTER - 1) // BYTES_PER_CLUSTER
        first = self.next_free
        assert first + nclus <= CLUSTER_COUNT + 2, "corpus image too small"
        for i in range(nclus):
            c = first + i
            nxt = (first + i + 1) if i + 1 < nclus else EOC
            struct.pack_into("<I", self.fat, c * 4, nxt)
        self.next_free += nclus
        return first

    def cluster_off(self, cluster: int) -> int:
        return (DATA_START + (cluster - 2) * SEC_PER_CLUS) * SECTOR

    # -- directory handling -------------------------------------------------
    def new_dir(self, parent_cluster: int, nbytes: int = BYTES_PER_CLUSTER) -> int:
        """Allocate a directory of at least `nbytes` (contiguous clusters,
        chained in the FAT) holding only its dot entries so far; return
        first cluster."""
        clusters = -(-nbytes // BYTES_PER_CLUSTER)
        c = self.alloc_chain(clusters * BYTES_PER_CLUSTER)
        buf = bytearray(clusters * BYTES_PER_CLUSTER)
        # spec sec 6.4: "." points at the dir itself, ".." at the parent
        buf[0:32] = dirent(b".          ", ATTR_DIR, c, 0)
        buf[32:64] = dirent(b"..         ", ATTR_DIR, parent_cluster, 0)
        self.dirs[c] = buf
        return c

    def add(self, dir_cluster: int, name: str, short: bytes, attr: int,
            content: bytes = b"", nt_reserved: bool = False,
            first: int | None = None) -> None:
        """Append (LFN?) + short entry to a directory; write file content."""
        size = 0 if attr & ATTR_DIR else len(content)
        if first is None:
            first = 0 if attr & ATTR_DIR else self.alloc_chain(len(content))
            if not attr & ATTR_DIR and content:
                off = self.cluster_off(first)
                self.img[off:off + len(content)] = content
        entries = []
        if not (attr & ATTR_VOL) and name is not None:
            entries += lfn_entries(name, short)
        entries.append(dirent(short, attr, first, size, nt_reserved))
        buf = self.dirs[dir_cluster]
        for e in entries:
            for i in range(0, len(buf), 32):
                if buf[i] in (0x00, 0xE5):
                    buf[i:i + 32] = e
                    break
            else:
                raise AssertionError("directory full")

    # -- final assembly -----------------------------------------------------
    def finish(self) -> bytes:
        # write directory clusters
        for c, buf in self.dirs.items():
            off = self.cluster_off(c)
            self.img[off:off + len(buf)] = buf

        # BPB boot sector (spec sec 3.1)
        bs = bytearray(SECTOR)
        assert len(bs) == SECTOR
        bs[0:3] = b"\xEB\x58\x90"                 # jmp + nop
        bs[3:11] = b"NOVACORP"                     # OEM name (8 bytes exactly)
        struct.pack_into("<H", bs, 11, SECTOR)     # BPB_BytsPerSec
        bs[13] = SEC_PER_CLUS                      # BPB_SecPerClus
        struct.pack_into("<H", bs, 14, RESERVED_SECTORS)
        bs[16] = FATS
        struct.pack_into("<H", bs, 17, 0)          # BPB_RootEntCnt = 0 (FAT32)
        struct.pack_into("<H", bs, 19, 0)          # BPB_TotSec16 = 0 (FAT32)
        bs[21] = 0xF8                              # media descriptor
        struct.pack_into("<H", bs, 22, 0)          # BPB_FATSz16 = 0 (FAT32)
        struct.pack_into("<H", bs, 24, 63)         # sectors per track
        struct.pack_into("<H", bs, 26, 255)        # heads
        struct.pack_into("<I", bs, 28, 0)          # hidden sectors
        struct.pack_into("<I", bs, 32, TOTAL_SECTORS)  # BPB_TotSec32
        struct.pack_into("<I", bs, 36, FAT_SECTORS)    # BPB_FATSz32
        struct.pack_into("<H", bs, 40, 0)          # ext flags: FATs mirrored
        struct.pack_into("<H", bs, 42, 0)          # version 0.0
        struct.pack_into("<I", bs, 44, ROOT_CLUSTER)   # BPB_RootClus
        struct.pack_into("<H", bs, 48, 1)          # FSInfo sector
        struct.pack_into("<H", bs, 50, 6)          # backup boot sector
        bs[64] = 0                                 # reserved (BPBS_Reserved1)
        bs[65:73] = b"\x00" * 8                    # reserved
        bs[73] = 0x29                              # extended boot signature
        struct.pack_into("<I", bs, 76, 0x4E4F5641)  # volume serial "NOVA"
        bs[80:91] = b"NOVAFAT32  "                 # volume label (11 bytes)
        bs[91:99] = b"FAT32   "                    # FS type (8 bytes)
        # Trail signature: 0xAA55 stored LITTLE-ENDIAN, i.e. byte 510 = 0x55
        # and byte 511 = 0xAA. (The first draft packed 0x55AA, which put
        # AA 55 on disk and made every FAT implementation - including our
        # own driver - rightly reject the volume.)
        struct.pack_into("<H", bs, 510, 0xAA55)
        assert len(bs) == SECTOR

        # FSInfo sector (spec sec 3.2): lead/struct/trail sigs + free count
        fsi = bytearray(SECTOR)
        struct.pack_into("<I", fsi, 0, 0x41615252)
        struct.pack_into("<I", fsi, 484, 0x61417272)
        free = CLUSTER_COUNT - (self.next_free - 2)
        struct.pack_into("<I", fsi, 488, free)     # FSI_Free_Count
        struct.pack_into("<I", fsi, 492, self.next_free)  # FSI_Nxt_Free
        # FSInfo trail signature: spec sec 3.2 "0xAA550000" - as a u32 in
        # little-endian that is bytes 00 00 55 AA (byte 510 = 0x55, byte
        # 511 = 0xAA), consistent with the boot-sector endianness note.
        struct.pack_into("<I", fsi, 508, 0xAA55_0000)
        assert len(fsi) == SECTOR

        backup_off = 6 * SECTOR
        self.img[0:SECTOR] = bs
        self.img[SECTOR:2 * SECTOR] = fsi
        self.img[backup_off:backup_off + SECTOR] = bs
        self.img[backup_off + SECTOR:backup_off + 2 * SECTOR] = fsi

        fat_off = RESERVED_SECTORS * SECTOR
        self.img[fat_off:fat_off + len(self.fat)] = self.fat
        self.img[fat_off + FAT_SECTORS * SECTOR:
                 fat_off + 2 * FAT_SECTORS * SECTOR] = self.fat
        # A bytearray slice-assign with an over-long value SILENTLY GROWS the
        # buffer (the boot-sector field writes above once produced a 2 MiB + 6
        # byte image). Never ship an image whose size drifted.
        assert len(self.img) == TOTAL_SECTORS * SECTOR, (
            f"image size drifted: {len(self.img)} != {TOTAL_SECTORS * SECTOR}")
        return bytes(self.img)


def build_image() -> tuple[bytes, list[dict]]:
    v = Volume()
    # The root is a cluster chain like any directory (FAT32 has no fixed
    # root region), so it is allocated in the FAT first. 2 KiB = 64 slots;
    # the root below uses 33.
    root = v.alloc_chain(2048)
    assert root == ROOT_CLUSTER
    v.dirs[root] = bytearray(2048)
    cases: list[dict] = []

    # Volume label in the root (spec sec 5, attr 0x08).
    v.add(root, None, b"NOVAVOL   ", ATTR_VOL)  # 7 + 4 spaces = 11 bytes
    cases.append({"case": "volume-label", "kind": "label",
                  "short": "NOVAVOL   ", "expect": "NOVAVOL   "})

    # Root cases ----------------------------------------------------------
    #  1 plain 8.3 upper            -> no LFN (name=None: no long entry)
    v.add(root, None, pack_83("SIMPLE", "TXT"), 0, b"plain 8.3 upper\n")
    cases.append({"case": "01-plain-83-upper", "kind": "file", "lfn": None,
                  "short": "SIMPLE  TXT", "size": 16,
                  **content_fields(b"plain 8.3 upper\n")})
    #  2 LFN lowercase file
    v.add(root, "hello world.txt", pack_83("HELLOW~1", "TXT"), 0,
          b"lowercase lfn content\n")
    cases.append({"case": "02-lfn-lowercase", "kind": "file",
                  "lfn": "hello world.txt", "short": "HELLOW~1 TXT",
                  "size": 22, **content_fields(b"lowercase lfn content\n")})
    #  3 LFN mixed case + digits + dot in basename
    v.add(root, "NoVA.File.v2.txt", pack_83("NOVA~1", "TXT"), 0,
          b"mixed case dots\n")
    cases.append({"case": "03-lfn-mixed-dots", "kind": "file",
                  "lfn": "NoVA.File.v2.txt", "short": "NOVA~1  TXT",
                  "size": 16, **content_fields(b"mixed case dots\n")})
    #  4 LFN longer than 13 chars (two LFN slots)
    v.add(root, "a-very-long-filename-indeed.bin", pack_83("AVERY~1", "BIN"),
          0, bytes(range(256)) * 3)  # 768 B: 2 clusters of 512 B
    cases.append({"case": "04-lfn-two-slots", "kind": "file",
                  "lfn": "a-very-long-filename-indeed.bin",
                  "short": "AVERY~1 BIN", "size": 768,
                  **content_fields(bytes(range(256)) * 3)})
    #  5 LFN > 2 slots (39+ chars) + multi-cluster file (5 KiB)
    long_name = "this-filename-requires-three-lfn-directory-slots.txt"
    assert len(long_name) > 26
    v.add(root, long_name, pack_83("THISF~1", "TXT"), 0,
          bytes(range(256)) * 20)  # 5120 B = 10 clusters of 512 B
    cases.append({"case": "05-lfn-three-slots-multicluster", "kind": "file",
                  "lfn": long_name, "short": "THISF~1 TXT", "size": 5120,
                  **content_fields(bytes(range(256)) * 20)})
    #  6 hidden + system attrs (mdir skips them without -a; -a lists them)
    v.add(root, "secret plan.txt", pack_83("SECRET~1", "TXT"),
          ATTR_RO | ATTR_HIDDEN, b"hidden\n")
    cases.append({"case": "06-hidden-system", "kind": "file",
                  "lfn": "secret plan.txt", "short": "SECRET~1 TXT",
                  "size": 7, "attr": "RO|HIDDEN",
                  **content_fields(b"hidden\n")})
    #  7 unicode LFN (utf-16 encodable, BMP)
    v.add(root, "uñí öde.txt", pack_83("U__~1", "TXT"), 0, b"unicode\n")
    cases.append({"case": "07-unicode-lfn", "kind": "file",
                  "lfn": "uñí öde.txt", "short": "U__~1  TXT", "size": 8,
                  **content_fields(b"unicode\n")})
    #  8 8.3 all-caps name WITH an LFN of itself uppercase (formatter wrote
    #    it because lowercase is impossible in 8.3 without NTRes)
    v.add(root, "UPPER.TXT", pack_83("UPPER", "TXT"), 0, b"upper\n")
    cases.append({"case": "08-upper-with-lfn", "kind": "file",
                  "lfn": "UPPER.TXT", "short": "UPPER   TXT", "size": 6,
                  **content_fields(b"upper\n")})
    #  9 lowercase 8.3 via NTRes (no LFN; mdir prints lowercase)
    v.add(root, None, pack_83("LOWER", "TXT"), 0, b"ntres lower\n",
          nt_reserved=True)
    cases.append({"case": "09-ntres-lowercase", "kind": "file",
                  "lfn": None, "short": "LOWER   TXT",
                  "size": 12, "attr": "NTRes 0x18",
                  **content_fields(b"ntres lower\n")})

    # Subdirectories ------------------------------------------------------
    sub = v.new_dir(root)
    v.add(root, "projects", pack_83("PROJECTS", ""), ATTR_DIR, first=sub)
    cases.append({"case": "10-subdir", "kind": "dir", "lfn": "projects",
                  "short": "PROJECTS   "})
    # nested: two levels deep
    deep = v.new_dir(sub)
    v.add(sub, "2026 january notes", pack_83("2026~1", ""), ATTR_DIR,
          first=deep)
    cases.append({"case": "11-nested-dir", "kind": "dir",
                  "lfn": "2026 january notes", "short": "2026~1     "})
    v.add(deep, "nested file with a long name.txt", pack_83("NESTED~1", "TXT"),
          0, b"deep content\n")
    cases.append({"case": "12-nested-lfn-file", "kind": "file",
                  "lfn": "nested file with a long name.txt",
                  "short": "NESTED~1 TXT", "size": 13,
                  **content_fields(b"deep content\n")})
    # empty directory (only dot entries)
    empty = v.new_dir(root)
    v.add(root, "empty directory", pack_83("EMPTY~1", ""), ATTR_DIR,
          first=empty)
    cases.append({"case": "13-empty-dir", "kind": "dir",
                  "lfn": "empty directory", "short": "EMPTY~1    "})
    # directory with many entries: each file's name (30 chars) needs 3 LFN
    # slots, so 64 files occupy 64 * 4 * 32 = 8192 bytes of directory data
    # (+ 64 bytes of dot entries) = 8256 bytes -> a 17-cluster directory.
    many = v.new_dir(root, nbytes=64 + 64 * 4 * 32)
    v.add(root, "many entries", pack_83("MANYEN~1", ""), ATTR_DIR,
          first=many)
    for i in range(64):
        v.add(many, f"file-number-{i:02d}-with-padding.txt",
              pack_83(f"FILE~{i:03d}", "TXT"), 0, b"x")
    cases.append({"case": "14-many-entries", "kind": "dir",
                  "lfn": "many entries", "short": "MANYEN~1    ",
                  "files": 64})

    return v.finish(), cases


def main() -> int:
    # Default: the committed artifact locations. With a directory argument,
    # write there instead (the CI determinism check regenerates and compares).
    if len(sys.argv) > 1:
        img_path = Path(sys.argv[1]) / "fat32-corpus.img"
        man_path = Path(sys.argv[1]) / "fat32-corpus-manifest.json"
    else:
        img_path = Path("kernel/src/fat32_corpus.img")
        man_path = Path("tools/fat32-corpus-manifest.json")
    img_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.parent.mkdir(parents=True, exist_ok=True)
    img, cases = build_image()
    img_path.write_bytes(img)
    man = {"generator": "tools/gen-fat32-corpus.py",
           "spec": "Microsoft EFI FAT32 File System Specification EFISP01",
           "image": "kernel/src/fat32_corpus.img",
           "bytes": len(img),
           "cases": cases}
    man_path.write_text(
        json.dumps(man, indent=1) + "\n", encoding="utf-8")
    print(f"gen-fat32-corpus: wrote {img_path} ({len(img)} bytes), "
          f"{len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
