#!/usr/bin/env python3
"""Sign a NOVA release manifest. Run it ONLY on the owner's offline machine.

    python tools/sign-manifest.py release-0.2.0.manifest /media/offline/nova-root-key/root.priv

Prints exactly what is being signed (every package and its hash) before
signing. Refuses malformed manifests, and manifests whose key_fingerprint names
a different key. The manifest file is rewritten in place with the signature.
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
    ap.add_argument("private_key", type=Path)
    args = ap.parse_args(argv)

    try:
        manifest = nt.parse_json(args.manifest.read_text(encoding="utf-8"))
        secret = nt.parse_private_key_pem(args.private_key.read_text(encoding="ascii"))
    except (OSError, ValueError) as e:
        print(f"sign-manifest: {e}", file=sys.stderr)
        return 2
    if isinstance(manifest, dict) and "signature" in manifest:
        print("sign-manifest: note: replacing the existing signature")

    try:
        signed = nt.sign_manifest(manifest, secret)
    except nt.ManifestError as e:
        print(f"sign-manifest: FAIL {e}", file=sys.stderr)
        return 1

    rel = signed["release"]
    print(f"sign-manifest: signing release {rel['version']}, release_sequence "
          f"{rel['release_sequence']}, with key {signed['trust']['key_fingerprint']}")
    for p in signed["packages"]:
        print(f"  {p['sequence']}. {p['id']} {p['version']}  {p['filename']}  {p['size']} B  "
              f"sha256 {p['sha256']}  caps {p['capabilities'] or '[]'}")
    nt.write_text_atomic(args.manifest, nt.dump_manifest(signed))
    print(f"sign-manifest: wrote signature into {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
