#!/usr/bin/env python3
"""Create an UNSIGNED NOVA release manifest on the build machine.

    python tools/create-manifest.py --version 0.2.0 --sequence 2 \\
        --package mathd:0.2.0:build/mathd.bin \\
        --capabilities mathd:compute:expression,compute:verify \\
        --output build/release-0.2.0.manifest

--sequence is required and must be higher than every release ever accepted:
verify-manifest.py refuses anything else. Package order is install order.
Carry the output to the offline machine and sign it with sign-manifest.py.
Format: docs/manifest-format.md.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_trust as nt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help="release version, e.g. 0.2.0")
    ap.add_argument("--sequence", required=True, type=int,
                    help="release_sequence: higher than every release already accepted")
    ap.add_argument("--package", action="append", required=True, metavar="ID:VERSION:PATH")
    ap.add_argument("--capabilities", action="append", default=[], metavar="ID:NS:PERM[,NS:PERM]",
                    help="capabilities for one package id; omit for none")
    ap.add_argument("--description")
    # Bootchain (C4): Limine and kernel hashes for verified boot
    ap.add_argument("--limine-version", help="Limine version, e.g. 12.9.0")
    ap.add_argument("--limine-hash", help="Limine file SHA-256 (64 hex)")
    ap.add_argument("--limine-size", type=int, help="Limine file size in bytes")
    ap.add_argument("--kernel-id", help="kernel id, e.g. nova-kernel")
    ap.add_argument("--kernel-version", help="kernel version, e.g. 0.1.0")
    ap.add_argument("--kernel-hash", help="kernel file SHA-256 (64 hex)")
    ap.add_argument("--kernel-size", type=int, help="kernel file size in bytes")
    ap.add_argument("--public-key", type=Path, default=Path("kernel/trust/root-key.pub"))
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args(argv)

    caps: dict[str, list[str]] = {}
    for spec in args.capabilities:
        pid, sep, rest = spec.partition(":")
        if not sep or not rest:
            print(f"create-manifest: bad --capabilities {spec!r}", file=sys.stderr)
            return 2
        caps.setdefault(pid, []).extend(c for c in rest.split(",") if c)

    packages = []
    for spec in args.package:
        parts = spec.split(":", 2)  # the path may itself contain ':' (C:\...)
        if len(parts) != 3:
            print(f"create-manifest: bad --package {spec!r} (want ID:VERSION:PATH)", file=sys.stderr)
            return 2
        pid, pver, path = parts[0], parts[1], Path(parts[2])
        if not path.is_file():
            print(f"create-manifest: {path} not found", file=sys.stderr)
            return 2
        packages.append((pid, pver, path, caps.pop(pid, [])))
    if caps:
        print(f"create-manifest: --capabilities for unknown package id(s): {sorted(caps)}",
              file=sys.stderr)
        return 2

    try:
        public = nt.parse_public_key_pem(args.public_key.read_text(encoding="ascii"))
    except (OSError, ValueError) as e:
        print(f"create-manifest: cannot read public key {args.public_key}: {e}", file=sys.stderr)
        return 2
    if args.output.exists():
        print(f"create-manifest: {args.output} exists; refusing to overwrite", file=sys.stderr)
        return 2

    # Build bootchain section if Limine fields are present
    bootchain = None
    if args.limine_version or args.limine_hash or args.limine_size:
        if not (args.limine_version and args.limine_hash and args.limine_size):
            print("create-manifest: all of --limine-version, --limine-hash, --limine-size required",
                  file=sys.stderr)
            return 2
        if not (args.kernel_id and args.kernel_version and args.kernel_hash and args.kernel_size):
            print("create-manifest: bootchain requires all of --kernel-id, --kernel-version, "
                  "--kernel-hash, --kernel-size", file=sys.stderr)
            return 2
        # Validate hash format
        if len(args.limine_hash) != 64 or not all(c in "0123456789abcdef" for c in args.limine_hash):
            print(f"create-manifest: --limine-hash must be 64 lowercase hex, got {args.limine_hash!r}",
                  file=sys.stderr)
            return 2
        if len(args.kernel_hash) != 64 or not all(c in "0123456789abcdef" for c in args.kernel_hash):
            print(f"create-manifest: --kernel-hash must be 64 lowercase hex, got {args.kernel_hash!r}",
                  file=sys.stderr)
            return 2
        bootchain = {
            "limine": {
                "version": args.limine_version,
                "filename": "limine.bin",
                "sha256": args.limine_hash,
                "size": args.limine_size,
            },
            "kernel": {
                "id": args.kernel_id,
                "version": args.kernel_version,
                "filename": "nova-kernel.bin",
                "sha256": args.kernel_hash,
                "size": args.kernel_size,
            },
        }

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = nt.build_manifest(args.version, args.sequence, stamp, packages, public,
                                 args.description, bootchain)
    errs = nt.structure_errors(manifest, require_signature=False)
    if errs:
        for e in errs:
            print(f"create-manifest: FAIL {e}", file=sys.stderr)
        return 1
    nt.write_text_atomic(args.output, nt.dump_manifest(manifest))

    print(f"create-manifest: wrote {args.output} (UNSIGNED)")
    print(f"create-manifest: release {args.version}, release_sequence {args.sequence}, "
          f"key {nt.fingerprint(public)}")
    if "bootchain" in manifest:
        b = manifest["bootchain"]
        print(f"  bootchain: Limine {b['limine']['version']} {b['limine']['size']} B "
              f"sha256 {b['limine']['sha256']}")
        print(f"             kernel {b['kernel']['version']} {b['kernel']['size']} B "
              f"sha256 {b['kernel']['sha256']}")
    for p in manifest["packages"]:
        print(f"  {p['sequence']}. {p['id']} {p['version']}  {p['filename']}  {p['size']} B  "
              f"sha256 {p['sha256']}  caps {p['capabilities'] or '[]'}")
    print("create-manifest: next, on the offline machine: "
          f"python tools/sign-manifest.py {args.output.name} <path>/root.priv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
