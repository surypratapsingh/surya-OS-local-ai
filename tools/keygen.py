#!/usr/bin/env python3
"""Generate the NOVA root signing key. Run it ONLY on the owner's offline machine.

Writes <out-dir>/root.priv (PKCS#8 PEM, owner-only) and <out-dir>/root.pub
(SubjectPublicKeyInfo PEM). Copy root.pub, never root.priv, to
kernel/trust/root-key.pub on the build machine. The ceremony is in
docs/root-key-ceremony.md.

    python tools/keygen.py --out-dir /media/offline/nova-root-key
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_trust as nt  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=Path("nova-root-key"))
    args = ap.parse_args(argv)

    priv = args.out_dir / "root.priv"
    pub = args.out_dir / "root.pub"
    for p in (priv, pub):
        if p.exists():
            print(f"keygen: {p} already exists; refusing to overwrite a root key", file=sys.stderr)
            return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(args.out_dir, 0o700)
    except OSError:
        pass
    seed = secrets.token_bytes(32)
    public = nt.public_key(seed)
    # O_EXCL fails instead of clobbering, and the 0600 mode applies at creation.
    fd = os.open(priv, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
        f.write(nt.private_key_pem(seed))
    pub.write_text(nt.public_key_pem(public), encoding="ascii", newline="\n")

    print(f"keygen: wrote {priv} (PRIVATE: keep offline, never copy to a networked machine)")
    print(f"keygen: wrote {pub} (public: copy to kernel/trust/root-key.pub)")
    print(f"keygen: fingerprint {nt.fingerprint(public)}")
    print("keygen: write the fingerprint down on paper; every release will show it")
    if os.name == "nt":
        print("keygen: WARNING: Windows does not apply the 0600 mode; the private key is "
              "protected only by this account's folder permissions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
