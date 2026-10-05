#!/usr/bin/env python3
"""Tests for tools/bios_install.py (the SAC-blocked-limine.exe fallback).

The oracle (AGENTS.md rules 1-2): the pinned upstream distribution this repo
already vendors — .freebuff/ref/limine/limine-binary/limine.c (bios_install(),
line 1015) and limine-bios-hdd.h (binary_limine_hdd_bin_data[]). The content
test below asserts bios_install.py writes exactly the bytes the upstream
algorithm writes, derived from upstream's own write list (limine.c lines
1795-1812):

  1. install_img[0:512]            -> image[0:512]        (stage 1 / MBR)
  2. install_img[512:]             -> image[stage2_loc:]  (stage 2)
  3. stage2_loc as little-endian
     uint64                        -> image[0x1a4:0x1ac]  (stage 2 pointer)
  4. the pre-write bytes of
     image[218:224] and
     image[440:510] restored       -> timestamp + MBR table preserved

The install image itself is reassembled here from the pinned header with a
SEPARATE, simpler extraction path (brace depth scan, not the regex the tool
uses), so a parser bug in tools/bios_install.py cannot hide behind a twin.

The GPT the tests install onto is built in this file from UEFI 2.10 §5.3.1
- §5.3.2 rules — it is NOT tools/make-esp.py's output, so a shared
misconception between builder and installer cannot pass both.
"""

from __future__ import annotations

import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import bios_install  # noqa: E402

LIMINE_C = ROOT / ".freebuff/ref/limine/limine-binary/limine.c"
HDD_HEADER = ROOT / ".freebuff/ref/limine/limine-binary/limine-bios-hdd.h"
BIOS_BOOT_GUID = b"Hah!IdontNeedEFI"

SECTOR = 512
IMG_SECTORS = 4096  # 2 MiB test image — small, nothing in the tests cares
PART1_START, PART1_END = 2048, 4095  # the "ESP"
PART2_START, PART2_END = 34, 2047    # the BIOS boot partition


# ----------------------------------------------------------------------------
# Independent extraction (brace-depth scan — deliberately not the tool's regex)
# ----------------------------------------------------------------------------

def extract_install_image() -> bytes:
    src = HDD_HEADER.read_text(encoding="ascii")
    start = src.index("binary_limine_hdd_bin_data[]")
    brace = src.index("{", start)
    depth, i = 1, brace + 1
    while depth:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    body = src[brace + 1:i - 1]
    vals = [int(tok.strip(), 16) for tok in body.split(",") if tok.strip()]
    return bytes(vals)


# ----------------------------------------------------------------------------
# Independent GPT builder (UEFI 2.10 §5.3.1/§5.3.2, not make-esp.py)
# ----------------------------------------------------------------------------

def build_gpt_image(with_bios_boot: bool = True,
                    break_array_crc: bool = False,
                    break_primary_only: bool = False,
                    bios_range: tuple[int, int] = (PART2_START, PART2_END),
                    esp_range: tuple[int, int] = (PART1_START, PART1_END)) -> bytearray:
    img = bytearray(IMG_SECTORS * SECTOR)

    def entry(type_guid: bytes, start: int, end: int) -> bytes:
        e = bytearray(128)
        e[0:16] = type_guid
        struct.pack_into("<QQ", e, 32, start, end)
        return bytes(e)

    entries = bytearray(128 * 128)
    # Entry 1: a real (non-BIOS) ESP-type partition, so "wrong partition"
    # and autodetect paths are exercised against a non-empty entry.
    entries[0:128] = entry(
        b"\x28\xa7\x2a\xc1\x1f\xf8\xd2\x11\xba\x4b\x00\xa0\xc9\x3e\xc9\x3b",
        *esp_range)
    if with_bios_boot:
        entries[128:256] = entry(BIOS_BOOT_GUID, *bios_range)

    def header(my_lba: int, alternate_lba: int) -> bytes:
        h = bytearray(92)
        h[0:8] = b"EFI PART"
        struct.pack_into("<I", h, 8, 0x00010000)
        struct.pack_into("<I", h, 12, 92)
        struct.pack_into("<QQQQ", h, 24, my_lba, alternate_lba, 34, IMG_SECTORS - 34)
        struct.pack_into("<Q", h, 72, 2)
        struct.pack_into("<III", h, 80, 128, 128, zlib.crc32(bytes(entries)) & 0xFFFFFFFF)
        if break_array_crc:
            struct.pack_into("<I", h, 88, (struct.unpack_from("<I", h, 88)[0]) ^ 0xFFFFFFFF)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h)

    # Protective MBR: 0xEE entry at offset 446+4, signature 0xAA55.
    pmbr = bytearray(SECTOR)
    pmbr[446 + 4] = 0xEE
    struct.pack_into("<H", pmbr, 510, 0xAA55)
    img[0:SECTOR] = pmbr

    primary = header(1, IMG_SECTORS - 1)
    backup = header(IMG_SECTORS - 1, 1)
    if break_primary_only:
        # Corrupt the primary header CRC only; the backup stays valid.
        bad = bytearray(primary)
        bad[16:20] = b"\x00\x00\x00\x00"
        primary = bytes(bad)

    img[SECTOR:SECTOR + 92] = primary
    img[2 * SECTOR:2 * SECTOR + len(entries)] = entries
    img[(IMG_SECTORS - 32) * SECTOR:(IMG_SECTORS - 32) * SECTOR + len(entries)] = entries
    img[(IMG_SECTORS - 1) * SECTOR:(IMG_SECTORS - 1) * SECTOR + 92] = backup
    return img


def expected_final_state(orig: bytes, install_img: bytes, stage2_loc: int) -> bytes:
    """Upstream's exact final state, derived from limine.c lines 1795-1812.

    Upstream writes stage 2, then the stage-1 sector, then the stage-2
    pointer, then RESTORES the original timestamp and partition table on
    top of the freshly written stage-1 sector. The net device state is
    therefore: stage-1 bytes everywhere, EXCEPT 218:224 (original
    timestamp), 0x1a4 (the stage-2 pointer, which lives below the restored
    table region and so survives it), and 440:510 (original partition
    table, including the 0xEE protective entry and the 0xAA55 signature).
    """
    out = bytearray(orig)
    out[0:512] = install_img[0:512]
    out[stage2_loc:stage2_loc + len(install_img) - 512] = install_img[512:]
    out[0x1A4:0x1AC] = struct.pack("<Q", stage2_loc)
    out[218:224] = orig[218:224]
    out[440:510] = orig[440:510]
    return bytes(out)


def run_install(img: bytearray, part_index: str | None = None) -> None:
    """Run the tool in-memory (temp file, like upstream writes a real device)."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".hdd", delete=False) as f:
        f.write(bytes(img))
        path = Path(f.name)
    try:
        bios_install.bios_install(path, part_index, quiet=True, install_img=extract_install_image())
        img[:] = bytearray(path.read_bytes())
    finally:
        path.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

class TestInstallImage(unittest.TestCase):
    def test_extraction_paths_agree(self) -> None:
        """The tool's regex extraction and this file's brace scan agree."""
        self.assertEqual(bios_install.load_install_image(HDD_HEADER), extract_install_image())

    def test_install_image_shape(self) -> None:
        """Stage 1 is exactly one sector; stage 2 is the remainder."""
        img = extract_install_image()
        self.assertGreater(len(img), 512)
        self.assertEqual(len(img) % 8, 0)  # formatted as "0x%02x, " = 8 chars/byte

    def test_stage1_is_an_mbr_shape(self) -> None:
        """[0:512] must be a plausible boot sector: sig, bootable-ish code, no FS sig."""
        img = extract_install_image()
        s1 = img[0:512]
        self.assertEqual(s1[510:512], b"\x55\xaa")
        self.assertNotEqual(s1[0:440], b"\x00" * 440, "stage 1 carries no code")
        # A pure stage-1 sector has no filesystem signatures where a real
        # partition boot sector would have them.
        self.assertNotIn(b"NTFS", s1[3:7])
        self.assertNotIn(b"FAT32", s1[3:8])

    def test_upstream_write_list_is_what_we_ported(self) -> None:
        """The parity contract targets upstream's actual write list (rule 4)."""
        src = LIMINE_C.read_text(encoding="ascii")
        for marker in ("Write the bootsector from the bootloader to the device",
                       "Write the rest of stage 2 to the device",
                       "Hardcode in the bootsector the location of stage 2",
                       "Write back timestamp",
                       "Write back the saved partition table to the device"):
            self.assertIn(marker, src)


class TestContentParity(unittest.TestCase):
    def setUp(self) -> None:
        self.install_img = extract_install_image()
        self.img = build_gpt_image()
        self.orig = bytes(self.img)

    def test_writes_match_upstream_algorithm(self) -> None:
        run_install(self.img)

        stage2_loc = PART2_START * SECTOR
        # The full final state equals upstream's write-then-restore model.
        self.assertEqual(bytes(self.img),
                         expected_final_state(self.orig, self.install_img, stage2_loc))
        # And its load-bearing parts, spelled out. Within the stage-1
        # sector, only 0:218, 224:420 and 428:440 are raw stage-1 bytes —
        # 218:224 (timestamp) and 0x1a4=420:428 (pointer) are deliberately
        # overwritten afterwards.
        self.assertEqual(self.img[0:218], self.install_img[0:218], "stage-1 boot code (head)")
        self.assertEqual(self.img[224:420], self.install_img[224:420], "stage-1 boot code (mid)")
        self.assertEqual(self.img[428:440], self.install_img[428:440], "stage-1 boot code (tail)")
        self.assertEqual(bytes(self.img[218:224]), self.orig[218:224], "timestamp restored")
        self.assertEqual(struct.unpack_from("<Q", self.img, 0x1A4)[0], stage2_loc,
                         "stage-2 pointer")
        self.assertEqual(bytes(self.img[440:510]), self.orig[440:510],
                         "partition table (incl. 0xEE + 0xAA55) restored over stage 1")
        self.assertEqual(self.img[510:512], b"\x55\xaa", "boot signature restored")
        self.assertEqual(self.img[stage2_loc:stage2_loc + len(self.install_img) - 512],
                         self.install_img[512:], "stage 2 bytes")

    def test_everything_else_untouched(self) -> None:
        """The total diff is exactly the union of upstream's write regions."""
        run_install(self.img)
        stage2_loc = PART2_START * SECTOR
        self.assertEqual(bytes(self.img),
                         expected_final_state(self.orig, self.install_img, stage2_loc))

    def test_gpt_structures_survive(self) -> None:
        run_install(self.img)
        # Primary header block and entry array are outside the write regions;
        # they must survive byte-identically.
        self.assertEqual(bytes(self.img[SECTOR:2 * SECTOR]), self.orig[SECTOR:2 * SECTOR])
        self.assertEqual(bytes(self.img[2 * SECTOR:2 * SECTOR + 16384]),
                         self.orig[2 * SECTOR:2 * SECTOR + 16384])

    def test_explicit_partition_index_same_result(self) -> None:
        run_install(self.img)
        a = bytes(self.img)
        b = build_gpt_image()
        run_install(b, part_index="2")  # BIOS boot partition is entry 2 (1-based)
        self.assertEqual(a, bytes(b))


class TestAlternateRecovery(unittest.TestCase):
    def test_recovers_from_valid_alternate(self) -> None:
        """Primary CRC corrupted, backup valid: install proceeds via backup."""
        img = build_gpt_image(break_primary_only=True)
        run_install(img)
        self.assertEqual(bytes(img),
                         expected_final_state(bytes(build_gpt_image(break_primary_only=True)),
                                              extract_install_image(),
                                              PART2_START * SECTOR))


class TestRefusals(unittest.TestCase):
    """Every refusal must leave the image byte-identical (fail-closed)."""

    def _assert_refusal_untouched(self, img: bytearray, part_index: str | None = None,
                                  expect_sub: str | None = None) -> None:
        orig = bytes(img)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".hdd", delete=False) as f:
            f.write(orig)
            path = Path(f.name)
        try:
            import io, contextlib
            err = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                bios_install.bios_install(path, part_index, quiet=True,
                                          install_img=extract_install_image())
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(path.read_bytes(), orig, "a refusal must not write")
            if expect_sub is not None:
                self.assertIn(expect_sub, err.getvalue(),
                              "refusal came from an unexpected code path")
        finally:
            path.unlink(missing_ok=True)

    def test_no_bios_boot_partition_refused(self) -> None:
        self._assert_refusal_untouched(build_gpt_image(with_bios_boot=False))

    def test_wrong_partition_type_refused(self) -> None:
        self._assert_refusal_untouched(build_gpt_image(), part_index="1")

    def test_no_such_partition_refused(self) -> None:
        self._assert_refusal_untouched(build_gpt_image(), part_index="9")

    def test_invalid_partition_number_refused(self) -> None:
        self._assert_refusal_untouched(build_gpt_image(), part_index="0")
        self._assert_refusal_untouched(build_gpt_image(), part_index="x")

    def test_broken_array_crc_refused(self) -> None:
        """Entry-array CRC break: neither header verifies -> refusal."""
        self._assert_refusal_untouched(build_gpt_image(break_array_crc=True))

    def test_no_protective_mbr_refused(self) -> None:
        img = build_gpt_image()
        img[446 + 4] = 0x07  # 0xEE -> something else: not a GPT carrier
        self._assert_refusal_untouched(img)

    def test_isohybrid_refused(self) -> None:
        img = build_gpt_image()
        img[32769:32774] = b"CD001"
        self._assert_refusal_untouched(img, expect_sub="ISOHYBRID")

    def test_fat_in_bios_partition_refused(self) -> None:
        img = build_gpt_image()
        off = PART2_START * SECTOR + 54
        img[off:off + 3] = b"FAT"
        self._assert_refusal_untouched(img, expect_sub="recognised filesystem")

    def test_tiny_bios_partition_refused(self) -> None:
        """BIOS boot partition under 32 KiB: the 32KiB check must be the
        path that fires (headers stay valid — built, not patched)."""
        self._assert_refusal_untouched(build_gpt_image(bios_range=(34, 36)),
                                       expect_sub="smaller than 32KiB")


class TestMutation(unittest.TestCase):
    """AGENTS.md reviewer check 3: break the implementation on purpose and
    prove the suite notices. The mutation is a real, executed copy of the
    tool with stage-2 placement off by one sector."""

    MUTATION_FROM = "stage2_loc = starting_lba * LB_SIZE"
    MUTATION_TO = "stage2_loc = (starting_lba + 1) * LB_SIZE"

    def test_a_mutated_implementation_is_caught_by_the_parity_predicate(self) -> None:
        src = (ROOT / "tools/bios_install.py").read_text(encoding="utf-8")
        self.assertIn(self.MUTATION_FROM, src, "mutation anchor missing — update the test")
        mutant_src = src.replace(self.MUTATION_FROM, self.MUTATION_TO, 1)

        import importlib.util
        import io, contextlib, tempfile
        out_dir = ROOT / "build"
        out_dir.mkdir(exist_ok=True)
        mpath = out_dir / "mutant_bios_install.py"
        mpath.write_text(mutant_src, encoding="utf-8")
        try:
            spec = importlib.util.spec_from_file_location("mutant_bios_install", mpath)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            img = build_gpt_image()
            orig = bytes(img)
            with tempfile.NamedTemporaryFile(suffix=".hdd", delete=False) as f:
                f.write(orig)
                path = Path(f.name)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                mod.bios_install(path, None, quiet=True,
                                 install_img=extract_install_image())
            got = path.read_bytes()
            path.unlink(missing_ok=True)

            expected = expected_final_state(orig, extract_install_image(),
                                            PART2_START * SECTOR)
            self.assertNotEqual(got, expected,
                                "mutant (stage 2 one sector late) was NOT caught "
                                "by the parity predicate — the suite is decorative")
            # And the refusal tests keep their teeth: the mutated tool must
            # still be refused on a bad GPT (image untouched).
            bad = build_gpt_image(break_array_crc=True)
            bad_orig = bytes(bad)
            with tempfile.NamedTemporaryFile(suffix=".hdd", delete=False) as f:
                f.write(bad_orig)
                bpath = Path(f.name)
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                mod.bios_install(bpath, None, quiet=True,
                                 install_img=extract_install_image())
            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(bpath.read_bytes(), bad_orig)
            bpath.unlink(missing_ok=True)
        finally:
            mpath.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
