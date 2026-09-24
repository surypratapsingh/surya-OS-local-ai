#!/usr/bin/env python3
"""Structurally verify a NOVA boot image.

W2 rewrite: the GPT layer below is derived from the UEFI Specification,
version 2.10, chapter 5 (GUID Partition Table Format), with the section
cited per check — NOT from tools/make-esp.py. Builder and verifier no
longer share a GPT model, so a misconception they have in common (the
LastUsableLBA/backup-array overlap this work order was created for) is
now caught instead of mirrored.

Sections:
  1. protective MBR              [UEFI 2.10 §5.2]
  2. GPT headers + entry arrays  [§5.3.1, §5.3.2]
  3. partition entries           [§5.3.2, §5.3.3]
  4. FAT16 BPB + FAT copies      [builder layout; MS FAT16 semantics]
  5. directory walk, file contents byte-exact vs build inputs
  6. BIOS install footprint

Library API (used by tools/fuzz-disk.py):
    verify_image(img, expect) -> list[str]
        Failures only; never prints; never raises (fuzzed garbage is
        converted into failure reports, not exceptions).
    load_expectations(kernel_dir, limine_bin_dir, ...) -> Expect
    checked_ranges(img, expect) -> [(start, end, kind)]
        Fuzz coverage map: which byte offsets carry a verifier invariant.

Exit 0 = every check passed.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SECTOR = 512
PART_START_LBA = 2048
IMAGE_SECTORS = 81920  # 40 MiB
# Backup entry array: 32 sectors flush under the backup header at the last
# LBA. The verifier does not trust this constant — it re-derives the backup
# header's own location and checks the header's PartitionEntryLBA against
# the UEFI rules below.
BACKUP_ENTRIES_LBA = IMAGE_SECTORS - 1 - 32
ESP_TYPE_GUID = bytes.fromhex("2832ac12f81fd211ba4b00a0c93ec93b")
BIOS_BOOT_GUID = b"Hah!IdontNeedEFI"


def crc32(data) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


class Expect:
    """Build inputs that image contents are compared byte-exact against."""

    def __init__(self, kernel: bytes, limine_sys: bytes, bootx64: bytes,
                 bootia32: bytes, conf: bytes):
        self.kernel = kernel
        self.limine_sys = limine_sys
        self.bootx64 = bootx64
        self.bootia32 = bootia32
        self.conf = conf


def load_expectations(kernel_dir, limine_bin, kernel_elf=None, conf=None) -> Expect:
    kernel_dir = Path(kernel_dir)
    limine_bin = Path(limine_bin)
    elf = Path(kernel_elf) if kernel_elf else (
        kernel_dir / "target/x86_64-unknown-none/release/nucleus")
    conf = Path(conf) if conf else kernel_dir / "limine.conf"
    return Expect(
        kernel=elf.read_bytes(),
        limine_sys=(limine_bin / "limine-bios.sys").read_bytes(),
        bootx64=(limine_bin / "BOOTX64.EFI").read_bytes(),
        bootia32=(limine_bin / "BOOTIA32.EFI").read_bytes(),
        conf=conf.read_bytes(),
    )


class Fat16Reader:
    """Minimal FAT16 reader over one partition image (bytes or memoryview)."""

    def __init__(self, part):
        self.part = part
        bpb = part[:SECTOR]
        self.bps = struct.unpack_from("<H", bpb, 11)[0]
        self.spc = bpb[13]
        self.reserved = struct.unpack_from("<H", bpb, 14)[0]
        self.nfats = bpb[16]
        self.root_entries = struct.unpack_from("<H", bpb, 17)[0]
        self.fat_sectors = struct.unpack_from("<H", bpb, 22)[0]
        if not (self.bps and self.spc and self.reserved and self.nfats
                and self.root_entries and self.fat_sectors):
            raise ValueError("absurd BPB field (zero bps/spc/reserved/nfats/root/fat)")
        root_sectors = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.fat0 = self.reserved * self.bps
        self.fat1 = self.fat0 + self.fat_sectors * self.bps
        self.root_off = self.fat1 + self.fat_sectors * self.bps
        self.data_off = self.root_off + root_sectors * self.bps
        self.clusters = (len(part) - self.data_off) // (self.spc * self.bps)
        if self.data_off >= len(part):
            raise ValueError("FAT structures do not fit in the partition")

    def fat_val(self, cluster: int) -> int:
        return struct.unpack_from("<H", self.part, self.fat0 + cluster * 2)[0]

    def dir_entries(self, cluster: int):
        """Yield (name, attr, first_cluster, size) from root (0) or a dir.
        LFN slot runs are folded into the following short entry's name."""
        if cluster == 0:
            region = self.part[self.root_off:self.root_off + self.root_entries * 32]
        else:
            off = self.data_off + (cluster - 2) * self.spc * self.bps
            region = self.part[off:off + self.spc * self.bps]
        lfn_parts: dict[int, bytes] = {}
        for i in range(0, len(region), 32):
            e = region[i:i + 32]
            if len(e) < 32:
                break
            if e[0] == 0x00:  # end of directory
                break
            if e[0] == 0xE5:  # deleted
                continue
            if e[11] & 0x3F == 0x0F:  # LFN slot: stash chunk by sequence no.
                seq = e[0] & 0x1F
                lfn_parts[seq] = bytes(e[1:11]) + bytes(e[14:26]) + bytes(e[28:32])
                continue
            name = bytes(e[0:8]).decode("ascii", errors="ignore").strip()
            ext = bytes(e[8:11]).decode("ascii", errors="ignore").strip()
            full = name + ("." + ext if ext else "")
            if lfn_parts:
                raw = b"".join(lfn_parts[k] for k in sorted(lfn_parts))
                long_name = raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
                if long_name:
                    full = long_name
                lfn_parts = {}
            first = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            yield full, e[11], first, size

    def read_file(self, first: int, size: int) -> bytes:
        out = bytearray()
        c, seen = first, set()
        while 2 <= c < 0xFFF0:
            if c in seen:
                raise ValueError("cluster chain loop")
            seen.add(c)
            off = self.data_off + (c - 2) * self.spc * self.bps
            if off + self.spc * self.bps > len(self.part):
                raise ValueError("cluster chain runs past end of partition")
            out += self.part[off:off + self.spc * self.bps]
            c = self.fat_val(c)
        if len(out) < size:
            raise ValueError(f"chain shorter than file size ({len(out)} < {size})")
        return bytes(out[:size])


class Verifier:
    """Runs all sections; collects failure strings. Any exception a fuzzed
    input can provoke inside a section is converted into a failure report —
    the fuzzer requires 'reports a failure', so crashing is a defect."""

    def __init__(self, img, expect: Expect, verbose: bool = False):
        self.img = img
        self.expect = expect
        self.verbose = verbose
        self.fails: list[str] = []
        self.last_lba = len(img) // SECTOR - 1
        self.fat: Fat16Reader | None = None
        self.prim_entries = None
        self.bak_entries = None
        self.p: dict | None = None
        self.b: dict | None = None

    def check(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.fails.append(msg)
        if self.verbose:
            print(("  ok   " if cond else "FAIL   ") + msg)
        return cond

    # ------------------------------------------------------------------
    def run(self) -> list[str]:
        self.check(len(self.img) == IMAGE_SECTORS * SECTOR,
                   f"image size == {IMAGE_SECTORS * SECTOR}")
        # Partition slice: layout facts for addressing only (which LBA range
        # holds the FAT). Everything the slice *contains* is checked below.
        self.esp = memoryview(self.img)[PART_START_LBA * SECTOR:BACKUP_ENTRIES_LBA * SECTOR]
        sections = (
            ("protective MBR", self._sec_mbr),
            ("GPT headers and entry arrays", self._sec_gpt),
            ("partition entries", self._sec_entries),
            ("FAT16 filesystem", self._sec_fat),
            ("directory contents vs build inputs", self._sec_files),
            ("BIOS install footprint", self._sec_stages),
        )
        for name, fn in sections:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — fuzzed input may be anything
                self.fails.append(f"{name}: unexpected exception ({type(e).__name__}: {e})")
        return self.fails

    # -- 1. protective MBR ---------------------------------------------
    def _sec_mbr(self) -> None:
        mbr = self.img[0:SECTOR]
        # [UEFI 2.10 §5.2 (Protective MBR layout): boot signature]
        self.check(mbr[510:512] == b"\x55\xaa", "protective MBR: boot signature 0x55AA")
        # [UEFI 2.10 §5.2.3 (Protective MBR Partition Record): type 0xEE]
        self.check(mbr[446 + 4] == 0xEE, "protective MBR: partition type 0xEE")
        self.check(any(b != 0 for b in mbr[0:440]),
                   "MBR stage-1 boot code present (bios-install ran)")

    # -- 2. GPT headers + entry arrays ----------------------------------
    def _header(self, lba: int, tag: str) -> dict | None:
        """Parse one GPT header with per-field checks [UEFI 2.10 §5.3.2]."""
        if not (0 <= lba <= self.last_lba):
            self.check(False, f"{tag}: header LBA {lba} out of range (image has LBAs 0..{self.last_lba})")
            return None
        h = self.img[lba * SECTOR:lba * SECTOR + 92]
        if len(h) < 92:
            self.check(False, f"{tag}: header truncated ({len(h)} < 92 bytes)")
            return None
        self.check(h[0:8] == b"EFI PART", f"{tag}: signature 'EFI PART' [§5.3.2]")
        self.check(struct.unpack_from("<I", h, 8)[0] == 0x00010000,
                   f"{tag}: revision 1.0 [§5.3.2]")
        self.check(struct.unpack_from("<I", h, 12)[0] == 92, f"{tag}: header size == 92 [§5.3.2]")
        stored = struct.unpack_from("<I", h, 16)[0]
        z = bytearray(h)
        z[16:20] = b"\x00" * 4
        self.check(crc32(z) == stored, f"{tag}: header CRC32 valid [§5.3.2]")
        my, alt, first, last = struct.unpack_from("<QQQQ", h, 24)
        # MyLBA is "the LBA that contains this data structure" [§5.3.2].
        self.check(my == lba, f"{tag}: MyLBA ({my}) == the LBA it lives at ({lba}) [§5.3.2]")
        return dict(my=my, alt=alt, first=first, last=last,
                    disk_guid=h[56:72], ent_lba=struct.unpack_from("<Q", h, 72)[0],
                    n=struct.unpack_from("<I", h, 80)[0],
                    esz=struct.unpack_from("<I", h, 84)[0],
                    ent_crc=struct.unpack_from("<I", h, 88)[0])

    def _sec_gpt(self) -> None:
        LAST = self.last_lba  # re-derived from the actual image, not the builder
        p = self._header(1, "primary GPT")
        # [UEFI 2.10 §5.3.2: "backup GPT Header must be located in the last
        #  LBA of the device."] If that check fails, _header reports it.
        b = self._header(LAST, "backup GPT")
        if p is None or b is None:
            return
        self.p, self.b = p, b
        self.check(p["alt"] == LAST and b["alt"] == 1,
                   "headers point at each other: primary AlternateLBA == last LBA, backup == 1 [§5.3.2]")
        self.check(p["disk_guid"] == b["disk_guid"], "disk GUID identical in both headers [§5.3.2]")

        first_usable, last_usable = p["first"], p["last"]
        # [§5.3.1: "A minimum of 16,384 bytes of space must be reserved for
        #  the GPT Partition Entry Array."] With 512 B sectors that is 32
        # sectors, after MBR + header → FirstUsableLBA >= 34.
        self.check(first_usable >= 34,
                   f"FirstUsableLBA ({first_usable}) >= 34 [§5.3.1]")
        self.check(first_usable <= last_usable,
                   f"FirstUsableLBA ({first_usable}) <= LastUsableLBA ({last_usable}) [§5.3.2]")

        n, esz = p["n"], p["esz"]
        self.check(n >= 128,
                   f"NumberOfPartitionEntries ({n}) >= 128 [§5.3.1: min 16,384 B array]")
        # [§5.3.2 SizeOfPartitionEntry: "Must be a multiple of 8."; 128 B is
        #  the entry layout of §5.3.3]
        self.check(esz >= 128 and esz % 8 == 0,
                   f"SizeOfPartitionEntry ({esz}) >= 128 and a multiple of 8 [§5.3.2, §5.3.3]")

        arr_sectors = (n * esz + SECTOR - 1) // SECTOR
        # Primary array: after the primary header, before FirstUsableLBA
        # [§5.3.1]. Primary header sits at LBA 1, so >= LBA 2.
        self.check(p["ent_lba"] >= 2 and p["ent_lba"] + arr_sectors - 1 < first_usable,
                   f"primary entry array (LBA {p['ent_lba']}..{p['ent_lba'] + arr_sectors - 1}) "
                   f"between primary header and FirstUsableLBA ({first_usable}) [§5.3.1]")

        # Backup array: after LastUsableLBA, ending before the backup header
        # [§5.3.1]. These two checks would have caught the W2 defect this
        # work order was written for (array at 81791 < LastUsableLBA 81886).
        self.check(b["ent_lba"] > last_usable,
                   f"backup entry array LBA ({b['ent_lba']}) > LastUsableLBA ({last_usable}) "
                   "[UEFI 2.10 §5.3.1: array after the Last Usable LBA]  ** W2-mandated check **")
        self.check(b["ent_lba"] + arr_sectors - 1 <= LAST - 1,
                   f"backup entry array ends (LBA {b['ent_lba'] + arr_sectors - 1}) "
                   f"before the backup header (LBA {LAST}) [§5.3.1]")

        self.prim_entries = self._entries_slice(p, "primary")
        self.bak_entries = self._entries_slice(b, "backup")
        if self.prim_entries is not None and self.bak_entries is not None:
            self.check(self.prim_entries == self.bak_entries,
                       "primary and backup partition entries identical [§5.3.1: backup is a copy]")

    def _entries_slice(self, h: dict, tag: str):
        n, esz = h["n"], h["esz"]
        if not (0 < n <= 1_048_576 and 0 < esz <= 65536):
            self.check(False, f"{tag}: absurd entry geometry (n={n}, esz={esz})")
            return None
        total = n * esz
        if total > IMAGE_SECTORS * SECTOR:
            self.check(False, f"{tag}: entry array ({total} B) larger than the image")
            return None
        start = h["ent_lba"] * SECTOR
        entries = self.img[start:start + total]
        if len(entries) < total:
            self.check(False, f"{tag}: entry array truncated ({len(entries)} < {total} B)")
            return None
        self.check(crc32(entries) == h["ent_crc"], f"{tag}: entry array CRC32 valid [§5.3.2]")
        return entries

    # -- 3. partition entries -------------------------------------------
    def _sec_entries(self) -> None:
        if self.p is None or self.prim_entries is None:
            self.fails.append("partition entries: skipped (GPT header section failed)")
            return
        p, esz = self.p, self.p["esz"]
        first_usable, last_usable = p["first"], p["last"]
        declared = []
        for i in range(p["n"]):
            e = self.prim_entries[i * esz:i * esz + 128]
            if len(e) < 128:
                self.check(False, f"partition entry {i}: truncated")
                continue
            type_guid = e[0:16]
            if type_guid == b"\x00" * 16:
                continue
            # [UEFI 2.10 §5.3.3: type GUID, unique GUID, StartingLBA/EndingLBA
            #  (EndingLBA is inclusive), attributes, name]
            first, last = struct.unpack_from("<QQ", e, 32)
            # [§5.3.2 LastUsableLBA: "The last logical block that may be used
            #  by a partition described by this GUID Partition Table" — so
            #  every declared partition must end at or before it.]
            #  ** W2-mandated check: LastUsableLBA >= end of every partition **
            ok = first <= last and first >= first_usable and last <= last_usable
            self.check(ok, f"partition {i}: LBA {first}..{last} within usable "
                           f"{first_usable}..{last_usable}, end <= start [§5.3.2/§5.3.3]")
            declared.append((first, last, i, type_guid))
        self.check(len(declared) >= 1, "at least one declared partition")
        # Layout: exactly our ESP + BIOS-boot pair, identified by TYPE GUID,
        # not by position or start-LBA order. (An earlier draft sorted by
        # StartingLBA and then asserted "entry 0 = ESP" — that relabels the
        # partitions whenever the BIOS-boot partition is declared first, and
        # reported four false failures on a correct image.)
        self.check(len(declared) == 2, f"exactly 2 declared partitions (found {len(declared)}) [layout]")
        if len(declared) == 2:
            by_type = {t: (f, l, i) for f, l, i, t in declared}
            if ESP_TYPE_GUID in by_type and BIOS_BOOT_GUID in by_type:
                fe, le, ie = by_type[ESP_TYPE_GUID]
                fb, lb, ib = by_type[BIOS_BOOT_GUID]
                self.check(le < fb or lb < fe,
                           f"declared partitions are disjoint (ESP {fe}..{le}, "
                           f"BIOS-boot {fb}..{lb}) [§5.3.3: partitions do not overlap]")
                self.check(fe == PART_START_LBA and le == BACKUP_ENTRIES_LBA - 1,
                           f"ESP spans LBA {PART_START_LBA}..{BACKUP_ENTRIES_LBA - 1} [layout]")
                self.check(fb == 34 and lb == PART_START_LBA - 1,
                           f"BIOS-boot partition spans LBA 34..{PART_START_LBA - 1} [layout]")
            else:
                self.check(False, "partition types: expected one ESP + one BIOS boot "
                                  f"(found type GUIDs {sorted(t.hex() for _f, _l, _i, t in declared)})")
        else:
            for first, last, i, _t in declared:
                self.check(first == PART_START_LBA and last == BACKUP_ENTRIES_LBA - 1,
                           f"partition {i}: expected ESP span LBA "
                           f"{PART_START_LBA}..{BACKUP_ENTRIES_LBA - 1} [layout]")

    # -- 4. FAT16 filesystem ---------------------------------------------
    def _sec_fat(self) -> None:
        try:
            fat = Fat16Reader(self.esp)
        except Exception as e:  # noqa: BLE001 — fuzzed BPB may be garbage
            self.fails.append(f"FAT16: BPB unusable ({type(e).__name__}: {e})")
            return
        self.fat = fat
        self.check(fat.bps == SECTOR, f"FAT16 bytes/sector == {SECTOR} [layout]")
        self.check(fat.nfats == 2, "FAT16 has two FAT copies [layout]")
        fsz = fat.fat_sectors * SECTOR
        self.check(self.esp[fat.fat0:fat.fat0 + fsz] == self.esp[fat.fat1:fat.fat1 + fsz],
                   "FAT copies identical")

    # -- 5. directory contents vs build inputs ---------------------------
    def _read_at(self, fat: Fat16Reader, where: dict, name: str, expect: bytes, label: str) -> None:
        if name not in where:
            self.check(False, f"{label}: /{name} present")
            return
        c, s = where[name]
        if s != len(expect):
            self.check(False, f"{label}: /{name} size {s} == build input {len(expect)}")
            return
        try:
            data = fat.read_file(c, s)
        except Exception as e:  # noqa: BLE001
            self.check(False, f"{label}: /{name} unreadable ({type(e).__name__}: {e})")
            return
        self.check(data == expect, f"{label}: /{name} byte-exact ({len(expect)} bytes)")

    def _sec_files(self) -> None:
        fat = self.fat
        if fat is None:
            self.fails.append("directory contents: skipped (FAT16 section failed)")
            return
        root = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(0)}
        for want in ("EFI", "NUCLEUS", "LIMINE", "LIMINE.CONF"):
            self.check(want in root, f"root contains /{want}")
        self._read_at(fat, root, "NUCLEUS", self.expect.kernel, "root")
        self._read_at(fat, root, "LIMINE.CONF", self.expect.conf, "root (BIOS config path)")
        if "LIMINE" in root:
            lim = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(root["LIMINE"][0])}
            self.check("LIMINE-BIOS.SYS" in lim, "/limine/ contains limine-bios.sys")
            self._read_at(fat, lim, "LIMINE-BIOS.SYS", self.expect.limine_sys, "/limine/")
        if "EFI" in root:
            efi = {n.upper(): c for n, a, c, s in fat.dir_entries(root["EFI"][0])}
            self.check("BOOT" in efi, "/EFI contains BOOT")
            if "BOOT" in efi:
                boot = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(efi["BOOT"])}
                self._read_at(fat, boot, "BOOTX64.EFI", self.expect.bootx64, "/EFI/BOOT")
                self._read_at(fat, boot, "BOOTIA32.EFI", self.expect.bootia32, "/EFI/BOOT")
                self._read_at(fat, boot, "LIMINE.CONF", self.expect.conf, "/EFI/BOOT")

    # -- 6. BIOS install footprint ----------------------------------------
    def _sec_stages(self) -> None:
        gap = self.img[34 * SECTOR:PART_START_LBA * SECTOR]
        self.check(any(b != 0 for b in gap),
                   "BIOS boot stages present in the BIOS-boot partition (bios-install ran)")


def verify_image(img, expect: Expect, verbose: bool = False) -> list[str]:
    """Verify one image; returns failure messages. Never prints (unless
    verbose) and never raises on malformed input."""
    return Verifier(img, expect, verbose=verbose).run()


def checked_ranges(img, expect: Expect) -> list[tuple[int, int, str]]:
    """Fuzz coverage map: (start, end, kind) byte ranges carrying a verifier
    invariant.

      exact    — the verifier byte-checks the range; ANY single-byte mutation
                 must be reported as a failure (CRCs, identity compares,
                 byte-exact file compares, checked fields).
      presence — only non-zeroness is asserted; a mutation to another
                 non-zero value may legitimately pass.

    Bytes not in any range have no invariant (free space, unused FAT
    slots, padding) — the builder zeroes them for determinism, but the
    verifier deliberately does not check them, so a mutation there is not
    a verifier defect. tools/fuzz-disk.py consumes this map and
    cross-validates it with targeted single-byte mutations.
    """
    last = len(img) // SECTOR - 1
    out: list[tuple[int, int, str]] = []

    def add(a: int, b: int, kind: str) -> None:
        if a < b:
            out.append((a, b, kind))

    # Protective MBR: checked type byte + signature; boot code presence-only.
    add(450, 451, "exact")
    add(510, 512, "exact")
    add(0, 440, "presence")
    # GPT: headers are CRC-protected (any byte), entry arrays are CRC-protected.
    add(SECTOR, SECTOR + 92, "exact")
    add(2 * SECTOR, 2 * SECTOR + 128 * 128, "exact")
    add((IMAGE_SECTORS - 33) * SECTOR, (IMAGE_SECTORS - 33) * SECTOR + 128 * 128, "exact")
    add((IMAGE_SECTORS - 1) * SECTOR, IMAGE_SECTORS * SECTOR, "exact")
    # BIOS-boot partition: stages presence-checked only.
    add(34 * SECTOR, PART_START_LBA * SECTOR, "presence")

    try:
        esp = memoryview(img)[PART_START_LBA * SECTOR:BACKUP_ENTRIES_LBA * SECTOR]
        fat = Fat16Reader(esp)
        p0 = PART_START_LBA * SECTOR
        # BPB fields the verifier asserts on. (root_entries/total/media are
        # NOT byte-checked → deliberately not marked.)
        add(p0 + 11, p0 + 16, "exact")    # bytes/sector, spc, reserved
        add(p0 + 16, p0 + 17, "exact")    # number of FATs (== 2 is asserted)
        add(p0 + 22, p0 + 24, "exact")    # sectors/FAT
        add(p0 + 510, p0 + 512, "exact")  # boot signature
        # FAT copies: the identity check covers every byte of both.
        add(p0 + fat.fat0, p0 + fat.fat0 + fat.fat_sectors * fat.bps, "exact")
        add(p0 + fat.fat1, p0 + fat.fat1 + fat.fat_sectors * fat.bps, "exact")

        def extents(first: int, size: int) -> list[tuple[int, int]]:
            spans = []
            c, seen = first, 0
            while 2 <= c < 0xFFF0 and seen <= fat.clusters:
                off = p0 + fat.data_off + (c - 2) * fat.spc * fat.bps
                spans.append((off, off + fat.spc * fat.bps))
                c = fat.fat_val(c)
                seen += 1
            spans.sort()
            merged = []
            for a, b in spans:
                if merged and a <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            return merged

        def mark_file(where: dict, name: str) -> None:
            hit = where.get(name)
            if hit:
                for a, b in extents(hit[0], hit[1]):
                    add(a, b, "exact")

        root = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(0)}
        mark_file(root, "NUCLEUS")
        mark_file(root, "LIMINE.CONF")
        if "LIMINE" in root:
            lim = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(root["LIMINE"][0])}
            mark_file(lim, "LIMINE-BIOS.SYS")
        if "EFI" in root:
            efi = {n.upper(): c for n, a, c, s in fat.dir_entries(root["EFI"][0])}
            if "BOOT" in efi:
                boot = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(efi["BOOT"])}
                for f in ("BOOTX64.EFI", "BOOTIA32.EFI", "LIMINE.CONF"):
                    mark_file(boot, f)
    except Exception:  # noqa: BLE001 — map is advisory; GPT/MBR ranges still valid
        pass
    out.sort()
    return out


def main() -> int:
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/nova.hdd")
    kernel_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("kernel")
    limine_bin = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".freebuff/ref/limine/limine-binary")
    elf = (Path(sys.argv[4]) if len(sys.argv) > 4 else
           kernel_dir / "target/x86_64-unknown-none/release/nucleus")

    expect = load_expectations(kernel_dir, limine_bin, kernel_elf=elf)
    img = img_path.read_bytes()
    print(f"verify-disk: {img_path} ({len(img)} bytes)")
    failures = verify_image(img, expect, verbose=True)
    print()
    if failures:
        print(f"verify-disk: {len(failures)} check(s) FAILED")
        return 1
    print("verify-disk: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
