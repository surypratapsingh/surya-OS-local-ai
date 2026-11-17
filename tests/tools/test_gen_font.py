"""Unit tests for tools/gen-font.py (stdlib unittest only).

Each test points the tool's REF and OUT globals at a temp directory and writes
a small reference in the format the parser documents. Expected rows are the
rows the test wrote into the reference, so the tool is checked against the
input format, not against its own output.
"""

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "gen-font.py"))

_spec = importlib.util.spec_from_file_location("gen_font", TOOL)
gf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gf)


def glyph_rows(code):
    # Eight distinct bytes per glyph, derived from the code point, so a wrong
    # row order or a shifted glyph shows up in the output.
    return [(code + i * 7) & 0xFF for i in range(8)]


def reference_text(codes, drop=()):
    lines = ["static const unsigned char font8x8_basic[128][8] = {"]
    for c in codes:
        if c in drop:
            continue
        rows = ", ".join(f"0x{r:02X}" for r in glyph_rows(c))
        lines.append(f"    {{ {rows} }}, // U+{c:04X} (glyph)")
    lines.append("};")
    return "\n".join(lines) + "\n"


class GenFontTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gen-font-test-"))
        self.saved = (gf.REF, gf.OUT)
        gf.REF = self.dir / "font8x8_basic.h"
        gf.OUT = self.dir / "out" / "font_data.rs"
        gf.OUT.parent.mkdir(parents=True)

    def tearDown(self):
        gf.REF, gf.OUT = self.saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def all_codes(self):
        return list(range(gf.FIRST, gf.LAST + 1))

    def test_parse_reads_every_glyph_row_in_order(self):
        gf.REF.write_text(reference_text(self.all_codes()), encoding="utf-8")
        glyphs = gf.parse_reference()
        self.assertEqual(len(glyphs), 95)
        self.assertEqual(glyphs[0x41], glyph_rows(0x41))

    def test_generate_writes_95_glyphs_with_expected_bytes(self):
        gf.REF.write_text(reference_text(self.all_codes()), encoding="utf-8")
        self.assertEqual(gf.main(), 0)
        out = gf.OUT.read_text(encoding="utf-8")
        self.assertIn("pub const GLYPH_COUNT: usize = 95;", out)
        self.assertIn("pub const FIRST_CHAR: u8 = 0x20;", out)
        self.assertIn("pub const LAST_CHAR: u8 = 0x7E;", out)
        # Row bytes for 'A' (0x41), in reference order.
        want = ", ".join(f"0x{r:02X}" for r in glyph_rows(0x41))
        self.assertIn(want + ",", out)
        # Table has 95 glyph lines, one per code point.
        self.assertEqual(out.count("\n    0x"), 95)

    def test_missing_glyph_fails(self):
        gf.REF.write_text(reference_text(self.all_codes(), drop={0x41}), encoding="utf-8")
        self.assertEqual(gf.main(), 1)
        self.assertFalse(gf.OUT.exists())

    def test_missing_reference_fails(self):
        self.assertEqual(gf.main(), 1)
        self.assertFalse(gf.OUT.exists())

    def test_backslash_and_quote_are_escaped_in_comments(self):
        self.assertEqual(gf.rust_escape(0x5C), "\\\\")
        self.assertEqual(gf.rust_escape(0x27), "\\'")
        self.assertEqual(gf.rust_escape(0x41), "A")


if __name__ == "__main__":
    unittest.main()
