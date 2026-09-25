"""NOVA release trust: Ed25519 signatures and signed release manifests.

Python standard library only (AGENTS.md rule 12). The four CLIs (keygen.py,
create-manifest.py, sign-manifest.py, verify-manifest.py) and the tests all run
this one module, so what the tests prove is what the tools do.

Ed25519 follows RFC 8032 section 5.1; section numbers are cited inline. It is
not constant-time, because Python integers leak timing. Signing must therefore
run only on the owner's offline machine (docs/root-key-ceremony.md).
Verification handles only public data, so its timing does not matter.

The manifest format is specified in docs/manifest-format.md.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

# --------------------------------------------------------------- Ed25519 ---

# RFC 8032 section 5.1: field prime, group order, curve constant d.
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)  # section 5.1.3, step 3


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


# Points use extended homogeneous coordinates (X, Y, Z, T) with x = X/Z,
# y = Y/Z, x*y = T/Z. The addition formula is from section 5.1.4.
def _point_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _scalar_mult(s: int, p):
    q = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            q = _point_add(q, p)
        p = _point_add(p, p)
        s >>= 1
    return q


def _point_equal(p, q) -> bool:
    return ((p[0] * q[2] - q[0] * p[2]) % _P == 0
            and (p[1] * q[2] - q[1] * p[2]) % _P == 0)


def _recover_x(y: int, sign: int):
    """Section 5.1.3 steps 2-4. Returns None when y is not a curve point."""
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_BY = 4 * pow(5, _P - 2, _P) % _P  # section 5.1: the base point's y is 4/5
_BX = _recover_x(_BY, 0)           # ...and its x is the even root
_B = (_BX, _BY, 1, _BX * _BY % _P)


def _encode_point(p) -> bytes:  # section 5.1.2
    zinv = pow(p[2], _P - 2, _P)
    x, y = p[0] * zinv % _P, p[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(s: bytes):  # section 5.1.3
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _expand_secret(secret: bytes):  # section 5.1.5, steps 1-3
    if len(secret) != 32:
        raise ValueError("an Ed25519 secret key is exactly 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8  # clear the low 3 bits and bits 254-255...
    a |= 1 << 254        # ...then set bit 254
    return a, h[32:]


def public_key(secret: bytes) -> bytes:
    a, _prefix = _expand_secret(secret)
    return _encode_point(_scalar_mult(a, _B))


def sign(secret: bytes, message: bytes) -> bytes:  # section 5.1.6
    a, prefix = _expand_secret(secret)
    pub = _encode_point(_scalar_mult(a, _B))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    big_r = _encode_point(_scalar_mult(r, _B))
    k = int.from_bytes(_sha512(big_r + pub + message), "little") % _L
    return big_r + ((r + k * a) % _L).to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Section 5.1.7, using the cofactorless equation [S]B = R + [k]A, which
    that section permits."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = _decode_point(public)
    r_point = _decode_point(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:  # a non-canonical S would make signatures malleable
        return False
    k = int.from_bytes(_sha512(signature[:32] + public + message), "little") % _L
    return _point_equal(_scalar_mult(s, _B),
                        _point_add(r_point, _scalar_mult(k, a_point)))


# ------------------------------------------------------------- key files ---

# For Ed25519, the DER encoding is a fixed prefix followed by the 32 key bytes:
# RFC 8410 section 7 (PKCS #8, private) and section 4 (SubjectPublicKeyInfo).
# The OID is id-Ed25519, 1.3.101.112.
_PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def _unpem(text: str, label: str) -> bytes:
    m = re.search(rf"-----BEGIN {label}-----\s*(.*?)\s*-----END {label}-----",
                  text, re.S)
    if not m:
        raise ValueError(f"no '{label}' PEM block found")
    return base64.b64decode("".join(m.group(1).split()), validate=True)


def private_key_pem(secret: bytes) -> str:
    return _pem("PRIVATE KEY", _PKCS8_PREFIX + secret)


def public_key_pem(public: bytes) -> str:
    return _pem("PUBLIC KEY", _SPKI_PREFIX + public)


def parse_private_key_pem(text: str) -> bytes:
    der = _unpem(text, "PRIVATE KEY")
    if len(der) != 48 or not der.startswith(_PKCS8_PREFIX):
        raise ValueError("not an unencrypted Ed25519 PKCS#8 private key")
    return der[16:]


def parse_public_key_pem(text: str) -> bytes:
    der = _unpem(text, "PUBLIC KEY")
    if len(der) != 44 or not der.startswith(_SPKI_PREFIX):
        raise ValueError("not an Ed25519 SubjectPublicKeyInfo public key")
    return der[12:]


def fingerprint(public: bytes) -> str:
    """SHA-256 of the raw 32-byte key, not of the PEM file, whose bytes change
    under line-ending conversion (git autocrlf on Windows)."""
    return "sha256:" + hashlib.sha256(public).hexdigest()


# -------------------------------------------------------------- manifests ---

MANIFEST_VERSION = 1
# Domain separation: a manifest signature can never be valid for any other
# message the root key signs.
SIGNING_CONTEXT = b"NOVA-MANIFEST-v1\n"


class ManifestError(ValueError):
    pass


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ManifestError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _no_float(text):
    raise ManifestError(f"non-integer number {text} (manifests use integers only)")


def _no_constant(text):
    raise ManifestError(f"{text} is not allowed in a manifest")


def parse_json(text: str):
    """Strict JSON: duplicate keys, floats, NaN and Infinity are errors."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                          parse_float=_no_float, parse_constant=_no_constant)
    except json.JSONDecodeError as e:
        raise ManifestError(f"not valid JSON: {e}") from None


def canonical_bytes(manifest: dict) -> bytes:
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def signing_input(manifest: dict) -> bytes:
    return SIGNING_CONTEXT + canonical_bytes(manifest)


def dump_manifest(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CAPABILITY = re.compile(r"[a-z][a-z0-9_]*:[a-z][a-z0-9_]*")  # no wildcards
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_TOP = {"manifest_version", "release", "trust", "packages"}
_RELEASE = {"version", "release_sequence", "timestamp"}
_TRUST = {"algorithm", "key_fingerprint"}
_PACKAGE = {"id", "version", "filename", "sha256", "size", "sequence", "capabilities"}


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and v != ""


def structure_errors(m, require_signature: bool = True) -> list[str]:
    """Every rule in docs/manifest-format.md except the signature itself.
    Unknown fields are errors: a field this verifier does not understand must
    not be able to carry meaning past it."""
    errs: list[str] = []

    def fields(obj, required, optional, where) -> bool:
        if not isinstance(obj, dict):
            errs.append(f"{where}: must be a JSON object")
            return False
        for k in sorted(required - obj.keys()):
            errs.append(f"{where}: missing field '{k}'")
        for k in sorted(obj.keys() - required - optional):
            errs.append(f"{where}: unknown field '{k}'")
        return required <= obj.keys()

    top_required = _TOP | ({"signature"} if require_signature else set())
    top_optional = set() if require_signature else {"signature"}
    if not fields(m, top_required, top_optional, "manifest"):
        return errs

    if not (_is_int(m["manifest_version"]) and m["manifest_version"] == MANIFEST_VERSION):
        errs.append(f"manifest_version: must be {MANIFEST_VERSION}")

    rel = m["release"]
    if fields(rel, _RELEASE, {"description"}, "release"):
        if not _nonempty_str(rel["version"]):
            errs.append("release.version: must be a non-empty string")
        if not (_is_int(rel["release_sequence"]) and rel["release_sequence"] >= 1):
            errs.append("release.release_sequence: must be an integer >= 1")
        if not (isinstance(rel["timestamp"], str) and _TIMESTAMP.fullmatch(rel["timestamp"])):
            errs.append("release.timestamp: must be UTC 'YYYY-MM-DDTHH:MM:SSZ'")
        if "description" in rel and not isinstance(rel["description"], str):
            errs.append("release.description: must be a string")

    tr = m["trust"]
    if fields(tr, _TRUST, set(), "trust"):
        if tr["algorithm"] != "Ed25519":
            errs.append("trust.algorithm: must be 'Ed25519'")
        if not (isinstance(tr["key_fingerprint"], str) and _FINGERPRINT.fullmatch(tr["key_fingerprint"])):
            errs.append("trust.key_fingerprint: must be 'sha256:' + 64 lowercase hex")

    pkgs = m["packages"]
    if not isinstance(pkgs, list) or not pkgs:
        errs.append("packages: must be a non-empty list")
    else:
        ids: set[str] = set()
        names: set[str] = set()
        for i, p in enumerate(pkgs):
            w = f"packages[{i}]"
            if not fields(p, _PACKAGE, set(), w):
                continue
            if not (isinstance(p["id"], str) and _ID.fullmatch(p["id"])):
                errs.append(f"{w}.id: must match [a-z0-9][a-z0-9._-]*")
            elif p["id"] in ids:
                errs.append(f"{w}.id: duplicate id '{p['id']}'")
            else:
                ids.add(p["id"])
            if not _nonempty_str(p["version"]):
                errs.append(f"{w}.version: must be a non-empty string")
            fn = p["filename"]
            # A bare name only: no directory parts, so --files can never be
            # steered outside the directory it was given.
            if not (isinstance(fn, str) and _FILENAME.fullmatch(fn)):
                errs.append(f"{w}.filename: must be a bare file name (no path, no leading dot)")
            elif fn.lower() in names:  # FAT and NTFS are case-insensitive
                errs.append(f"{w}.filename: duplicate file name '{fn}'")
            else:
                names.add(fn.lower())
            if not (isinstance(p["sha256"], str) and _SHA256.fullmatch(p["sha256"])):
                errs.append(f"{w}.sha256: must be 64 lowercase hex characters")
            if not (_is_int(p["size"]) and p["size"] >= 0):
                errs.append(f"{w}.size: must be an integer >= 0")
            if not (_is_int(p["sequence"]) and p["sequence"] == i + 1):
                errs.append(f"{w}.sequence: must be {i + 1} (1, 2, 3... in list order)")
            caps = p["capabilities"]
            if not isinstance(caps, list):
                errs.append(f"{w}.capabilities: must be a list")
            else:
                for c in caps:
                    if not (isinstance(c, str) and _CAPABILITY.fullmatch(c)):
                        errs.append(f"{w}.capabilities: {c!r} is not 'namespace:permission' "
                                    "(lowercase, no wildcards)")
                if len(set(map(str, caps))) != len(caps):
                    errs.append(f"{w}.capabilities: duplicates")

    if require_signature and not isinstance(m.get("signature"), str):
        errs.append("signature: must be a base64 string")
    return errs


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(version: str, release_sequence: int, timestamp: str,
                   packages: list[tuple[str, str, Path, list[str]]],
                   public: bytes, description: str | None = None) -> dict:
    """packages: (id, version, path, capabilities), in boot/install order."""
    release = {"version": version, "release_sequence": release_sequence,
               "timestamp": timestamp}
    if description is not None:
        release["description"] = description
    return {
        "manifest_version": MANIFEST_VERSION,
        "release": release,
        "trust": {"algorithm": "Ed25519", "key_fingerprint": fingerprint(public)},
        "packages": [
            {"id": pid, "version": pver, "filename": path.name,
             "sha256": sha256_file(path), "size": path.stat().st_size,
             "sequence": n, "capabilities": list(caps)}
            for n, (pid, pver, path, caps) in enumerate(packages, start=1)
        ],
    }


def sign_manifest(manifest: dict, secret: bytes) -> dict:
    """Refuses to sign anything malformed, or anything declared for another key."""
    errs = structure_errors(manifest, require_signature=False)
    if errs:
        raise ManifestError("refusing to sign a malformed manifest:\n  " + "\n  ".join(errs))
    public = public_key(secret)
    if manifest["trust"]["key_fingerprint"] != fingerprint(public):
        raise ManifestError("refusing to sign: trust.key_fingerprint names a different "
                            f"key (this key is {fingerprint(public)})")
    signed = {k: v for k, v in manifest.items() if k != "signature"}
    signed["signature"] = base64.b64encode(sign(secret, signing_input(signed))).decode("ascii")
    return signed


def verify_manifest(m, trusted_public: bytes, last_accepted_sequence: int | None = None,
                    files_dir: Path | None = None) -> list[str]:
    """Return every failure; an empty list means the manifest is acceptable.

    The order is fixed: key, then signature, then content. Nothing unsigned is
    interpreted, so a forged manifest yields a signature failure only.
    """
    if not isinstance(m, dict):
        return ["manifest: must be a JSON object"]
    trust = m.get("trust")
    claimed = trust.get("key_fingerprint") if isinstance(trust, dict) else None
    if claimed != fingerprint(trusted_public):
        return [f"trust.key_fingerprint: {claimed!r} is not the trusted key "
                f"{fingerprint(trusted_public)}"]
    sig_text = m.get("signature")
    try:
        sig = base64.b64decode(sig_text, validate=True) if isinstance(sig_text, str) else b""
    except ValueError:
        sig = b""
    if len(sig) != 64 or not verify(trusted_public, signing_input(m), sig):
        return ["signature: not a valid Ed25519 signature by the trusted key over this manifest"]

    errs = structure_errors(m, require_signature=True)
    if errs:
        return errs

    seq = m["release"]["release_sequence"]
    if last_accepted_sequence is not None and seq <= last_accepted_sequence:
        errs.append(f"release.release_sequence: {seq} is not greater than the last accepted "
                    f"{last_accepted_sequence} (replay or rollback)")

    if files_dir is not None:
        for p in m["packages"]:
            path = Path(files_dir) / p["filename"]
            if not path.is_file():
                errs.append(f"{p['id']}: file {p['filename']} not found in {files_dir}")
                continue
            size = path.stat().st_size
            if size != p["size"]:
                errs.append(f"{p['id']}: size {size} != manifest {p['size']}")
            elif sha256_file(path) != p["sha256"]:
                errs.append(f"{p['id']}: sha256 does not match the manifest")
    return errs


# ------------------------------------------------------------ replay state ---

def read_state(path: Path) -> int:
    """The last accepted release_sequence. A missing file is an error, never a
    silent 0: deleting the state must not re-open old releases. A first
    install creates it deliberately (docs/manifest-format.md)."""
    obj = parse_json(Path(path).read_text(encoding="utf-8"))
    if not (isinstance(obj, dict) and obj.keys() == {"release_sequence"}
            and _is_int(obj["release_sequence"]) and obj["release_sequence"] >= 0):
        raise ManifestError(f'{path}: must be exactly {{"release_sequence": <integer >= 0>}}')
    return obj["release_sequence"]


def write_text_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then os.replace(): a crash
    leaves either the old file or the new one, never a torn mix."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_state(path: Path, release_sequence: int) -> None:
    write_text_atomic(path, json.dumps({"release_sequence": release_sequence}) + "\n")
