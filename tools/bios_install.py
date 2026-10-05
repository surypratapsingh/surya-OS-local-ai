#!/usr/bin/env python3
"""bios-install for NOVA disk images — a port of the pinned upstream tool.

Why this file exists: the vendored Windows host tool
(`.freebuff/ref/limine/limine-binary/limine-tool-windows-x86/limine.exe`)
is blocked on some Windows hosts by Smart App Control (CodeIntegrity event
3033: "did not meet the Enterprise signing level requirements"); see
docs/sac-build-blocks.md. This host also has no C compiler, so the
build-from-vendored-source fallback in scripts/build-disk.sh cannot run.
This port reimplements the ONE operation the build needs from that tool —
`bios-install` on a GPT image with a BIOS boot partition — so the image is
bootable on SeaBIOS again.

Source of truth (AGENTS.md rule 1/2): the pinned upstream distribution this
repo already vendors and trust-pins (scripts/trust.sh,
tools/manifest/bootchain.sha256):

  .freebuff/ref/limine/limine-binary/limine.c          (sha256-pinned)
  .freebuff/ref/limine/limine-binary/limine-bios-hdd.h (sha256-pinned)

`limine-bios-hdd.h` embeds the install image as C array
`binary_limine_hdd_bin_data[]`: [0:512] is the stage-1 boot sector,
[512:] is stage 2. The bytes written to the image below are exactly those
pinned bytes — this script does not generate boot code of its own.

Faithfulness contract, and the two deviations (both deliberate, both
fail-closed — this is a build-time installer for images this repo produces,
not a general-purpose tool):

  1. GPT only. Upstream also handles MBR images and ISOHYBRID GPT→MBR
     conversion (limine.c lines 1258-1423, 1443-1572). NOVA images are GPT
     (tools/make-esp.py); anything else is refused loudly instead of being
     rewritten. An ISOHYBRID ("CD001" at byte 32769, limine.c line 1263) is
     refused for the same reason.
  2. No --force. Upstream's --force overwrites filesystem signatures
     (validate_or_force, limine.c lines 635-693) to install over a
     recognised filesystem. This port refuses instead: a build image whose
     BIOS boot partition contains a filesystem is a bug in the image, not
     something to override in the build.

Everything else mirrors upstream limine.c `bios_install()` (line 1015) and
its helpers: same validation order, same refusal conditions, same messages.
On any refusal NOTHING is written — stricter than upstream, which relies on
its uninstall journal to undo a failed install.

Independence notes: CRC-32 is zlib.crc32 (a mature external implementation
of the same IEEE polynomial upstream hand-rolls at limine.c lines 705-720:
init 0xffffffff, poly 0xedb88320, final complement). Tests exercise this
port against a GPT built independently in tests/test_bios_install.py.
"""

from __future__ import annotations

import re
import struct
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIMINE_BIN_DIR = REPO_ROOT / ".freebuff/ref/limine/limine-binary"

# limine.c line 696-698: header CRC covers the header with its own CRC field
# zeroed [UEFI 2.10 §5.3.2, "Header - CRC32"].
GPT_HEADER_CRC_OFFSET = 16
GPT_HEADER_SIZE = 92
# limine.c line 620: the loader enumerates at most this many entries; the
# installer keeps step for autodetection. The full declared array is still
# bounded by the 1 MiB resource limit below before anything is read.
MAX_GPT_PARTITIONS = 256
GPT_MAX_ARRAY_SIZE = 1024 * 1024  # limine.c line 701
BIOS_BOOT_GUID = b"Hah!IdontNeedEFI"  # limine.c line 1664 (raw memcmp)

DEVATIONS = (
    "This port installs to GPT images only and offers no --force "
    "(deviations from upstream limine.c are listed in the docstring)."
)


def fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def warn(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


# ----------------------------------------------------------------------------
# The pinned install image, extracted from limine-bios-hdd.h
# ----------------------------------------------------------------------------

def load_install_image(header_path: Path | None = None) -> bytes:
    """Reassemble binary_limine_hdd_bin_data[] from the pinned C header.

    tests/test_bios_install.py checks this extraction independently (size,
    determinism, and that the stage-1 sector is a plausible MBR).
    """
    path = header_path or (LIMINE_BIN_DIR / "limine-bios-hdd.h")
    text = path.read_text(encoding="ascii")
    arrays = re.findall(r"binary_limine_hdd_bin_data\[\]\s*=\s*\{(.*?)\};", text, re.S)
    if len(arrays) != 1:
        fail(f"limine-bios-hdd.h: expected exactly one binary_limine_hdd_bin_data array, found {len(arrays)}")
    vals = [int(x, 16) for x in re.findall(r"0x[0-9a-fA-F]{2}", arrays[0])]
    img = bytes(vals)
    if len(img) <= 512:
        fail(f"limine-bios-hdd.h: install image implausibly small ({len(img)} bytes)")
    return img


# ----------------------------------------------------------------------------
# GPT structures [UEFI 2.10 §5.3.1/§5.3.2; limine.c lines 92-117, 696-698]
# ----------------------------------------------------------------------------

def crc32(data: bytes) -> int:
    """IEEE CRC-32 via zlib — external oracle for upstream's hand-rolled loop."""
    return zlib.crc32(data) & 0xFFFFFFFF


def header_crc_ok(header: bytes) -> bool:
    """Header CRC with the crc32 field itself zeroed [limine.c lines 723-748]."""
    zeroed = bytearray(header)
    zeroed[GPT_HEADER_CRC_OFFSET:GPT_HEADER_CRC_OFFSET + 4] = b"\x00\x00\x00\x00"
    stored = struct.unpack_from("<I", header, GPT_HEADER_CRC_OFFSET)[0]
    return crc32(bytes(zeroed)) == stored


def parse_gpt_header(block: bytes, header_lba: int, lb_size: int) -> dict | None:
    """Validate one GPT header per limine.c gpt_verify_header() (line 755).

    Returns the parsed fields, or None if the header does not verify. Checks:
    signature, revision 0x00010000, 92 <= header_size <= lb_size, MyLBA
    self-consistency, header CRC, entry size a power of two >= 128, and the
    entry-array CRC — the same checks in the same order.
    """
    if len(block) < GPT_HEADER_SIZE:
        return None
    (signature, revision, header_size, _crc32, _reserved,
     my_lba, _alternate_lba, _first_usable, _last_usable,
     _disk_guid, partition_entry_lba,
     number_of_entries, entry_size, array_crc32) = struct.unpack_from(
        "<8sIIII4Q16sQ3I", block, 0)

    if signature != b"EFI PART":                       # limine.c line 769
        return None
    if revision != 0x00010000:                          # limine.c line 772
        return None
    if header_size < GPT_HEADER_SIZE or header_size > lb_size:  # line 775
        return None
    if my_lba != header_lba:                            # line 779
        return None
    if not header_crc_ok(block[:header_size]):          # line 785
        return None
    if entry_size < 128 or (entry_size & (entry_size - 1)) != 0:  # line 789
        return None

    array_size = number_of_entries * entry_size
    if array_size == 0 or array_size > GPT_MAX_ARRAY_SIZE:      # line 797
        return None

    array_lba = partition_entry_lba
    if crc32(read_entry_array(array_lba, array_size)) != array_crc32:  # line 866
        return None

    return {
        "header_lba": header_lba,
        "alternate_lba": _alternate_lba,
        "first_usable": _first_usable,
        "last_usable": _last_usable,
        "partition_entry_lba": partition_entry_lba,
        "number_of_entries": number_of_entries,
        "entry_size": entry_size,
    }


def read_entry_array(array_lba: int, array_size: int) -> bytes:
    """Read the entry array through the module-level image handle."""
    loc = array_lba * LB_SIZE
    data = IMAGE[loc:loc + array_size]
    if len(data) != array_size:
        fail(f"GPT partition entry array at LBA {array_lba} runs past the end of the image")
    return data


def parse_entry(array: bytes, index: int, entry_size: int) -> tuple[bytes, int, int]:
    """One GPT entry as (raw 128-byte type-guid, starting_lba, ending_lba)."""
    off = index * entry_size
    type_guid = array[off:off + 16]
    starting_lba, ending_lba = struct.unpack_from("<QQ", array, off + 32)
    return type_guid, starting_lba, ending_lba


# ----------------------------------------------------------------------------
# The port of bios_install() itself
# ----------------------------------------------------------------------------

IMAGE = bytearray()
LB_SIZE = 512  # set by find_gpt(); files probe 512 first exactly like upstream


def find_gpt(img: bytearray, quiet: bool) -> tuple[dict, bool]:
    """Locate and verify the GPT [limine.c gpt_locate_header(), line 911].

    Probe order matches upstream: logical block sizes 512/2048/4096 (a
    regular file always accepts the first read, so 512 wins — the size this
    repo's images are built with), primary header at LBA 1, then the
    AlternateLBA the failed primary names, then the last block of the
    medium — with the last block tried first when both exist, because a
    genuine alternate at the end wins over whatever a corrupt primary
    points at (limine.c lines 969-985).
    """
    global LB_SIZE

    # Protective MBR: a GPT lives behind a 0xEE entry [limine.c line 896].
    mbr = img[0:512]
    if not any(mbr[0x1be + 16 * i + 4] == 0xEE for i in range(4)):
        fail("no EFI PART header found: image has no protective MBR (GPT images only in this port; "
             "upstream would fall back to its MBR path)")

    # Block-size probe: for a regular file the first guess always reads,
    # matching upstream device_init() (limine.c lines 419-446).
    lb_size = None
    for guess in (512, 2048, 4096):
        if len(img) >= guess:
            lb_size = guess
            break
    if lb_size is None:
        fail("image smaller than one logical block")
    LB_SIZE = lb_size
    warn(f"Installing to GPT. Logical block size of {lb_size} bytes.", quiet)

    # Last block of the medium, probed not assumed [limine.c lines 873-894]:
    # a regular file ends where reads start failing.
    last_block = (len(img) // lb_size) - 1

    primary = img[lb_size:lb_size + GPT_HEADER_SIZE]
    header = parse_gpt_header(primary, 1, lb_size)
    if header is not None:
        return header, False

    # Primary failed: recovery per limine.c lines 949-985. The AlternateLBA
    # is a candidate ONLY when the primary's signature is valid — without it
    # the field is not an LBA, it is whatever happens to be at offset 32
    # (limine.c lines 956-960) — and only when that block exists and is not
    # the last block. Tried order: the last block first, then AlternateLBA
    # (lines 969-985: a genuine alternate at the end wins over whatever a
    # corrupt primary points at).
    candidates: list[int] = []
    if last_block >= 1:
        candidates.append(last_block)
    if primary[0:8] == b"EFI PART":
        alt = struct.unpack_from("<Q", primary, 32)[0]
        if alt > 1 and alt <= last_block and alt != last_block:
            candidates.append(alt)

    for cand in candidates:
        block = img[cand * lb_size:cand * lb_size + GPT_HEADER_SIZE]
        header = parse_gpt_header(block, cand, lb_size)
        if header is not None:
            warn("warning: Primary GPT did not verify; using the alternate header.", quiet)
            return header, True

    fail("no verifiable GPT header found (primary and every recovery candidate failed)")


def validate_or_force(offset: int, partition_num: int, quiet: bool) -> None:
    """Refuse to install over a recognised filesystem [limine.c line 635].

    Upstream offers --force to clobber the signatures; this port refuses
    (deviation 2). Same probes, same order: NTFS at +3, FAT at +54 and +82,
    FAT32 at +3, ext2 magic 0xEF53 at +1080.
    """
    end = offset + 2048
    if end > len(IMAGE):
        fail(f"partition {partition_num} extends past the end of the image")
    region = bytes(IMAGE[offset:end])
    if region[3:7] == b"NTFS":
        fail("the partition selected to install the BIOS boot code to contains "
             "a recognised filesystem (NTFS signature); this port does not "
             "offer upstream's --force override")
    if region[54:57] == b"FAT" or region[82:85] == b"FAT":
        fail("the partition selected to install the BIOS boot code to contains "
             "a recognised filesystem (FAT signature); this port does not "
             "offer upstream's --force override")
    if region[3:8] == b"FAT32":
        fail("the partition selected to install the BIOS boot code to contains "
             "a recognised filesystem (FAT32 signature); this port does not "
             "offer upstream's --force override")
    if struct.unpack_from("<H", region, 1080)[0] == 0xEF53:
        fail("the partition selected to install the BIOS boot code to contains "
             "a recognised filesystem (ext2 magic); this port does not offer "
             "upstream's --force override")


def bios_install(image_path: Path, part_index_arg: str | None, quiet: bool, install_img: bytes) -> None:
    global IMAGE
    IMAGE = bytearray(image_path.read_bytes())
    orig_len = len(IMAGE)

    if bytes(IMAGE[32769:32774]) == b"CD001":
        fail("image looks like an ISOHYBRID (CD001 at byte 32769); upstream would "
             "convert its GPT to MBR, which this port refuses (deviation 1). "
             + DEVATIONS)

    gpt, from_alternate = find_gpt(IMAGE, quiet)
    # A header recovered from the alternate is used as-is below, exactly as
    # upstream does (limine.c lines 940-946).

    # Parse the declared entry array once [UEFI §5.3.1: array follows its
    # header's PartitionEntryLBA; bounded by GPT_MAX_ARRAY_SIZE in
    # parse_gpt_header].
    array_loc = gpt["partition_entry_lba"] * LB_SIZE
    array_size = gpt["number_of_entries"] * gpt["entry_size"]
    array = bytes(IMAGE[array_loc:array_loc + array_size])

    # --- pick the BIOS boot partition -------------------------------------
    # Explicit index (limine.c lines 1610-1652) or autodetect (1654-1677):
    # the first partition whose 16-byte type GUID equals "Hah!IdontNeedEFI".
    partition_num: int | None = None
    starting_lba = ending_lba = 0
    if part_index_arg is not None:
        if not part_index_arg.isdigit() or int(part_index_arg) == 0:
            fail(f"invalid partition number '{part_index_arg}': expected a whole number starting at 1")
        idx = int(part_index_arg) - 1
        if idx >= min(gpt["number_of_entries"], MAX_GPT_PARTITIONS):
            fail("partition number is too large")
        type_guid, starting_lba, ending_lba = parse_entry(array, idx, gpt["entry_size"])
        if type_guid == b"\x00" * 16:
            fail(f"no such partition: {idx + 1}")
        if type_guid != BIOS_BOOT_GUID:
            fail("chosen partition for BIOS boot code is not of BIOS boot partition type; "
                 "this port does not offer upstream's --force override")
        partition_num = idx
    else:
        for i in range(min(gpt["number_of_entries"], MAX_GPT_PARTITIONS)):
            type_guid, starting_lba, ending_lba = parse_entry(array, i, gpt["entry_size"])
            if type_guid == BIOS_BOOT_GUID:
                partition_num = i
                warn(f"Autodetected partition {i + 1} as BIOS boot partition.", quiet)
                break
        if partition_num is None:
            fail("installing to a GPT device, but no BIOS boot partition specified or detected")

    pnum = partition_num + 1

    # --- the upstream GPT-path checks, same order --------------------------
    if ending_lba < starting_lba:                                    # line 1682
        fail(f"partition {pnum} has ending LBA less than starting LBA")
    if starting_lba < 2 + (16384 + LB_SIZE - 1) // LB_SIZE:          # line 1692
        fail(f"partition {pnum} starts inside the GPT reserve")
    if starting_lba < gpt["first_usable"] or ending_lba > gpt["last_usable"]:  # line 1697
        fail(f"partition {pnum} lies outside the GPT usable range")

    last_block = (len(IMAGE) // LB_SIZE) - 1
    end_reserve = (16384 + LB_SIZE - 1) // LB_SIZE
    if last_block < 1 + end_reserve or ending_lba > last_block - 1 - end_reserve:  # line 1712
        fail(f"partition {pnum} ends inside the alternate GPT")

    # Overlap check against every declared non-empty sibling [limine.c
    # lines 1723-1749: the usable range is where all GPT structures live,
    # and UEFI requires that partitions not overlap].
    for i in range(gpt["number_of_entries"]):
        if i == partition_num:
            continue
        other_guid, o_start, o_end = parse_entry(array, i, gpt["entry_size"])
        if other_guid == b"\x00" * 16:
            continue
        if starting_lba <= o_end and o_start <= ending_lba:
            fail(f"partition {pnum} overlaps partition {i + 1}")

    part_size = (ending_lba - starting_lba + 1) * LB_SIZE
    if part_size < 32768:                                            # line 1757
        fail(f"partition {pnum} is smaller than 32KiB")

    stage2_loc = starting_lba * LB_SIZE
    stage2_max = part_size

    validate_or_force(stage2_loc, pnum, quiet)                       # line 1712

    warn(f"Installing BIOS boot code to partition {pnum}.", quiet)
    warn(f"Stage 2 to be located at byte offset 0x{stage2_loc:x}.", quiet)

    if len(install_img) - 512 > stage2_max:                          # line 1789
        fail(f"stage 2 needs {len(install_img) - 512} bytes at offset 0x{stage2_loc:x}, "
             f"but only {stage2_max} are available before the next thing on the device")

    # --- the writes [limine.c lines 1795-1812] -----------------------------
    # Save original timestamp (218..224) and partition table (440..510),
    # write stage 1 over LBA 0, stage 2 at stage2_loc, hardcode stage 2's
    # byte offset at 0x1a4, then restore the saved regions. In upstream the
    # writes go through a write journal; here they are applied to an
    # in-memory copy committed only after every validation above passed.
    timestamp = bytes(IMAGE[218:224])
    orig_table = bytes(IMAGE[440:510])

    IMAGE[0:512] = install_img[0:512]
    IMAGE[stage2_loc:stage2_loc + (len(install_img) - 512)] = install_img[512:]
    IMAGE[0x1A4:0x1AC] = struct.pack("<Q", stage2_loc)
    IMAGE[218:224] = timestamp
    IMAGE[440:510] = orig_table

    assert len(IMAGE) == orig_len
    image_path.write_bytes(IMAGE)

    warn(
        "Reminder: Remember to copy the limine-bios.sys file in either\n"
        "          the root, /boot, /limine, or /boot/limine directories of\n"
        "          one of the partitions on the device, or boot will fail!",
        quiet,
    )
    warn("Limine BIOS stages installed successfully.", quiet)


def main(argv: list[str]) -> int:
    quiet = False
    positional: list[str] = []
    for arg in argv:
        if arg in ("--quiet", "-q"):
            quiet = True
        elif arg in ("--help", "-h"):
            print("usage: bios_install.py bios-install <image> [GPT partition index] [--quiet]")
            print(DEVATIONS)
            return 0
        elif arg.startswith("-"):
            print(f"error: unrecognised option '{arg}'; this port supports --quiet/--help only "
                  f"(upstream's --force is deliberately absent)", file=sys.stderr)
            return 2
        else:
            positional.append(arg)

    if not positional or positional[0] != "bios-install":
        print("usage: bios_install.py bios-install <image> [GPT partition index] [--quiet]", file=sys.stderr)
        return 2
    positional = positional[1:]
    if not positional or len(positional) > 2:
        print("usage: bios_install.py bios-install <image> [GPT partition index] [--quiet]", file=sys.stderr)
        return 2

    image_path = Path(positional[0])
    part_index = positional[1] if len(positional) == 2 else None

    if not image_path.is_file():
        fail(f"no such file: {image_path}")

    install_img = load_install_image()
    bios_install(image_path, part_index, quiet, install_img)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
