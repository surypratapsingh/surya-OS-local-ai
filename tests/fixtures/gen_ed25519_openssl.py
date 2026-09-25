#!/usr/bin/env python3
"""Generate tests/fixtures/ed25519-openssl.json, the oracle for tools/nova_trust.py.

Every expected value in the JSON comes from OpenSSL, through the `cryptography`
package, which shares no code with nova_trust.py. This script is dev-only. The
tests read the committed JSON and need nothing beyond the standard library.

    python -m pip install cryptography        # dev machine only
    python tests/fixtures/gen_ed25519_openssl.py

Output is deterministic: seeds and messages derive from fixed labels, and
Ed25519 signing is deterministic (RFC 8032 section 5.1.6). Re-running with the
same OpenSSL reproduces the file byte for byte.
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

import cryptography
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends.openssl import backend
from cryptography.hazmat.primitives import serialization as ser
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

OUT = Path(__file__).resolve().parent / "ed25519-openssl.json"
N_VECTORS = 32
MSG_LENGTHS = [0, 1, 2, 3, 31, 32, 33, 63, 64, 65, 127, 128, 129, 255, 256, 1000]
FLIP_BITS = [0, 7, 100, 255, 256, 263, 400, 511]  # spread across R (0-255) and S (256-511)
L = 2**252 + 27742317777372353535851937790883648493  # RFC 8032 section 5.1
P = 2**255 - 19


def det_bytes(label: str, n: int) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha512(f"{label}/{i}".encode()).digest()
        i += 1
    return out[:n]


def raw_pub(sk) -> bytes:
    return sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)


def openssl_accepts(pub: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except InvalidSignature:
        return False


def main() -> int:
    keys = [Ed25519PrivateKey.from_private_bytes(det_bytes(f"nova-ed25519-seed-{i}", 32))
            for i in range(N_VECTORS)]
    vectors = []
    for i, sk in enumerate(keys):
        seed = sk.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption())
        pub = raw_pub(sk)
        msg = det_bytes(f"nova-ed25519-msg-{i}", MSG_LENGTHS[i % len(MSG_LENGTHS)])
        sig = sk.sign(msg)
        negatives = []

        def neg(kind, p, m, s):
            negatives.append({"kind": kind, "public": p.hex(), "message": m.hex(),
                              "signature": s.hex(), "openssl_accepts": openssl_accepts(p, m, s)})

        for bit in FLIP_BITS:
            flipped = bytearray(sig)
            flipped[bit // 8] ^= 1 << (bit % 8)
            neg(f"signature bit {bit} flipped", pub, msg, bytes(flipped))
        changed = (bytes([msg[0] ^ 1]) + msg[1:]) if msg else b"\x00"
        neg("message changed", pub, changed, sig)
        s_plus_l = int.from_bytes(sig[32:], "little") + L
        if s_plus_l < 2**256:
            neg("S + L (malleable encoding)", pub, msg, sig[:32] + s_plus_l.to_bytes(32, "little"))
        neg("R with y >= p (non-canonical)", pub, msg, (P + 1).to_bytes(32, "little") + sig[32:])
        neg("wrong public key", raw_pub(keys[(i + 1) % N_VECTORS]), msg, sig)

        v = {"seed": seed.hex(), "public": pub.hex(), "message": msg.hex(),
             "signature": sig.hex(), "negatives": negatives}
        if i < 3:
            v["private_pem"] = sk.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8,
                                                ser.NoEncryption()).decode("ascii")
            v["public_pem"] = sk.public_key().public_bytes(
                ser.Encoding.PEM, ser.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
        vectors.append(v)

    # One manifest signed by OpenSSL. The signing input is built here straight
    # from docs/manifest-format.md ("Signing"), not by calling nova_trust.
    sk = keys[0]
    pub = raw_pub(sk)
    manifest = {
        "manifest_version": 1,
        "release": {"version": "0.1.0", "release_sequence": 3,
                    "timestamp": "2026-09-25T00:00:00Z", "description": "fixture"},
        "trust": {"algorithm": "Ed25519",
                  "key_fingerprint": "sha256:" + hashlib.sha256(pub).hexdigest()},
        "packages": [
            {"id": "mathd", "version": "0.1.0", "filename": "mathd.bin",
             "sha256": hashlib.sha256(b"mathd payload").hexdigest(), "size": 13,
             "sequence": 1, "capabilities": ["compute:expression", "compute:verify"]},
            {"id": "novacore", "version": "0.1.0", "filename": "novacore.tar",
             "sha256": hashlib.sha256(b"novacore payload").hexdigest(), "size": 16,
             "sequence": 2, "capabilities": []},
        ],
    }
    signing_input = b"NOVA-MANIFEST-v1\n" + json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    manifest["signature"] = base64.b64encode(sk.sign(signing_input)).decode("ascii")

    doc = {
        "provenance": {
            "generator": "tests/fixtures/gen_ed25519_openssl.py",
            "cryptography": cryptography.__version__,
            "openssl": backend.openssl_version_text(),
        },
        "vectors": vectors,
        "manifest": {"public": pub.hex(), "signing_input": signing_input.hex(),
                     "json": json.dumps(manifest, indent=2, sort_keys=True)},
    }
    OUT.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="ascii", newline="\n")
    n_neg = sum(len(v["negatives"]) for v in vectors)
    accepted = sum(n["openssl_accepts"] for v in vectors for n in v["negatives"])
    print(f"wrote {OUT.name}: {len(vectors)} vectors, {n_neg} negative cases "
          f"(OpenSSL accepted {accepted} of them), 1 signed manifest; "
          f"{backend.openssl_version_text()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
