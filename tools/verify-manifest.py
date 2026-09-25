#!/usr/bin/env python3
"""Verify a signed NOVA release manifest before anything from it is installed.

    python tools/verify-manifest.py release-0.2.0.manifest \\
        --files /media/stick/release --state /var/nova/accepted.json --accept

Checks, in order: the key fingerprint, the Ed25519 signature, every structural
rule, replay/rollback against --state, and each package file's size and SHA-256
under --files. --accept records the new release_sequence in --state, and only
after every check has passed. Exit 0 = acceptable, 1 = rejected, 2 = usage or
I/O error. Format: docs/manifest-format.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_trust as nt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--public-key", type=Path, default=Path("kernel/trust/root-key.pub"))
    ap.add_argument("--files", type=Path, help="directory holding the package files")
    ap.add_argument("--state", type=Path,
                    help='replay state: {"release_sequence": N}, the last accepted release')
    ap.add_argument("--accept", action="store_true",
                    help="after a PASS, record this release_sequence in --state")
    args = ap.parse_args(argv)
    if args.accept and not args.state:
        ap.error("--accept requires --state")

    try:
        public = nt.parse_public_key_pem(args.public_key.read_text(encoding="ascii"))
        manifest = nt.parse_json(args.manifest.read_text(encoding="utf-8"))
        last = nt.read_state(args.state) if args.state else None
    except (OSError, ValueError) as e:
        print(f"verify-manifest: {e}", file=sys.stderr)
        return 2
    if args.files is not None and not args.files.is_dir():
        print(f"verify-manifest: --files {args.files} is not a directory", file=sys.stderr)
        return 2

    print(f"verify-manifest: {args.manifest} against key {nt.fingerprint(public)}")
    failures = nt.verify_manifest(manifest, public, last, args.files)
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        print(f"verify-manifest: REJECTED ({len(failures)} failure(s)); install nothing from it")
        return 1

    rel = manifest["release"]
    print(f"  ok   signature valid; release {rel['version']}, release_sequence "
          f"{rel['release_sequence']}")
    print("  ok   structure valid" + (f"; newer than last accepted {last}" if last is not None
                                       else "; replay NOT checked (no --state)"))
    if args.files is not None:
        print(f"  ok   {len(manifest['packages'])} package file(s) match size and sha256")
    else:
        print("  --   package files NOT checked (no --files)")
    if args.accept:
        nt.write_state(args.state, rel["release_sequence"])
        print(f"verify-manifest: recorded release_sequence {rel['release_sequence']} in {args.state}")
    print("verify-manifest: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
