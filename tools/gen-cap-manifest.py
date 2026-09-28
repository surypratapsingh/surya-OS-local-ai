#!/usr/bin/env python3
"""Generate the K4 capability-oracle package and its signed manifest.

Work order K4, host-side oracle for kernel/scripts/test-cap.sh (check stage
11). Everything here is deterministic (fixed key seed, fixed package bytes,
fixed timestamp), so two runs produce byte-identical output.

What it does:
  * parses the CAP_REFS vocabulary OUT of kernel/src/cap.rs source text —
    the oracle input is the kernel file, not the compiled binary, so the
    host side and the kernel side cannot share a compiled constant;
  * builds one package ("cap-oracle") granting exactly the row the kernel
    gate's done-when subject declares ("fs:read") and signs a manifest with
    the REAL C2 signing path (tools/nova_trust.py, RFC 8032 Ed25519);
  * the key is a throwaway test key generated from a fixed seed. It is NOT
    the owner's root key (docs/root-key-ceremony.md: that key is created
    offline and never lives on a networked machine); this one exists only
    so test-cap.sh can re-sign tampered manifests for its fail-closed
    section. It is written into build/ and never committed.

Output (build/cap-oracle/): oracle-package.bin, manifest.json (signed),
package.caps, root.priv, root.pub.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_trust as nt  # noqa: E402

# The row the kernel gate's done-when subject declares. Cross-checked below
# against the vocabulary parsed from cap.rs so the two cannot drift.
SUBJECT_GRANT = "fs:read"

# The kernel gate's done-when subject must match this position in the
# parsed vocabulary (cap.rs Resource discriminant 3). Checked, not assumed.
SUBJECT_VOCAB_INDEX = 3

CAP_REFS_RE = re.compile(r"CAP_REFS[^=]*=\s*&\[(.*?)\];", re.DOTALL)


def parse_cap_refs(cap_src_text: str) -> tuple[list[str], str]:
    """Extract the CAP_REFS table from cap.rs source.

    Returns (grantable wire names in order, raw table text). The table's
    last entry is the unquoted identifier RESERVED_NAME (the kernel-reserved
    arm); the quoted names are the grantable vocabulary.
    """
    m = CAP_REFS_RE.search(cap_src_text)
    if m is None:
        raise SystemExit(
            "gen-cap-manifest: CAP_REFS table not found in cap.rs - the "
            "host oracle's anchor moved; update this parser with the kernel"
        )
    raw = m.group(1)
    return re.findall(r'"([^"]+)"', raw), raw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("build/cap-oracle"))
    ap.add_argument("--cap-src", type=Path, default=Path("kernel/src/cap.rs"))
    args = ap.parse_args(argv)

    vocab, raw_table = parse_cap_refs(args.cap_src.read_text(encoding="utf-8"))
    # The sentinel arm must still be the table's last entry, or this oracle
    # is not anchored on the table it believes it is.
    if not raw_table.rstrip().endswith("RESERVED_NAME,"):
        print("gen-cap-manifest: CAP_REFS no longer ends with RESERVED_NAME - "
              "the kernel-reserved arm moved; re-verify the anchor",
              file=sys.stderr)
        return 1
    grantable = vocab
    if len(grantable) != 7:
        print(f"gen-cap-manifest: unexpected vocabulary from cap.rs: {grantable}",
              file=sys.stderr)
        return 1
    if SUBJECT_VOCAB_INDEX >= len(grantable) or grantable[SUBJECT_VOCAB_INDEX] != SUBJECT_GRANT:
        print(
            "gen-cap-manifest: cap.rs vocabulary drifted: index "
            f"{SUBJECT_VOCAB_INDEX} is {grantable[SUBJECT_VOCAB_INDEX]!r}, "
            f"expected {SUBJECT_GRANT!r}",
            file=sys.stderr,
        )
        return 1

    args.out.mkdir(parents=True, exist_ok=True)

    # Fixed-seed throwaway test key (NOT the owner's root key; see module
    # doc). Deterministic so the signed manifest is byte-identical per run.
    secret = hashlib.sha256(b"NOVA cap-oracle test key seed 2026-09-28").digest()
    public = nt.public_key(secret)
    (args.out / "root.priv").write_text(nt.private_key_pem(secret), encoding="ascii")
    (args.out / "root.pub").write_text(nt.public_key_pem(public), encoding="ascii")

    # The package payload: deterministic bytes, no meaning beyond existing
    # as a hashable file for the manifest.
    pkg = args.out / "oracle-package.bin"
    pkg.write_bytes(b"NOVA-CAP-ORACLE v1: fs:read-only subject\n")

    caps = [SUBJECT_GRANT]
    (args.out / "package.caps").write_text("".join(c + "\n" for c in caps),
                                           encoding="utf-8")

    # The C2 verifier in this tree requires the bootchain section in every
    # manifest (_TOP in nova_trust.py), although docs/manifest-format.md
    # calls it optional. Not this work order's bug to fix (AGENTS.md rule
    # 11); the generator complies with the code by carrying a deterministic
    # dummy bootchain whose files live beside the package.
    limine = args.out / "limine.bin"
    kernel = args.out / "kernel.bin"
    limine.write_bytes(b"NOVA-CAP-ORACLE deterministic fake limine stage\n")
    kernel.write_bytes(b"NOVA-CAP-ORACLE deterministic fake kernel image\n")
    bootchain = {
        "limine": {
            "version": "12.9.0",
            "filename": "limine.bin",
            "sha256": nt.sha256_file(limine),
            "size": limine.stat().st_size,
        },
        "kernel": {
            "id": "nova-kernel",
            "version": "0.1.0",
            "filename": "kernel.bin",
            "sha256": nt.sha256_file(kernel),
            "size": kernel.stat().st_size,
        },
    }

    manifest = nt.build_manifest(
        "0.1.0", 1, "2026-09-28T00:00:00Z",
        [("cap-oracle", "0.1.0", pkg, caps)],
        public,
        description="K4 capability oracle: fs:read-only subject (see "
                    "kernel/src/capselftest.rs)",
        bootchain=bootchain,
    )
    signed = nt.sign_manifest(manifest, secret)
    nt.write_text_atomic(args.out / "manifest.json", nt.dump_manifest(signed))

    print(f"gen-cap-manifest: vocabulary from {args.cap_src} ({len(grantable)} grantable):")
    for i, name in enumerate(grantable):
        mark = " <- subject grant" if name == SUBJECT_GRANT else ""
        print(f"  {i}: {name}{mark}")
    print(f"gen-cap-manifest: signed manifest for subject caps {caps} "
          f"-> {args.out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
