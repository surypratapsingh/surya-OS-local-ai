#!/usr/bin/env python3
"""W2 mutation test: the verifier must catch the defect this work order was
created for.

The pre-W2 builder placed the backup GPT entry array at
    IMAGE_SECTORS - 1 - 128 = 81791
while declaring LastUsableLBA = IMAGE_SECTORS - 34 = 81886 — the declared
usable range covered the backup array itself, and the old verifier never
checked the field.

This script:
  1. builds a correct image with tools/make-esp.py (as check.sh does),
  2. patches ONLY the GPT bookkeeping: moves the 16 KiB backup entry array
     back to LBA 81791, points both the backup header at it, recomputes the
     two header CRC32s, and leaves LastUsableLBA = 81886,
  3. runs verify-disk.py and REQUIRES failure, printing which checks fired.

Exit 0 iff the verifier reported the defect; any other outcome exits 1.

Usage: py tools/demo-w2-detection.py <kernel_dir> <limine_bin_dir> <elf> [conf]
"""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path

SECTOR = 512
IMAGE_SECTORS = 81920
LAST_LBA = IMAGE_SECTORS - 1
GOOD_BAK_ENTRIES = IMAGE_SECTORS - 1 - 32   # 81887 (correct, post-W2)
OLD_BAK_ENTRIES = IMAGE_SECTORS - 1 - 128   # 81791 (the pre-W2 defect)
LAST_USABLE = IMAGE_SECTORS - 34            # 81886


def crc32(data) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def main() -> int:
    kernel_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("kernel")
    limine_bin = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".freebuff/ref/limine/limine-binary")
    elf = (Path(sys.argv[3]) if len(sys.argv) > 3 else
           kernel_dir / "target/x86_64-unknown-none/release/nucleus")
    conf = Path(sys.argv[4]) if len(sys.argv) > 4 else kernel_dir / "limine.conf"

    out = Path("build/nova-old-layout.hdd")
    out.parent.mkdir(exist_ok=True)

    # 1. correct image, straight from the builder
    r = subprocess.run([sys.executable, "tools/make-esp.py",
                        str(kernel_dir), str(limine_bin), str(out),
                        str(elf), str(conf)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        print("demo-w2: make-esp.py failed")
        return 1

    img = bytearray(out.read_bytes())

    # 2. relocate the backup entry array to the defective LBA
    entries = bytes(img[GOOD_BAK_ENTRIES * SECTOR:(GOOD_BAK_ENTRIES + 32) * SECTOR])
    img[OLD_BAK_ENTRIES * SECTOR:OLD_BAK_ENTRIES * SECTOR + len(entries)] = entries
    img[GOOD_BAK_ENTRIES * SECTOR:(GOOD_BAK_ENTRIES + 32) * SECTOR] = b"\x00" * len(entries)

    ent_crc = crc32(img[2 * SECTOR:2 * SECTOR + 128 * 128])

    def resealed(h: bytearray) -> bytes:
        struct.pack_into("<I", h, 16, 0)
        struct.pack_into("<I", h, 16, crc32(bytes(h)))
        return bytes(h)

    # backup header: point PartitionEntryLBA at the defective array
    bh = bytearray(img[LAST_LBA * SECTOR:LAST_LBA * SECTOR + 92])
    struct.pack_into("<Q", bh, 72, OLD_BAK_ENTRIES)
    struct.pack_into("<I", bh, 88, ent_crc)
    # LastUsableLBA (offset 48) is deliberately left at 81886 — that overlap
    # is the defect.
    assert struct.unpack_from("<Q", bh, 48)[0] == LAST_USABLE
    img[LAST_LBA * SECTOR:LAST_LBA * SECTOR + 92] = resealed(bh)

    # primary header: entry array unchanged at LBA 2, but its CRC covers the
    # entries (unchanged) — recompute anyway so ONLY the layout defect can fail
    ph = bytearray(img[SECTOR:SECTOR + 92])
    struct.pack_into("<I", ph, 88, ent_crc)
    img[SECTOR:SECTOR + 92] = resealed(ph)

    out.write_bytes(bytes(img))
    print(f"demo-w2: wrote {out} with backup entry array at LBA {OLD_BAK_ENTRIES} "
          f"and LastUsableLBA {LAST_USABLE}")

    # 3. the rewritten verifier must report it
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_disk", "tools/verify-disk.py")
    vd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vd)
    expect = vd.load_expectations(kernel_dir, limine_bin, kernel_elf=elf)
    failures = vd.verify_image(bytes(img), expect)

    layout_fails = [f for f in failures
                    if "backup entry array" in f or "LastUsableLBA" in f
                    or "usable" in f]
    print(f"demo-w2: verifier reported {len(failures)} failure(s) on the defective image:")
    for f in failures:
        print(f"    - {f}")

    if not layout_fails:
        print("demo-w2: FAIL — no layout check caught the backup-array/LastUsableLBA overlap")
        return 1
    print(f"demo-w2: PASS — {len(layout_fails)} layout check(s) caught the W2 defect")
    return 0


if __name__ == "__main__":
    sys.exit(main())
