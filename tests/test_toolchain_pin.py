#!/usr/bin/env python3
"""C5-pin: the Rust toolchain must be an exact version, never a moving channel.

A channel such as "stable" or "nightly" changes the kernel hash between
builds, so reproducible builds (C5) would break without any source change.
"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    tc = tomllib.loads((ROOT / "rust-toolchain.toml").read_text(encoding="utf-8"))["toolchain"]
    errors = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", tc.get("channel", "")):
        errors.append(f"channel {tc.get('channel')!r} is not an exact x.y.z version")
    if "x86_64-unknown-none" not in tc.get("targets", []):
        errors.append("target x86_64-unknown-none missing")
    for e in errors:
        print("FAIL:", e)
    if not errors:
        print("ok: toolchain pinned to", tc["channel"])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
