#!/usr/bin/env python3
"""Build nova.hdd — a raw bootable disk image for the Nucleus kernel.

Produces a GPT disk with one EFI System Partition formatted FAT16, containing:
  /EFI/BOOT/BOOTX64.EFI, /EFI/BOOT/BOOTIA32.EFI   (Limine UEFI loaders)
  /EFI/BOOT/LIMINE.CONF                            (boot menu)
  /NUCLEUS                                         (the kernel ELF)

Pure Python 3 stdlib, deterministic output (fixed GUIDs/volume id). After
this, `limine-tool bios-install nova.hdd` patches the MBR for BIOS boot
(run by scripts/build-disk.sh so this file stays tool-free).

Layout (512B sectors):
  LBA0              protective MBR
  LBA1              primary GPT header
  LBA2..33          GPT partition entries (128 x 128B)
  LBA34..2047       BIOS boot partition (Limine BIOS stages)
  LBA2048..81886    ESP (FAT16) — last usable LBA is 81886
  LBA81887..81918   backup GPT partition entries (32 sectors)
  LBA81919          backup GPT header
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SECTOR = 512
PART_START_LBA = 2048
IMAGE_SECTORS = 81920  # 40 MiB
BACKUP_ENTRIES_LBA = IMAGE_SECTORS - 1 - 32  # 81887: 32 entry sectors, flush
                                             # against the backup header at 81919
ESP_SECTORS = BACKUP_ENTRIES_LBA - PART_START_LBA  # 79839

# EFI System Partition type GUID + our fixed partition/disk GUIDs.
ESP_TYPE_GUID = bytes.fromhex("2832ac12f81fd211ba4b00a0c93ec93b")
DISK_GUID = bytes.fromhex("101a7c6ded5e0b4a9a5a4e4f56410001")
PART_GUID = bytes.fromhex("101a7c6ded5e0b4a9a5a4e4f56410002")
PART2_GUID = bytes.fromhex("101a7c6ded5e0b4a9a5a4e4f56410003")
# Standard BIOS boot partition type GUID (spells "Hah!IdontNeedEFI").
BIOS_BOOT_GUID = b"Hah!IdontNeedEFI"
VOLUME_ID = 0x4E4F5641  # "NOVA"


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


class Fat16:
    """Minimal FAT16 writer: 8.3 names, dirs, clustered files. Readable by
    every firmware and by Limine."""

    def __init__(self, total_sectors: int):
        self.spc = 4  # sectors per cluster (2 KiB)
        self.reserved = 1
        self.fats = 2
        self.root_entries = 512
        root_sectors = (self.root_entries * 32 + SECTOR - 1) // SECTOR
        fat_sectors = 8
        clusters = 0
        for _ in range(6):
            data_sectors = total_sectors - self.reserved - self.fats * fat_sectors - root_sectors
            clusters = data_sectors // self.spc
            fat_sectors = ((clusters + 2) * 2 + SECTOR - 1) // SECTOR
        self.fat_sectors = fat_sectors
        self.root_sectors = root_sectors
        self.total_sectors = total_sectors
        self.clusters = clusters
        self.data_sectors = data_sectors
        self.next_cluster = 2
        self.fat = bytearray(fat_sectors * SECTOR)
        self.data = bytearray(data_sectors * SECTOR)
        self.root = bytearray(root_sectors * SECTOR)
        struct.pack_into("<H", self.fat, 0, 0xFFF8)  # media descriptor
        struct.pack_into("<H", self.fat, 2, 0xFFFF)  # EOC marker

    def _alloc(self, size: int) -> int:
        """Allocate a cluster chain; returns the first cluster."""
        n = max(1, (size + self.spc * SECTOR - 1) // (self.spc * SECTOR))
        first = self.next_cluster
        for i in range(n):
            c = self.next_cluster
            self.next_cluster += 1
            if c + 1 > self.clusters + 1:
                raise RuntimeError("FAT16: out of clusters")
            val = 0xFFFF if i == n - 1 else c + 1
            struct.pack_into("<H", self.fat, c * 2, val)
        return first

    def _write_cluster(self, first: int, buf: bytes) -> None:
        off = (first - 2) * self.spc * SECTOR
        self.data[off : off + len(buf)] = buf

    @staticmethod
    def sfn(name: str) -> bytes:
        """Short 8.3 name, uppercase, space padded. Names that don't fit
        (e.g. LIMINE.CONF) collapse to LFN-backed 8.3: LIMINE~1.CON."""
        base, _, ext = name.partition(".")
        if len(base) > 8 or len(ext) > 3:
            base = base[:6] + "~1"
            ext = ext[:3]
        return base.upper().ljust(8).encode() + ext.upper().ljust(3).encode()

    @staticmethod
    def _sfn_checksum(short11: bytes) -> int:
        s = 0
        for b in short11:
            s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
        return s

    @classmethod
    def _needs_lfn(cls, name: str) -> bool:
        base, _, ext = name.partition(".")
        return len(base) > 8 or len(ext) > 3

    @classmethod
    def _lfn_entries(cls, name: str, short11: bytes) -> list[bytes]:
        """LFN directory slots for a name, in on-disk order (they sit
        immediately before the short entry, last segment first)."""
        per = 13
        n = (len(name) + per - 1) // per
        out = []
        for i in range(n, 0, -1):
            seg = name[(i - 1) * per : i * per]
            buf = seg.encode("utf-16-le") + b"\x00\x00"
            buf += b"\xff" * (26 - len(buf))
            e = bytearray(32)
            e[0] = (0x40 | i) if i == n else i
            e[1:11] = buf[0:10]
            e[11] = 0x0F
            e[13] = cls._sfn_checksum(short11)
            e[14:26] = buf[10:22]
            e[28:32] = buf[22:26]
            out.append(bytes(e))
        return out

    @staticmethod
    def _entry(name: str, first: int, size: int, attr: int) -> bytes:
        # FAT directory entry: name(11) attr(1) ntres(1) crtTenth(1)
        # crtTime(2) crtDate(2) accDate(2) clusHI(2) wrtTime(2) wrtDate(2)
        # clusLO(2) size(4) = 32 bytes. Dates fixed for determinism.
        e = bytearray(32)
        e[0:11] = Fat16.sfn(name)
        e[11] = attr
        struct.pack_into("<H", e, 14, 0x6D60)  # CrtTime 13:11
        struct.pack_into("<H", e, 16, 0x5321)  # CrtDate 2024-05-01
        struct.pack_into("<H", e, 18, 0x5321)  # LstAccDate
        struct.pack_into("<H", e, 20, (first >> 16) & 0xFFFF)  # FstClusHI
        struct.pack_into("<H", e, 22, 0x6D60)  # WrtTime
        struct.pack_into("<H", e, 24, 0x5321)  # WrtDate
        struct.pack_into("<H", e, 26, first & 0xFFFF)          # FstClusLO
        struct.pack_into("<I", e, 28, size)
        return bytes(e)

    def add_file(self, dirbuf: bytearray, slot: int, name: str, content: bytes) -> None:
        short11 = self.sfn(name)
        first = self._alloc(len(content))
        buf = bytearray(self.spc * SECTOR * ((len(content) + self.spc * SECTOR - 1) // (self.spc * SECTOR)))
        buf[: len(content)] = content
        self._write_cluster(first, bytes(buf))
        if self._needs_lfn(name):
            lfns = self._lfn_entries(name, short11)
            assert slot >= len(lfns), "no room for LFN entries before short slot"
            start = slot - len(lfns)
            if any(dirbuf[start * 32 : slot * 32]):
                raise RuntimeError(
                    f"LFN slot collision for {name}: slots {start}..{slot - 1} not free"
                )
            for k, lfn in enumerate(lfns):
                dirbuf[(slot - len(lfns) + k) * 32 : (slot - len(lfns) + k) * 32 + 32] = lfn
        dirbuf[slot * 32 : slot * 32 + 32] = self._entry(name, first, len(content), 0x20)

    def add_dir(self, parentbuf: bytearray, slot: int, name: str, parent_cluster: int) -> int:
        """Create a directory in `parentbuf`; returns its first cluster."""
        first = self._alloc(self.spc * SECTOR)
        buf = bytearray(self.spc * SECTOR)
        buf[0:32] = self._entry(".", first, 0, 0x10)
        buf[32:64] = self._entry("..", parent_cluster, 0, 0x10)
        self._write_cluster(first, bytes(buf))
        parentbuf[slot * 32 : slot * 32 + 32] = self._entry(name, first, 0, 0x10)
        return first

    def image_bytes(self) -> bytes:
        bpb = bytearray(SECTOR)
        bpb[0:3] = b"\xEB\x3C\x90"
        bpb[3:11] = b"NOVAESP "  # OEM label (exactly 8 bytes)
        struct.pack_into("<H", bpb, 11, SECTOR)
        bpb[13] = self.spc
        struct.pack_into("<H", bpb, 14, self.reserved)
        bpb[16] = self.fats
        struct.pack_into("<H", bpb, 17, self.root_entries)
        total = self.total_sectors
        struct.pack_into("<H", bpb, 19, total if total < 0x10000 else 0)
        bpb[21] = 0xF8
        struct.pack_into("<H", bpb, 22, self.fat_sectors)
        struct.pack_into("<H", bpb, 24, 63)   # sectors/track
        struct.pack_into("<H", bpb, 26, 255)  # heads
        struct.pack_into("<I", bpb, 28, PART_START_LBA)  # hidden sectors
        struct.pack_into("<I", bpb, 32, total)  # total sectors 32-bit
        bpb[36] = 0x80                          # BIOS drive number
        bpb[38] = 0x29                          # extended boot signature
        struct.pack_into("<I", bpb, 39, VOLUME_ID)
        bpb[43:54] = b"NOVAESP    "
        bpb[54:62] = b"FAT16   "
        bpb[510:512] = b"\x55\xAA"
        return bytes(bpb) + bytes(self.fat) * self.fats + bytes(self.root) + bytes(self.data)


def build_gpt_image(esp_fat: bytes) -> bytes:
    img = bytearray(IMAGE_SECTORS * SECTOR)

    # Protective MBR (one 0xEE partition covering the disk).
    mbr = bytearray(SECTOR)
    mbr[446:462] = (
        bytes([0x00]) + bytes(3) + bytes([0xEE]) + bytes(3)
        + struct.pack("<II", 1, min(IMAGE_SECTORS - 1, 0xFFFFFFFF))
    )
    mbr[510:512] = b"\x55\xAA"
    img[0:SECTOR] = mbr

    # Partition entry table (128 x 128B).
    entries = bytearray(128 * 128)
    last_lba = PART_START_LBA + len(esp_fat) // SECTOR - 1

    def part_entry(type_guid: bytes, part_guid: bytes, start: int, end: int, name: str) -> bytes:
        e = type_guid + part_guid
        e += struct.pack("<QQ", start, end)
        e += struct.pack("<Q", 0)  # attributes
        utf16 = name.encode("utf-16-le") + b"\x00\x00"
        e += utf16 + b"\x00" * (72 - len(utf16))  # name field is 72 bytes
        assert len(e) == 128
        return e

    # 1: the ESP. 2: BIOS boot partition (the LBA 34..2047 gap) so
    # `limine bios-install` has somewhere to put its BIOS stages.
    entries[0:128] = part_entry(ESP_TYPE_GUID, PART_GUID, PART_START_LBA, last_lba, "ESP")
    entries[128:256] = part_entry(BIOS_BOOT_GUID, PART2_GUID, 34, PART_START_LBA - 1, "BIOSBOOT")

    def gpt_header(my_lba: int, alt_lba: int, entries_lba: int) -> bytearray:
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<I", h, 8, 0x00010000)   # revision 1.0
        struct.pack_into("<I", h, 12, 92)          # header size
        struct.pack_into("<Q", h, 24, my_lba)
        struct.pack_into("<Q", h, 32, alt_lba)
        struct.pack_into("<Q", h, 40, 34)          # first usable
        struct.pack_into("<Q", h, 48, IMAGE_SECTORS - 34)  # last usable
        h[56:72] = DISK_GUID
        struct.pack_into("<Q", h, 72, entries_lba)
        struct.pack_into("<I", h, 80, 128)         # entry count
        struct.pack_into("<I", h, 84, 128)         # entry size
        struct.pack_into("<I", h, 88, crc32(bytes(entries)))
        struct.pack_into("<I", h, 16, crc32(bytes(h)))
        return h

    img[SECTOR : SECTOR + 92] = gpt_header(1, IMAGE_SECTORS - 1, 2)
    img[2 * SECTOR : 2 * SECTOR + len(entries)] = entries

    # Backup GPT at the end of the disk.
    be_lba = BACKUP_ENTRIES_LBA
    img[be_lba * SECTOR : be_lba * SECTOR + len(entries)] = entries
    bh = bytes(gpt_header(IMAGE_SECTORS - 1, 1, be_lba))
    img[(IMAGE_SECTORS - 1) * SECTOR : IMAGE_SECTORS * SECTOR] = bh + b"\x00" * (SECTOR - len(bh))

    # ESP contents.
    img[PART_START_LBA * SECTOR : PART_START_LBA * SECTOR + len(esp_fat)] = esp_fat
    return bytes(img)


def main() -> int:
    if len(sys.argv) < 4:
        print("usage: make-esp.py <kernel_dir> <limine_bin_dir> <out.hdd> [kernel_elf]")
        return 2
    kernel_dir = Path(sys.argv[1])
    limine_bin = Path(sys.argv[2])
    out = Path(sys.argv[3])
    elf = Path(sys.argv[4]) if len(sys.argv) > 4 else (
        kernel_dir / "target/x86_64-unknown-none/release/nucleus"
    )

    bootx64 = (limine_bin / "BOOTX64.EFI").read_bytes()
    bootia32 = (limine_bin / "BOOTIA32.EFI").read_bytes()
    bios_sys = (limine_bin / "limine-bios.sys").read_bytes()
    conf_path = Path(sys.argv[5]) if len(sys.argv) > 5 else kernel_dir / "limine.conf"
    conf = conf_path.read_bytes()
    nucleus = elf.read_bytes()

    fat = Fat16(ESP_SECTORS)

    # Root: /EFI dir + /NUCLEUS kernel + /limine.conf. The BIOS stage-2
    # search list does NOT include /EFI/BOOT/, so the config must also exist
    # at the root for BIOS boots. limine-bios.sys lives in /limine/ (also an
    # official search location) to keep multi-slot LFN runs out of the root.
    efi_c = fat.add_dir(fat.root, 0, "EFI", 0)
    fat.add_file(fat.root, 1, "NUCLEUS", nucleus)
    fat.add_file(fat.root, 3, "limine.conf", conf)  # LFN slot 2, short entry 3
    limine_c = fat.add_dir(fat.root, 4, "LIMINE", 0)
    lim_buf = bytearray(fat.spc * SECTOR)
    lim_buf[0:32] = fat._entry(".", limine_c, 0, 0x10)
    lim_buf[32:64] = fat._entry("..", 0, 0, 0x10)
    fat.add_file(lim_buf, 4, "limine-bios.sys", bios_sys)  # LFN 2..3, short 4
    fat._write_cluster(limine_c, bytes(lim_buf))

    # /EFI: . .. BOOT/
    efi_buf = bytearray(fat.spc * SECTOR)
    efi_buf[0:32] = fat._entry(".", efi_c, 0, 0x10)
    efi_buf[32:64] = fat._entry("..", 0, 0, 0x10)  # parent is root (cluster 0)
    boot_c = fat.add_dir(efi_buf, 2, "BOOT", efi_c)

    # /EFI/BOOT: . .. loaders + config. LIMINE.CONF needs one LFN slot, so it
    # lands at slot 5 with its LFN entry at slot 4.
    eb_buf = bytearray(fat.spc * SECTOR)
    eb_buf[0:32] = fat._entry(".", boot_c, 0, 0x10)
    eb_buf[32:64] = fat._entry("..", efi_c, 0, 0x10)
    fat.add_file(eb_buf, 2, "BOOTX64.EFI", bootx64)
    fat.add_file(eb_buf, 3, "BOOTIA32.EFI", bootia32)
    fat.add_file(eb_buf, 5, "LIMINE.CONF", conf)
    fat._write_cluster(boot_c, bytes(eb_buf))
    fat._write_cluster(efi_c, bytes(efi_buf))

    img = build_gpt_image(bytes(fat.image_bytes()))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(img)
    print(
        f"make-esp: wrote {out} ({len(img)} bytes; FAT16 {fat.clusters} clusters; "
        f"kernel={len(nucleus)}B bootx64={len(bootx64)}B bios_sys={len(bios_sys)}B conf={conf_path.name})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
