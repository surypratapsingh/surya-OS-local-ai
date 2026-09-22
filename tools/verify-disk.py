#!/usr/bin/env python3
"""Structurally verify build/nova.hdd — no external tools required.

Parses the image back the way firmware would:
  1. protective MBR (0xEE entry, 0x55AA signature)
  2. primary + backup GPT headers: signature, revision, LBAs, CRC32s
  3. partition entry: ESP type GUID, expected LBA range
  4. FAT16 BPB fields, identical FAT copies
  5. directory walk: /EFI/BOOT/{BOOTX64.EFI,BOOTIA32.EFI,LIMINE.CONF},
     /NUCLEUS, /LIMINE.SYS
  6. file contents byte-exact against the build inputs (kernel ELF, config,
     Limine binaries) via cluster-chain reads
  7. BIOS install footprint in the unallocated gap (LBA 34..2047)

Exit 0 = every check passed.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SECTOR = 512
PART_START_LBA = 2048
IMAGE_SECTORS = 81920
BACKUP_ENTRIES_LBA = IMAGE_SECTORS - 1 - 32  # must sit flush under the backup header
ESP_TYPE_GUID = bytes.fromhex("2832ac12f81fd211ba4b00a0c93ec93b")
BIOS_BOOT_GUID = b"Hah!IdontNeedEFI"

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "FAIL   ") + msg)
    if not cond:
        failures.append(msg)


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


class Fat16Reader:
    def __init__(self, part: bytes):
        self.part = part
        bpb = part[:SECTOR]
        self.bps = struct.unpack_from("<H", bpb, 11)[0]
        self.spc = bpb[13]
        self.reserved = struct.unpack_from("<H", bpb, 14)[0]
        self.nfats = bpb[16]
        self.root_entries = struct.unpack_from("<H", bpb, 17)[0]
        self.fat_sectors = struct.unpack_from("<H", bpb, 22)[0]
        root_sectors = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.fat0 = self.reserved * self.bps
        self.fat1 = self.fat0 + self.fat_sectors * self.bps
        self.root_off = self.fat1 + self.fat_sectors * self.bps
        self.data_off = self.root_off + root_sectors * self.bps

    def fat_val(self, cluster: int) -> int:
        return struct.unpack_from("<H", self.part, self.fat0 + cluster * 2)[0]

    def dir_entries(self, cluster: int):
        """Yield (name, attr, first_cluster, size) from root (0) or a dir.
        LFN slot runs are folded into the following short entry's name,
        reassembled by sequence number (on disk the last part comes first)."""
        if cluster == 0:
            region = self.part[self.root_off : self.root_off + self.root_entries * 32]
        else:
            off = self.data_off + (cluster - 2) * self.spc * self.bps
            region = self.part[off : off + self.spc * self.bps]
        lfn_parts: dict[int, bytes] = {}
        for i in range(0, len(region), 32):
            e = region[i : i + 32]
            if e[0] == 0x00:  # end of directory
                break
            if e[0] == 0xE5:  # deleted
                continue
            if e[11] & 0x3F == 0x0F:  # LFN slot: stash chunk by sequence no.
                seq = e[0] & 0x1F
                lfn_parts[seq] = e[1:11] + e[14:26] + e[28:32]
                continue
            name = e[0:8].decode("ascii").strip()
            ext = e[8:11].decode("ascii").strip()
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
            out += self.part[off : off + self.spc * self.bps]
            c = self.fat_val(c)
        if len(out) < size:
            raise ValueError(f"chain shorter than file size ({len(out)} < {size})")
        return bytes(out[:size])


def gpt_checks(img: bytes, lba: int, my: int, alt: int, ent_lba: int, tag: str) -> bytes:
    h = img[lba * SECTOR : lba * SECTOR + 92]
    check(h[0:8] == b"EFI PART", f"{tag}: signature")
    check(struct.unpack_from("<I", h, 8)[0] == 0x00010000, f"{tag}: revision 1.0")
    check(struct.unpack_from("<I", h, 12)[0] == 92, f"{tag}: header size 92")
    check(struct.unpack_from("<Q", h, 24)[0] == my, f"{tag}: MyLBA == {my}")
    check(struct.unpack_from("<Q", h, 32)[0] == alt, f"{tag}: AlternateLBA == {alt}")
    check(struct.unpack_from("<Q", h, 72)[0] == ent_lba, f"{tag}: partition entries LBA == {ent_lba}")
    stored = struct.unpack_from("<I", h, 16)[0]
    z = bytearray(h)
    z[16:20] = b"\x00" * 4
    check(crc32(bytes(z)) == stored, f"{tag}: header CRC32 valid")
    n = struct.unpack_from("<I", h, 80)[0]
    esz = struct.unpack_from("<I", h, 84)[0]
    entcrc = struct.unpack_from("<I", h, 88)[0]
    entries = img[ent_lba * SECTOR : ent_lba * SECTOR + n * esz]
    check(crc32(entries) == entcrc, f"{tag}: entries CRC32 valid")
    return entries  # full entry table


def main() -> int:
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/nova.hdd")
    kernel_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("kernel")
    limine_bin = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(".freebuff/ref/limine/limine-binary")
    elf = (
        Path(sys.argv[4])
        if len(sys.argv) > 4
        else kernel_dir / "target/x86_64-unknown-none/release/nucleus"
    )

    img = img_path.read_bytes()
    print(f"verify-disk: {img_path} ({len(img)} bytes)")
    check(len(img) == IMAGE_SECTORS * SECTOR, f"image size == {IMAGE_SECTORS * SECTOR}")

    # 1. Protective MBR.
    mbr = img[:SECTOR]
    check(mbr[510:512] == b"\x55\xAA", "MBR boot signature 0x55AA")
    check(mbr[446 + 4] == 0xEE, "protective MBR partition type 0xEE")

    # 2. GPT headers.
    prim = gpt_checks(img, 1, 1, IMAGE_SECTORS - 1, 2, "primary GPT")
    backup = gpt_checks(img, IMAGE_SECTORS - 1, IMAGE_SECTORS - 1, 1, BACKUP_ENTRIES_LBA, "backup GPT")
    check(prim == backup, "primary and backup partition entries identical")

    # 3. Partition entries: ESP + BIOS boot.
    esp_entry = prim[:128]
    bb_entry = prim[128:256]
    check(esp_entry[:16] == ESP_TYPE_GUID, "partition 1 type = EFI System Partition")
    first_lba, last_lba = struct.unpack_from("<QQ", esp_entry, 32)
    check(first_lba == PART_START_LBA, f"partition 1 starts at LBA {PART_START_LBA}")
    check(
        last_lba == BACKUP_ENTRIES_LBA - 1,
        f"partition 1 ends at LBA {BACKUP_ENTRIES_LBA - 1}",
    )
    check(bb_entry[:16] == BIOS_BOOT_GUID, "partition 2 type = BIOS boot (Hah!IdontNeedEFI)")
    bb_first, bb_last = struct.unpack_from("<QQ", bb_entry, 32)
    check(bb_first == 34 and bb_last == PART_START_LBA - 1,
          f"partition 2 spans LBA 34..{PART_START_LBA - 1}")
    esp = img[PART_START_LBA * SECTOR : (last_lba + 1) * SECTOR]

    # 4. FAT16 filesystem.
    fat = Fat16Reader(esp)
    check(fat.bps == SECTOR, f"FAT16 bytes/sector == {SECTOR}")
    check(fat.nfats == 2, "two FAT copies")
    fsz = fat.fat_sectors * SECTOR
    check(esp[fat.fat0 : fat.fat0 + fsz] == esp[fat.fat1 : fat.fat1 + fsz], "FAT copies identical")

    # 5. Directory walk.
    root = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(0)}
    for want in ("EFI", "NUCLEUS", "LIMINE", "LIMINE.CONF"):
        check(want in root, f"root contains /{want}")

    # 6. File contents byte-exact.
    expect_kernel = elf.read_bytes()
    c, s = root["NUCLEUS"]
    check(fat.read_file(c, s) == expect_kernel, f"/NUCLEUS byte-exact ({len(expect_kernel)} bytes)")
    expect_sys = (limine_bin / "limine-bios.sys").read_bytes()
    lim = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(root["LIMINE"][0])}
    check("LIMINE-BIOS.SYS" in lim, "/limine/ contains limine-bios.sys")
    c, s = lim["LIMINE-BIOS.SYS"]
    check(fat.read_file(c, s) == expect_sys, f"/limine/limine-bios.sys byte-exact ({len(expect_sys)} bytes)")

    efi_c = root["EFI"][0]
    efi = {n.upper(): c for n, a, c, s in fat.dir_entries(efi_c)}
    check("BOOT" in efi, "/EFI contains BOOT")
    boot = {n.upper(): (c, s) for n, a, c, s in fat.dir_entries(efi["BOOT"])}
    for want in ("BOOTX64.EFI", "BOOTIA32.EFI", "LIMINE.CONF"):
        check(want in boot, f"/EFI/BOOT contains {want}")
    expect_conf = (kernel_dir / "limine.conf").read_bytes()
    c, s = root["LIMINE.CONF"]
    check(fat.read_file(c, s) == expect_conf, "root /LIMINE.CONF byte-exact (BIOS config path)")
    c, s = boot["LIMINE.CONF"]
    check(fat.read_file(c, s) == expect_conf, "/EFI/BOOT/LIMINE.CONF byte-exact")
    for fname in ("BOOTX64.EFI", "BOOTIA32.EFI"):
        expect = (limine_bin / fname).read_bytes()
        c, s = boot[fname]
        check(fat.read_file(c, s) == expect, f"/EFI/BOOT/{fname} byte-exact ({len(expect)} bytes)")

    # 7. BIOS install footprint (stage 2 written into the unallocated gap).
    gap = img[34 * SECTOR : PART_START_LBA * SECTOR]
    check(any(b != 0 for b in gap), "BIOS boot stages present in unallocated gap (bios-install ran)")

    print()
    if failures:
        print(f"verify-disk: {len(failures)} check(s) FAILED")
        return 1
    print("verify-disk: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
