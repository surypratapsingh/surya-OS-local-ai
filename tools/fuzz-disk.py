#!/usr/bin/env python3
"""Fuzz tools/verify-disk.py (work order W2, item 4).

Mutate random bytes of nova.hdd and require the verifier to report a
failure rather than crashing or passing. Three outcomes are counted
SEPARATELY, as the work order demands:

  crashes       the verifier raised an exception it did not convert into a
                report. Any crash count > 0 is a defect.

  false passes  the verifier reported success despite the mutation. Each is
                classified with the coverage map from verify-disk.py:
                  exact-checked  — MUST be 0. The verifier byte-checks that
                                   range, so a pass there is a verifier
                                   defect (the serious finding W2 warns of).
                  presence-only  — only non-zeroness is asserted there; a
                                   mutation to another non-zero value may
                                   pass by design (documented limit).
                  no-invariant   — free space, unused FAT slots, padding:
                                   no invariant exists to violate. Not a
                                   defect — the verifier's job is
                                   structural validity, not golden-image
                                   equality (a golden-image compare would
                                   share the builder's misconceptions).

  map-sanity    deterministic cross-check of the coverage map itself: a
                single-byte mutation inside every exact range must be
                detected. If the map ever drifts from the checks, this
                fails — the map cannot silently overstate coverage.

Seeded (default 0x4E4F5641 = "NOVA" little-endian) for reproducibility.

Exit 0 only if: crashes == 0, exact-checked false passes == 0, and every
map-sanity mutation was detected.
"""

from __future__ import annotations

import argparse
import bisect
import random
import sys
import time
from pathlib import Path

import importlib.util  # noqa: E402

# Load by path: the module file is 'verify-disk.py' (hyphenated), which the
# plain 'import' statement cannot name.
_spec = importlib.util.spec_from_file_location(
    "verify_disk", Path(__file__).resolve().parent / "verify-disk.py")
vd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vd)

DEFAULT_SEED = 0x4E4F5641  # "NOVA"


def classify(pos: int, starts: list[int], ranges: list[tuple[int, int, str]]) -> str:
    i = bisect.bisect_right(starts, pos) - 1
    if i >= 0:
        a, b, kind = ranges[i]
        if a <= pos < b:
            return kind
    return "none"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iterations", nargs="?", type=int, default=10000)
    ap.add_argument("--image", default="build/nova.hdd")
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=DEFAULT_SEED)
    ap.add_argument("--kernel-dir", default="kernel")
    ap.add_argument("--limine-bin", default=".freebuff/ref/limine/limine-binary")
    ap.add_argument("--kernel-elf", default=None)
    args = ap.parse_args()

    img_path = Path(args.image)
    img = bytearray(img_path.read_bytes())
    expect = vd.load_expectations(args.kernel_dir, args.limine_bin,
                                  kernel_elf=args.kernel_elf)
    ranges = vd.checked_ranges(bytes(img), expect)
    starts = [r[0] for r in ranges]
    n_exact = sum(b - a for a, b, k in ranges if k == "exact")
    n_presence = sum(b - a for a, b, k in ranges if k == "presence")
    n_ranges = len(ranges)

    print(f"fuzz-disk: image {img_path} ({len(img)} bytes), {args.iterations} iterations, "
          f"seed {args.seed:#x}")
    print(f"fuzz-disk: coverage map: {n_ranges} ranges — {n_exact} B exact-checked, "
          f"{n_presence} B presence-only, {len(img) - n_exact - n_presence} B no-invariant")

    # ---- map-sanity: prove the map matches the checks -------------------
    sanity_total = sanity_miss = 0
    for a, b, kind in ranges:
        if kind != "exact":
            continue
        for pos in (a, (a + b) // 2, b - 1):
            old = img[pos]
            img[pos] = old ^ 0xFF if old ^ 0xFF != old else old ^ 0x55
            try:
                fails = vd.verify_image(img, expect)
            except Exception as e:  # noqa: BLE001
                fails = None
                print(f"fuzz-disk: map-sanity CRASH at byte {pos}: {type(e).__name__}: {e}")
            img[pos] = old
            sanity_total += 1
            if not fails:
                sanity_miss += 1
                print(f"fuzz-disk: map-sanity MISS at byte {pos} "
                      f"(range {a}..{b}): mutation was NOT detected")
    print(f"fuzz-disk: map-sanity: {sanity_total - sanity_miss}/{sanity_total} "
          "targeted single-byte mutations inside exact ranges were detected")

    # ---- random single-byte mutations -----------------------------------
    rng = random.Random(args.seed)
    detected = false_pass = crashes = 0
    fp_by_kind = {"exact": 0, "presence": 0, "none": 0}
    fp_examples: dict[str, tuple[int, int, int]] = {}
    crash_examples: list[str] = []
    t0 = time.perf_counter()

    for _ in range(args.iterations):
        pos = rng.randrange(len(img))
        old = img[pos]
        new = old ^ (1 << rng.randrange(8))  # guaranteed different bit flip
        img[pos] = new
        try:
            fails = vd.verify_image(img, expect)
        except Exception as e:  # noqa: BLE001 — backstop; the verifier itself
            crashes += 1       # catches section-internal exceptions
            if len(crash_examples) < 5:
                crash_examples.append(f"byte {pos}: {type(e).__name__}: {e}")
        else:
            if fails:
                detected += 1
            else:
                false_pass += 1
                kind = classify(pos, starts, ranges)
                fp_by_kind[kind] += 1
                fp_examples.setdefault(kind, (pos, old, new))
        img[pos] = old

    dt = time.perf_counter() - t0

    print(f"fuzz-disk: detected:    {detected}/{args.iterations} mutations reported as failures "
          f"({dt / max(args.iterations, 1) * 1000:.1f} ms/verify)")
    print(f"fuzz-disk: false pass:  {false_pass}/{args.iterations} reported success, classified:")
    for kind in ("exact", "presence", "none"):
        note = {
            "exact": "  <- must be 0; >0 is a verifier defect",
            "presence": "  <- by design: only non-zeroness is asserted there",
            "none": "  <- free space/unused FAT slots: no invariant to violate",
        }[kind]
        line = f"fuzz-disk:   {kind:>8}: {fp_by_kind[kind]}{note}"
        if kind in fp_examples:
            pos, old, new = fp_examples[kind]
            line += f"  [e.g. byte {pos}: {old:#04x}->{new:#04x}]"
        print(line)
    print(f"fuzz-disk: crashes:     {crashes}")
    for c in crash_examples:
        print(f"fuzz-disk:   crash: {c}")

    ok = crashes == 0 and fp_by_kind["exact"] == 0 and sanity_miss == 0
    print(f"fuzz-disk: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
