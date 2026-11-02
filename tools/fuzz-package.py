#!/usr/bin/env python3
"""Fuzz package parsing: tools/verify-manifest.py, on top of tools/nova_trust.py
(work order D3: "FAT, GPT and package parsing. Crashes and false passes
reported separately.").

Two targets, two oracles
------------------------
1. tools/verify-manifest.py is driven through its own main() with mutated
   manifests and mutated package files. Its contract is
   "0 = acceptable, 1 = rejected, 2 = usage or I/O error"; an exception is
   neither, so it is counted as a CRASH.

   The oracle for "this mutation must be rejected" never comes from the
   verifier:

     - a changed package file: hashlib.sha256 of the mutated bytes is not the
       sha256 the manifest (already fixed and signed) declares;
     - a changed manifest: the text no longer parses to the same JSON object
       as the baseline (stdlib json, per docs/manifest-format.md "Canonical
       form"). A pure whitespace change parses equal and is allowed to pass;
       anything else must be rejected, because the signature covers every
       field except `signature` itself and Ed25519 is deterministic;
     - a replay/rollback: release_sequence compared with the --state value
       that the test writes, an integer comparison the verifier cannot fake.

2. tools/nova_trust.py is also fuzzed directly, so a bug that verify-manifest
   never reaches still shows up: bit flips in the committed OpenSSL fixtures
   (tests/fixtures/ed25519-openssl.json) must make verify() return False, and
   malformed JSON/key text must raise ValueError or return a value - never
   anything else.

Baselines are checked first, because a target that rejects everything would
make every mutation look "detected" (the failure mode AGENTS.md rule 1 names).

Phase B needs a signed manifest whose package files exist, and no committed
fixture hashes the files it ships. It is therefore built and signed in-process
with nova_trust itself, using a seed from the committed OpenSSL fixture
(tests/fixtures/ed25519-openssl.json) and package files this script writes.
That baseline is self-signed, and is labelled as such: the mutation oracles
above (hashlib, object equality, integer comparison) do not depend on the
signer being correct. Phase A uses the OpenSSL-signed fixture manifest
verbatim, so signature handling itself is anchored outside the code under test.

Seeded (default 0x4E4F5641 = "NOVA"), seed printed, exit 0 only when
crashes == 0 and false passes == 0.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import random
import sys
import tempfile
from pathlib import Path

DEFAULT_SEED = 0x4E4F5641  # "NOVA"
ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
FIXTURE = ROOT / "tests" / "fixtures" / "ed25519-openssl.json"


def load_modules():
    """Load nova_trust and verify-manifest by path, sharing one module object.

    tools/verify-manifest.py does `import nova_trust` itself; registering ours
    under that name first guarantees both sides use the same code.
    """
    spec = importlib.util.spec_from_file_location("nova_trust", TOOLS / "nova_trust.py")
    nt = importlib.util.module_from_spec(spec)
    sys.modules["nova_trust"] = nt
    spec.loader.exec_module(nt)

    spec = importlib.util.spec_from_file_location("verify_manifest", TOOLS / "verify-manifest.py")
    vm = importlib.util.module_from_spec(spec)
    sys.modules["verify_manifest"] = vm
    spec.loader.exec_module(vm)
    return nt, vm


class Tally:
    """Outcome counters for one phase, kept separate as D3 requires."""

    def __init__(self, name: str):
        self.name = name
        self.detected = 0
        self.false_pass = 0
        self.crash = 0
        self.crash_kinds: dict[str, int] = {}
        self.fp_examples: list[str] = []
        self.crash_examples: list[str] = []

    def detect(self) -> None:
        self.detected += 1

    def pass_(self, detail: str) -> None:
        self.false_pass += 1
        if len(self.fp_examples) < 5:
            self.fp_examples.append(detail)

    def crash_(self, detail: str, exc: BaseException | None = None) -> None:
        self.crash += 1
        kind = type(exc).__name__ if exc is not None else detail.split(":")[0]
        self.crash_kinds[kind] = self.crash_kinds.get(kind, 0) + 1
        if len(self.crash_examples) < 5:
            self.crash_examples.append(detail)

    def report(self) -> None:
        total = self.detected + self.false_pass + self.crash
        _TOTALS["crash"] += self.crash
        _TOTALS["false_pass"] += self.false_pass
        print(f"fuzz-package: {self.name}: {self.detected}/{total} mutations rejected, "
              f"{self.false_pass} false passes, {self.crash} crashes")
        for e in self.fp_examples:
            print(f"fuzz-package:   FALSE PASS {e}")
        for e in self.crash_examples:
            print(f"fuzz-package:   CRASH {e}")
        for kind, count in sorted(self.crash_kinds.items(), key=lambda kv: -kv[1]):
            print(f"fuzz-package:   crash kind: {kind}: {count}")


class Harness:
    def __init__(self, nt, vm, work: Path):
        self.nt = nt
        self.vm = vm
        self.work = work
        self.manifest = work / "release.manifest"
        self.pub = work / "root-key.pub"
        self.files = work / "files"
        self.files.mkdir()
        self.state = work / "state.json"

    # -- calling the target ------------------------------------------------

    def call(self, *, files: bool = False, state: Path | None = None,
             accept: bool = False) -> tuple[int | None, BaseException | None, str]:
        """Run verify-manifest's main(argv) in-process. None = crash."""
        argv = [str(self.manifest), "--public-key", str(self.pub)]
        if files:
            argv += ["--files", str(self.files)]
        if state is not None:
            argv += ["--state", str(state)]
        if accept:
            argv += ["--accept"]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = self.vm.main(argv)
            return rc, None, out.getvalue()
        except BaseException as e:  # noqa: BLE001 - a target crash IS the datum
            return None, e, out.getvalue()

    def write_manifest(self, data: bytes) -> None:
        self.manifest.write_bytes(data)

    def write_state(self, seq: int, name: str = "state.json") -> Path:
        p = self.work / name
        p.write_text(json.dumps({"release_sequence": seq}) + "\n", encoding="ascii")
        return p

    # -- fixtures ----------------------------------------------------------

    def write_pem(self, public: bytes) -> None:
        self.pub.write_text(self.nt.public_key_pem(public), encoding="ascii")

    def build_self_signed(self, seed: bytes, seq: int) -> bytes:
        """Package files plus a manifest signed by a fixture seed (phase B)."""
        payloads = {
            "mathd.bin": bytes(range(64)),
            "novacore.tar": b"novacore fixture payload\n",
        }
        for name, blob in payloads.items():
            (self.files / name).write_bytes(blob)
        public = self.nt.public_key(seed)
        manifest = self.nt.build_manifest(
            "0.1.0", seq, "2026-11-02T00:00:00Z",
            [("mathd", "0.1.0", self.files / "mathd.bin", ["compute:verify"]),
             ("novacore", "0.1.0", self.files / "novacore.tar", [])],
            public, description="fuzz-package baseline (self-signed)",
        )
        signed = self.nt.sign_manifest(manifest, seed)
        self.write_pem(public)
        return self.nt.dump_manifest(signed).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_object(text: bytes):
    """Parse with the stdlib, per docs/manifest-format.md canonical form.

    Returns the object, or None when the bytes are not valid JSON. Used only
    to decide what the mutation did, never to decide whether a signature is
    valid.
    """
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - undecodable/unparsable is an answer
        return None


def effective_object(text: bytes):
    """The parsed manifest with `signature` replaced by its decoded bytes.

    docs/manifest-format.md defines signature as base64 of the 64-byte Ed25519
    signature, so a different base64 spelling of the SAME bytes carries the
    same meaning (Python's b64decode ignores the padding bits in the final
    character). Treating that as "the object changed" would report a target
    defect that does not exist.
    """
    obj = canonical_object(text)
    if not isinstance(obj, dict):
        return obj
    sig = obj.get("signature")
    if isinstance(sig, str):
        try:
            obj["signature"] = base64.b64decode(sig, validate=True)
        except Exception:  # noqa: BLE001
            obj["signature"] = ("undecodable", sig)
    return obj


# Deterministic adversarial documents. Each must be answered with exit 1 or 2,
# never a traceback. These are hand-written inputs, not sourced data.
ADVERSARIAL = [
    ("empty file", b""),
    ("top-level list", b"[]"),
    ("top-level string", b'"x"'),
    ("top-level number", b"1"),
    ("null", b"null"),
    ("deep arrays", b"[" * 20000 + b"]" * 20000),
    ("deep objects", b'{"a":' * 5000 + b"1" + b"}" * 5000),
    ("huge integer", b'{"release_sequence": ' + b"9" * 6000 + b"}"),
    ("invalid UTF-8", b'{"release": "\xff\xfe"}'),
    ("NUL byte", b'{"a": "\x00"}'),
    ("UTF-8 BOM", b"\xef\xbb\xbf" + b'{"a": 1}'),
    ("NaN", b'{"a": NaN}'),
    ("Infinity", b'{"a": Infinity}'),
    ("duplicate keys", b'{"a": 1, "a": 2}'),
    ("trailing garbage", b'{"a": 1} x'),
    ("unterminated string", b'{"a": "x}'),
    ("lone surrogate escape", b'{"a": "\\ud800"}'),
]


def random_bit_flip(rng: random.Random, data: bytes) -> tuple[int, int, bytes]:
    """(byte index, bit, mutated bytes) for one guaranteed-different bit."""
    off = rng.randrange(len(data))
    bit = rng.randrange(8)
    out = bytearray(data)
    out[off] ^= 1 << bit
    return off, bit, bytes(out)


def phase_signature_and_replay(h: Harness, nt, fix: dict, manifest_bytes: bytes) -> None:
    """Targeted probes for the checks a random byte flip cannot isolate."""
    t = Tally("probes (signature, replay, files)")
    baseline = h.call(files=True)
    if baseline[0] != 0 or baseline[1] is not None:
        t.crash_(f"BASELINE rejects the valid self-signed manifest (exit {baseline[0]})",
                 baseline[1])
        t.report()
        return
    # Replay and rollback: same manifest, different recorded last release.
    for label, state_seq, want in (
        ("replay: state equals release_sequence", 3, "reject"),
        ("rollback: state above release_sequence", 4, "reject"),
        ("newer: state below release_sequence", 2, "accept"),
    ):
        sp = h.write_state(state_seq, f"state-{state_seq}.json")
        rc, exc, out = h.call(files=True, state=sp)
        if exc is not None:
            t.crash_(f"{label}: {exc!r}", exc)
        elif want == "reject" and rc != 0:
            t.detect()
        elif want == "accept" and rc == 0:
            t.detect()
        else:
            t.pass_(f"{label}: exit {rc}, wanted {want} ({out.strip().splitlines()[-1:]})")

    # A different key, and a truncated signature, must both be refused.
    other = bytes.fromhex(fix["vectors"][1]["public"])
    saved = h.pub.read_bytes()
    h.write_pem(other)
    rc, exc, _ = h.call(files=True)
    if exc is not None:
        t.crash_("wrong public key: crashed", exc)
    elif rc != 0:
        t.detect()
    else:
        t.pass_("wrong public key: accepted")
    h.pub.write_bytes(saved)

    obj = json.loads(manifest_bytes)
    truncated = dict(obj)
    truncated["signature"] = obj["signature"][:8]
    h.write_manifest(json.dumps(truncated).encode())
    rc, exc, _ = h.call(files=True)
    if exc is not None:
        t.crash_("truncated signature: crashed", exc)
    elif rc != 0:
        t.detect()
    else:
        t.pass_("truncated signature: accepted")
    t.report()


def phase_package_files(h: Harness, manifest_bytes: bytes, iterations: int,
                        rng: random.Random) -> None:
    """A changed package byte must be refused; hashlib is the oracle."""
    t = Tally("package files")
    declared = {p["filename"]: p["sha256"] for p in json.loads(manifest_bytes)["packages"]}
    h.write_manifest(manifest_bytes)
    for _ in range(iterations):
        name = rng.choice(sorted(declared))
        path = h.files / name
        original = path.read_bytes()
        off, bit, mutated = random_bit_flip(rng, original)
        assert sha256(mutated) != declared[name]  # the independent oracle
        path.write_bytes(mutated)
        rc, exc, _ = h.call(files=True)
        path.write_bytes(original)
        if exc is not None:
            t.crash_(f"{name} byte {off} bit {bit}: {exc!r}", exc)
        elif rc == 0:
            t.pass_(f"{name} byte {off} bit {bit}: accepted with sha256 "
                    f"{sha256(mutated)[:16]}... but manifest says {declared[name][:16]}...")
        else:
            t.detect()
    t.report()


def phase_manifest_text(h: Harness, manifest_bytes: bytes, iterations: int,
                        rng: random.Random) -> None:
    """A changed manifest must be refused unless it parses to the same object."""
    t = Tally("manifest text")
    baseline_obj = effective_object(manifest_bytes)
    if baseline_obj is None:
        t.crash_("BASELINE manifest text does not parse")
        t.report()
        return
    allowed = 0
    for _ in range(iterations):
        off, bit, mutated = random_bit_flip(rng, manifest_bytes)
        expected_ok = effective_object(mutated) == baseline_obj
        h.write_manifest(mutated)
        rc, exc, _ = h.call(files=True)
        if exc is not None:
            t.crash_(f"byte {off} bit {bit}: {exc!r}", exc)
        elif rc == 0 and expected_ok:
            allowed += 1  # formatting-equivalent: passing is correct
        elif rc == 0:
            t.pass_(f"byte {off} bit {bit}: accepted, but the JSON object changed")
        elif expected_ok:
            t.pass_(f"byte {off} bit {bit}: rejected, but it is formatting-equivalent")
            # A rejection here is conservative, not a security defect; report
            # it as a false pass so the count is honest rather than hidden.
        else:
            t.detect()
    h.write_manifest(manifest_bytes)
    if allowed:
        print(f"fuzz-package: {allowed}/{iterations} mutations were formatting-only or "
              "re-encoded the same signature bytes, and were correctly accepted")
    t.report()


def phase_adversarial(h: Harness, iterations_each: int = 1) -> None:
    """Adversarial documents must yield 1 or 2, never a traceback."""
    t = Tally("adversarial documents")
    for label, data in ADVERSARIAL:
        for _ in range(iterations_each):
            h.write_manifest(data)
            rc, exc, _ = h.call(files=True)
            if exc is not None:
                t.crash_(f"{label}: {exc!r}", exc)
            elif rc in (1, 2):
                t.detect()
            else:
                t.pass_(f"{label}: exit {rc}, wanted 1 (rejected) or 2 (I/O error)")
    h.write_manifest(b"")
    t.report()


def phase_nova_trust(nt, fix: dict, iterations: int, rng: random.Random) -> None:
    """Target nova_trust directly: verify() must return False, never raise."""
    t = Tally("nova_trust.verify")
    vectors = fix["vectors"]

    # Every bit of one committed OpenSSL signature must be rejected.
    v = vectors[5]
    pub = bytes.fromhex(v["public"])
    msg = bytes.fromhex(v["message"])
    sig = bytes.fromhex(v["signature"])
    if not nt.verify(pub, msg, sig):
        t.crash_("BASELINE: the committed OpenSSL signature does not verify")
        t.report()
        return
    t.detect()
    for bit in range(512):
        flipped = bytearray(sig)
        flipped[bit // 8] ^= 1 << (bit % 8)
        try:
            got = nt.verify(pub, msg, bytes(flipped))
        except BaseException as e:  # noqa: BLE001
            t.crash_(f"signature bit {bit}: {e!r}", e)
        else:
            if got:
                t.pass_(f"signature bit {bit}: ACCEPTED a corrupted signature")
            else:
                t.detect()

    # Every bit of a committed public key, and random message bits.
    for _ in range(iterations):
        which = rng.choice(("public", "message"))
        src = pub if which == "public" else msg
        off = rng.randrange(len(src))
        bit = rng.randrange(8)
        mutated = bytearray(src)
        mutated[off] ^= 1 << bit
        args = (bytes(mutated), msg, sig) if which == "public" else (pub, bytes(mutated), sig)
        try:
            got = nt.verify(*args)
        except BaseException as e:  # noqa: BLE001
            t.crash_(f"{which} byte {off} bit {bit}: {e!r}", e)
        else:
            if got:
                t.pass_(f"{which} byte {off} bit {bit}: ACCEPTED")
            else:
                t.detect()
    t.report()


def phase_key_text(nt, fix: dict, rng: random.Random, iterations: int) -> None:
    """parse_public_key_pem: ValueError or a 32-byte key, never anything else."""
    t = Tally("nova_trust.parse_public_key_pem")
    pem = fix["vectors"][0]["public_pem"].encode()
    for _ in range(iterations):
        off, bit, mutated = random_bit_flip(rng, pem)
        text = mutated.decode("ascii", "replace")
        try:
            key = nt.parse_public_key_pem(text)
        except ValueError:
            t.detect()
        except BaseException as e:  # noqa: BLE001
            t.crash_(f"byte {off} bit {bit}: {e!r}", e)
        else:
            if isinstance(key, bytes) and len(key) == 32:
                t.detect()
            else:
                t.pass_(f"byte {off} bit {bit}: returned {key!r}")
    t.report()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("iterations", nargs="?", type=int, default=150,
                    help="mutations per random phase (default 150)")
    ap.add_argument("--seed", type=lambda s: int(s, 0), default=DEFAULT_SEED)
    args = ap.parse_args()

    nt, vm = load_modules()
    fix = json.loads(FIXTURE.read_text(encoding="ascii"))

    seed_bytes = bytes.fromhex(fix["vectors"][0]["seed"])
    print(f"fuzz-package: seed {args.seed:#x}; OpenSSL fixture {FIXTURE.relative_to(ROOT)} "
          f"({len(fix['vectors'])} vectors)")

    with tempfile.TemporaryDirectory(prefix="d3-package-") as tmp:
        work = Path(tmp)
        h = Harness(nt, vm, work)

        # ---- phase A: the committed OpenSSL manifest, no package files ----
        text_a = fix["manifest"]["json"].encode()
        h.write_pem(bytes.fromhex(fix["manifest"]["public"]))
        h.write_manifest(text_a)
        rc, exc, out = h.call()
        if exc is not None or rc != 0:
            print(f"fuzz-package: BASELINE A FAIL: exit={rc} exc={exc!r}")
            for line in out.splitlines()[-5:]:
                print(f"fuzz-package:   {line}")
            return 1
        print("fuzz-package: baseline A: committed OpenSSL-signed manifest accepted "
              "(exit 0, no --files)")

        # ---- phase B: a self-signed manifest whose files exist ------------
        text_b = h.build_self_signed(seed_bytes, seq=3)
        h.write_manifest(text_b)
        rc, exc, _ = h.call(files=True)
        if exc is not None or rc != 0:
            print(f"fuzz-package: BASELINE B FAIL: exit={rc} exc={exc!r}")
            return 1
        print("fuzz-package: baseline B: self-signed manifest with real package files "
              "accepted (exit 0)")

        rng = random.Random(args.seed)
        phase_signature_and_replay(h, nt, fix, text_b)
        phase_package_files(h, text_b, args.iterations, rng)
        phase_manifest_text(h, text_b, args.iterations, rng)
        phase_adversarial(h)
        phase_nova_trust(nt, fix, args.iterations, rng)
        phase_key_text(nt, fix, rng, args.iterations)

    totals = _TOTALS
    print(f"fuzz-package: crashes: {totals['crash']}")
    print(f"fuzz-package: false passes: {totals['false_pass']}")
    ok = totals["crash"] == 0 and totals["false_pass"] == 0
    print(f"fuzz-package: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


_TOTALS = {"crash": 0, "false_pass": 0}


if __name__ == "__main__":
    sys.exit(main())
