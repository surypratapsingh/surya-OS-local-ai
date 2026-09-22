#!/usr/bin/env python3
"""Verify the kernel's generated font matches the vendored reference.

Parses .freebuff/ref/font8x8_basic.h (dhepper/font8x8, public domain) and
compares every printable ASCII glyph byte-for-byte against the table the
kernel actually embeds (kernel/src/font/font_data.rs).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / ".freebuff" / "ref" / "font8x8_basic.h"
GEN = ROOT / "kernel" / "src" / "font" / "font_data.rs"


def parse_reference() -> dict[int, list[int]]:
    glyphs: dict[int, list[int]] = {}
    entry = re.compile(
        r"\{\s*(0x[0-9A-Fa-f]{2}(?:\s*,\s*0x[0-9A-Fa-f]{2}){7})\s*\}"
        r"\s*,?\s*//\s*U\+([0-9A-Fa-f]{4})"
    )
    for m in entry.finditer(REF.read_text(encoding="utf-8", errors="replace")):
        cp = int(m.group(2), 16)
        glyphs[cp] = [int(x, 16) for x in m.group(1).split(",")]
    return glyphs


def parse_generated() -> tuple[int, int, list[int]]:
    text = GEN.read_text(encoding="utf-8")
    first = int(re.search(r"FIRST_CHAR:\s*u8\s*=\s*(0x[0-9A-Fa-f]+)", text).group(1), 16)
    last = int(re.search(r"LAST_CHAR:\s*u8\s*=\s*(0x[0-9A-Fa-f]+)", text).group(1), 16)
    body = re.search(
        r"GLYPHS:\s*\[u8;\s*GLYPH_COUNT \* 8\]\s*=\s*\[(.*?)\];", text, re.S
    ).group(1)
    vals = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]{2}", body)]
    return first, last, vals


def main() -> int:
    failures: list[str] = []
    if not REF.exists():
        failures.append(f"reference font not found: {REF}")
        ref = {}
    else:
        ref = parse_reference()

    first, last, gen = parse_generated()
    count = last - first + 1
    expected = count * 8
    if len(gen) != expected:
        failures.append(f"generated table has {len(gen)} bytes, expected {expected}")

    for cp in range(first, last + 1):
        want = ref.get(cp)
        if want is None:
            failures.append(f"U+{cp:04X} missing from reference parse")
            continue
        off = (cp - first) * 8
        got = gen[off:off + 8]
        if got != want:
            failures.append(f"U+{cp:04X} mismatch: gen={got} ref={want}")

    if failures:
        print("FONT TEST FAILED:")
        for f in failures[:20]:
            print(" -", f)
        return 1
    print(f"font: {count} glyphs (U+{first:04X}..U+{last:04X}) match reference byte-for-byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
