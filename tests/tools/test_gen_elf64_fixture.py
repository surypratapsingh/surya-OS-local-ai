"""Unit tests for tools/gen-elf64-fixture.py (stdlib unittest only).

Expected bytes come from the layout written in the tool's module docstring
and from the gABI field offsets, both re-stated here by the test. The
independent check is tools/verify-elf64.py: it must accept the positive
fixture and reject every negative. The generator is not used to decide
what the expected answer is.
"""

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
TOOL = os.path.join(ROOT, "tools", "gen-elf64-fixture.py")
VERIFY = os.path.join(ROOT, "tools", "verify-elf64.py")

_spec = importlib.util.spec_from_file_location("gen_elf64_fixture", TOOL)
gf = importlib.util.module_from_spec(_spec)
# The module reads sys.argv[1] at import time; neutralise it for import.
_saved_argv = sys.argv
sys.argv = ["gen-elf64-fixture.py"]
try:
    _spec.loader.exec_module(gf)
finally:
    sys.argv = _saved_argv

NEGATIVE_NAMES = {
    "bad-magic", "ei-class32", "ei-data-be", "et-dyn", "phnum-zero",
    "phentsize-wrong", "memsz-lt-filesz", "offset-oob", "interp-present",
    "entry-oob",
}


class GenElf64FixtureTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp(prefix="elf64-fixture-test-"))
        self.saved_out = gf.OUT
        gf.OUT = self.out
        with contextlib.redirect_stdout(io.StringIO()):
            gf.main()
        self.base = (self.out / "elf64-mini").read_bytes()

    def tearDown(self):
        gf.OUT = self.saved_out
        shutil.rmtree(self.out, ignore_errors=True)

    def test_positive_fixture_size_and_header_fields(self):
        # Layout from the module docstring: file is 0x120 = 288 bytes.
        self.assertEqual(len(self.base), 0x120)
        self.assertEqual(self.base[:4], b"\x7fELF")
        e_entry, e_phoff = struct.unpack_from("<QQ", self.base, 0x18)
        self.assertEqual(e_entry, 0x400100)
        self.assertEqual(e_phoff, 64)
        (e_phnum,) = struct.unpack_from("<H", self.base, 0x38)
        self.assertEqual(e_phnum, 2)

    def test_segment_payload_bytes_match_documented_pattern(self):
        # Documented: seg0 at 0x100 is 8 bytes 0x10..0x17; seg1 at 0x108 is
        # 16 bytes (0xA0 + i) & 0xFF. Expected built here, not read from gf.
        want_code = bytes(range(0x10, 0x18))
        want_data = bytes((0xA0 + i) & 0xFF for i in range(16))
        self.assertEqual(self.base[0x100:0x108], want_code)
        self.assertEqual(self.base[0x108:0x118], want_data)

    def test_both_segments_are_page_congruent(self):
        # gABI rule: p_vaddr % p_align == p_offset % p_align for each PT_LOAD.
        for ph_off, p_off, p_va in ((0x40, 0x100, 0x400100), (0x78, 0x108, 0x400108)):
            ptype, pflags, poff, pvaddr, _, filesz, memsz, palign = \
                struct.unpack_from("<IIQQQQQQ", self.base, ph_off)
            self.assertEqual(ptype, 1)
            self.assertEqual(poff, p_off)
            self.assertEqual(pvaddr, p_va)
            self.assertEqual(pvaddr % palign, poff % palign)

    def test_verifier_accepts_positive_fixture(self):
        p = subprocess.run([sys.executable, VERIFY, str(self.out / "elf64-mini")],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("ELFORACLE VERDICT valid", p.stdout)

    def test_all_ten_negatives_are_written(self):
        names = {p.stem for p in (self.out / "negative").glob("*.elf")}
        self.assertEqual(names, NEGATIVE_NAMES)

    def test_each_negative_is_rejected_by_independent_verifier(self):
        for name in sorted(NEGATIVE_NAMES):
            with self.subTest(name=name):
                p = subprocess.run(
                    [sys.executable, VERIFY, str(self.out / "negative" / f"{name}.elf")],
                    capture_output=True, text=True)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn("ELFORACLE VERDICT invalid", p.stdout)

    def test_negatives_are_single_field_mutations_of_base(self):
        # Each negative is a minimal mutation; the byte count of changes
        # stays small (interp adds a 56-byte header in place of zero padding).
        for name in sorted(NEGATIVE_NAMES):
            with self.subTest(name=name):
                img = (self.out / "negative" / f"{name}.elf").read_bytes()
                self.assertEqual(len(img), len(self.base))
                diff = sum(1 for a, b in zip(img, self.base) if a != b)
                self.assertLessEqual(diff, 56)

    def test_bad_magic_negative_changes_only_magic(self):
        img = (self.out / "negative" / "bad-magic.elf").read_bytes()
        self.assertEqual(img[:4], b"\x7fELG")
        self.assertEqual(img[4:], self.base[4:])

    def test_interp_negative_puts_interp_header_in_table(self):
        img = (self.out / "negative" / "interp-present.elf").read_bytes()
        (e_phnum,) = struct.unpack_from("<H", img, 0x38)
        self.assertEqual(e_phnum, 3)
        (ptype,) = struct.unpack_from("<I", img, 0xB0)
        self.assertEqual(ptype, 3)  # PT_INTERP


if __name__ == "__main__":
    unittest.main()
