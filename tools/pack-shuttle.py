#!/usr/bin/env python3
"""Build and sign an offline NOVA shuttle package using a throwaway TEST key.

    python tools/pack-shuttle.py --out-dir build/shuttle-pkg \
        --package mathd:0.2.0:build/mathd.bin \
        --capabilities mathd:compute:expression,compute:verify \
        --sequence 10

Uses throwaway Ed25519 root keys explicitly labelled TEST.
Creates the package manifest and signs it with nova_trust.
The output directory contains the package files and the signed release.manifest,
ready for verification by tools/verify-manifest.py and offline pendrive transport.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_trust as nt  # noqa: E402


TEST_KEY_SEED = hashlib.sha256(b"NOVA_SHUTTLE_TEST_ROOT_KEY_ED25519_20261103").digest()


def ensure_test_key(key_dir: Path) -> tuple[Path, Path]:
    """Ensure a deterministic throwaway TEST keypair exists in key_dir."""
    key_dir.mkdir(parents=True, exist_ok=True)
    priv_path = key_dir / "TEST-root.priv"
    pub_path = key_dir / "TEST-root.pub"

    if not priv_path.exists() or not pub_path.exists():
        priv_pem = nt.private_key_pem(TEST_KEY_SEED)
        pub_pem = nt.public_key_pem(nt.public_key(TEST_KEY_SEED))
        priv_path.write_text(priv_pem, encoding="ascii")
        pub_path.write_text(pub_pem, encoding="ascii")

    return priv_path, pub_path


def pack_shuttle(
    out_dir: Path,
    packages: list[tuple[str, str, Path, list[str]]],  # (id, version, src_path, capabilities)
    sequence: int = 1,
    version: str = "0.1.0",
    description: str = "STUB test shuttle package",
    key_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Pack payload files into out_dir and write a signed release.manifest.

    Returns (manifest_path, public_key_path).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if key_dir is None:
        key_dir = out_dir / "test-keys"
    priv_path, pub_path = ensure_test_key(key_dir)

    print("pack-shuttle: STUB test root key used")

    # 1. Copy payload files into out_dir and prepare package records
    copied_packages = []
    for pid, pver, src_path, caps in packages:
        src = Path(src_path)
        if not src.is_file():
            raise FileNotFoundError(f"package file {src} not found")
        dest = out_dir / src.name
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        copied_packages.append((pid, pver, dest, caps))

    # 2. Build unsigned manifest
    public_key = nt.parse_public_key_pem(pub_path.read_text(encoding="ascii"))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = nt.build_manifest(
        version=version,
        release_sequence=sequence,
        timestamp=stamp,
        packages=copied_packages,
        public=public_key,
        description=description,
    )

    # 3. Check structure
    errs = nt.structure_errors(manifest, require_signature=False)
    if errs:
        raise ValueError(f"manifest structure errors: {errs}")

    # 4. Sign manifest
    secret = nt.parse_private_key_pem(priv_path.read_text(encoding="ascii"))
    signed_manifest = nt.sign_manifest(manifest, secret)

    # 5. Write signed manifest
    manifest_path = out_dir / "release.manifest"
    nt.write_text_atomic(manifest_path, nt.dump_manifest(signed_manifest))

    return manifest_path, pub_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path, help="output directory for package and manifest")
    ap.add_argument("--version", default="0.1.0", help="release version")
    ap.add_argument("--sequence", type=int, default=1, help="release sequence number")
    ap.add_argument("--description", default="STUB test shuttle package")
    ap.add_argument("--package", action="append", required=True, metavar="ID:VERSION:PATH",
                    help="package specification, e.g. mathd:0.2.0:build/mathd.bin")
    ap.add_argument("--capabilities", action="append", default=[], metavar="ID:NS:PERM[,NS:PERM]",
                    help="capabilities for package ID")
    ap.add_argument("--key-dir", type=Path, default=None, help="directory containing or receiving TEST root keys")
    args = ap.parse_args(argv)

    caps: dict[str, list[str]] = {}
    for spec in args.capabilities:
        pid, sep, rest = spec.partition(":")
        if not sep or not rest:
            print(f"pack-shuttle: bad --capabilities {spec!r}", file=sys.stderr)
            return 2
        caps.setdefault(pid, []).extend(c for c in rest.split(",") if c)

    packages = []
    for spec in args.package:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            print(f"pack-shuttle: bad --package {spec!r} (want ID:VERSION:PATH)", file=sys.stderr)
            return 2
        pid, pver, path = parts[0], parts[1], Path(parts[2])
        if not path.is_file():
            print(f"pack-shuttle: {path} not found", file=sys.stderr)
            return 2
        packages.append((pid, pver, path, caps.pop(pid, [])))

    if caps:
        print(f"pack-shuttle: --capabilities for unknown package id(s): {sorted(caps)}", file=sys.stderr)
        return 2

    try:
        manifest_path, pub_path = pack_shuttle(
            out_dir=args.out_dir,
            packages=packages,
            sequence=args.sequence,
            version=args.version,
            description=args.description,
            key_dir=args.key_dir,
        )
    except Exception as e:
        print(f"pack-shuttle: error: {e}", file=sys.stderr)
        return 1

    print(f"pack-shuttle: successfully packed {len(packages)} package(s) into {args.out_dir}")
    print(f"pack-shuttle: manifest: {manifest_path}")
    print(f"pack-shuttle: test public key: {pub_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
