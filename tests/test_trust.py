#!/usr/bin/env python3
"""Tests for tools/nova_trust.py and the trust CLIs (work orders C1 and C2).

Oracle: tests/fixtures/ed25519-openssl.json, written by OpenSSL through
tests/fixtures/gen_ed25519_openssl.py. Ed25519 is deterministic, so public keys
and signatures must match OpenSSL byte for byte, and every tampered case must
get OpenSSL's verdict. No expected value in this file comes from nova_trust.

The manifest tests are properties, not values:
- every single-field change to a signed manifest is rejected;
- every replay or rollback is refused;
- every altered package file is caught;
- every malformed manifest is refused before it can be signed.

Run: python tests/test_trust.py
"""
import base64
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
import nova_trust as nt  # noqa: E402

FIX = json.loads((ROOT / "tests" / "fixtures" / "ed25519-openssl.json").read_text(encoding="ascii"))
VECTORS = FIX["vectors"]
SEED = bytes.fromhex(VECTORS[0]["seed"])
OTHER_SEED = bytes.fromhex(VECTORS[1]["seed"])


class Ed25519AgainstOpenSSL(unittest.TestCase):
    def test_fixture_is_populated(self):
        self.assertEqual(len(VECTORS), 32)
        self.assertEqual(sum(len(v["negatives"]) for v in VECTORS), 384)

    def test_public_keys_match_openssl(self):
        for v in VECTORS:
            with self.subTest(public=v["public"][:16]):
                self.assertEqual(nt.public_key(bytes.fromhex(v["seed"])).hex(), v["public"])

    def test_signatures_match_openssl_byte_for_byte(self):
        for v in VECTORS:
            with self.subTest(message_bytes=len(v["message"]) // 2):
                sig = nt.sign(bytes.fromhex(v["seed"]), bytes.fromhex(v["message"]))
                self.assertEqual(sig.hex(), v["signature"])

    def test_openssl_signatures_verify(self):
        for v in VECTORS:
            with self.subTest(public=v["public"][:16]):
                self.assertTrue(nt.verify(bytes.fromhex(v["public"]), bytes.fromhex(v["message"]),
                                          bytes.fromhex(v["signature"])))

    def test_tampered_cases_get_the_openssl_verdict(self):
        checked = 0
        for v in VECTORS:
            for case in v["negatives"]:
                with self.subTest(kind=case["kind"]):
                    got = nt.verify(bytes.fromhex(case["public"]), bytes.fromhex(case["message"]),
                                    bytes.fromhex(case["signature"]))
                    self.assertEqual(got, case["openssl_accepts"])
                    checked += 1
        self.assertEqual(checked, 384)

    def test_every_signature_bit_flip_is_rejected(self):
        v = VECTORS[5]
        pub, msg, sig = (bytes.fromhex(v[k]) for k in ("public", "message", "signature"))
        for bit in range(512):
            flipped = bytearray(sig)
            flipped[bit // 8] ^= 1 << (bit % 8)
            with self.subTest(bit=bit):
                self.assertFalse(nt.verify(pub, msg, bytes(flipped)))

    def test_pem_matches_openssl(self):
        for v in VECTORS[:3]:
            seed, pub = bytes.fromhex(v["seed"]), bytes.fromhex(v["public"])
            self.assertEqual(nt.private_key_pem(seed), v["private_pem"])
            self.assertEqual(nt.public_key_pem(pub), v["public_pem"])
            self.assertEqual(nt.parse_private_key_pem(v["private_pem"]), seed)
            self.assertEqual(nt.parse_public_key_pem(v["public_pem"]), pub)
            # git autocrlf must not change which key a .pub file holds
            self.assertEqual(nt.parse_public_key_pem(v["public_pem"].replace("\n", "\r\n")), pub)

    def test_non_ed25519_pem_is_refused(self):
        with self.assertRaises(ValueError):
            nt.parse_public_key_pem(VECTORS[0]["private_pem"])
        with self.assertRaises(ValueError):
            nt.parse_private_key_pem(VECTORS[0]["public_pem"])


class ManifestAgainstOpenSSL(unittest.TestCase):
    def test_signing_input_matches_the_spec(self):
        m = nt.parse_json(FIX["manifest"]["json"])
        self.assertEqual(nt.signing_input(m).hex(), FIX["manifest"]["signing_input"])

    def test_openssl_signed_manifest_verifies(self):
        m = nt.parse_json(FIX["manifest"]["json"])
        self.assertEqual(nt.verify_manifest(m, bytes.fromhex(FIX["manifest"]["public"])), [])

    def test_key_order_in_the_file_does_not_matter(self):
        def reverse_keys(node):
            if isinstance(node, dict):
                return {k: reverse_keys(node[k]) for k in reversed(list(node))}
            if isinstance(node, list):
                return [reverse_keys(x) for x in node]
            return node
        m = reverse_keys(nt.parse_json(FIX["manifest"]["json"]))
        self.assertEqual(nt.signing_input(m).hex(), FIX["manifest"]["signing_input"])
        self.assertEqual(nt.verify_manifest(m, bytes.fromhex(FIX["manifest"]["public"])), [])


def _changed(value):
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        return value + "x"
    raise TypeError(type(value))


def single_change_mutants(signed: dict):
    """(description, manifest) pairs, each differing from `signed` in one place:
    every leaf changed, every field and list item removed, an unknown field
    added to every object, and an item added to every list."""
    sites = []

    def visit(node, path):
        if isinstance(node, dict):
            sites.append((path, "add-field"))
            for k in node:
                if path == () and k == "signature":
                    continue
                sites.append((path + (k,), "remove"))
                visit(node[k], path + (k,))
        elif isinstance(node, list):
            sites.append((path, "append"))
            for i, item in enumerate(node):
                sites.append((path + (i,), "remove"))
                visit(item, path + (i,))
        else:
            sites.append((path, "change"))

    visit(signed, ())
    for path, op in sites:
        m = copy.deepcopy(signed)
        target = m
        for step in (path if op in ("add-field", "append") else path[:-1]):
            target = target[step]
        if op == "add-field":
            target["unknown_field"] = 1
        elif op == "append":
            target.append(copy.deepcopy(target[0]) if target and isinstance(target[0], dict) else "x:y")
        elif op == "remove":
            del target[path[-1]]
        else:
            target[path[-1]] = _changed(target[path[-1]])
        yield f"{op} {'/'.join(map(str, path)) or '<root>'}", m


class ManifestRules(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.files = Path(self._tmp.name)
        (self.files / "mathd.bin").write_bytes(b"mathd payload bytes")
        (self.files / "novacore.tar").write_bytes(b"n" * 1000)
        self.pub = nt.public_key(SEED)
        self.unsigned = nt.build_manifest(
            "0.2.0", 7, "2026-09-25T10:00:00Z",
            [("mathd", "0.2.0", self.files / "mathd.bin", ["compute:expression", "compute:verify"]),
             ("novacore", "0.1.0", self.files / "novacore.tar", [])],
            self.pub, "test release")
        self.signed = nt.sign_manifest(self.unsigned, SEED)

    def tearDown(self):
        self._tmp.cleanup()

    def check(self, m, last=None, files=True):
        return nt.verify_manifest(m, self.pub, last, self.files if files else None)

    def test_valid_manifest_passes(self):
        self.assertEqual(self.check(self.signed, last=6), [])

    def test_text_round_trip_passes(self):
        self.assertEqual(self.check(nt.parse_json(nt.dump_manifest(self.signed))), [])

    def test_every_single_field_change_is_rejected(self):
        mutants = list(single_change_mutants(self.signed))
        self.assertGreaterEqual(len(mutants), 50, "mutant enumeration is too small to mean anything")
        for desc, m in mutants:
            with self.subTest(mutation=desc):
                self.assertNotEqual(self.check(m), [], f"accepted after: {desc}")

    def test_every_signature_bit_flip_is_rejected(self):
        sig = base64.b64decode(self.signed["signature"])
        for bit in range(512):
            flipped = bytearray(sig)
            flipped[bit // 8] ^= 1 << (bit % 8)
            m = dict(self.signed, signature=base64.b64encode(bytes(flipped)).decode())
            with self.subTest(bit=bit):
                self.assertNotEqual(self.check(m), [])

    def test_unsigned_or_garbled_signature_is_rejected(self):
        self.assertNotEqual(self.check(self.unsigned), [])
        for bad in ("", "not base64!", base64.b64encode(b"short").decode()):
            with self.subTest(signature=bad):
                self.assertNotEqual(self.check(dict(self.signed, signature=bad)), [])

    def test_other_key_is_rejected_both_ways(self):
        other_pub = nt.public_key(OTHER_SEED)
        self.assertNotEqual(nt.verify_manifest(self.signed, other_pub), [])
        forged = nt.build_manifest("0.2.0", 7, "2026-09-25T10:00:00Z",
                                   [("mathd", "0.2.0", self.files / "mathd.bin", [])], other_pub)
        forged = nt.sign_manifest(forged, OTHER_SEED)
        failures = self.check(forged)
        self.assertEqual(len(failures), 1)
        self.assertIn("not the trusted key", failures[0])

    def test_replay_and_rollback_are_refused(self):
        for last, ok in ((0, True), (6, True), (7, False), (8, False), (1000, False)):
            with self.subTest(last_accepted=last):
                self.assertEqual(self.check(self.signed, last=last) == [], ok)

    def test_altered_package_files_are_caught(self):
        path = self.files / "mathd.bin"
        original = path.read_bytes()
        for desc, data in (("one byte changed", bytes([original[0] ^ 1]) + original[1:]),
                           ("one byte appended", original + b"\0"),
                           ("truncated", original[:-1])):
            path.write_bytes(data)
            with self.subTest(desc):
                self.assertNotEqual(self.check(self.signed), [])
        path.unlink()
        self.assertTrue(any("not found" in f for f in self.check(self.signed)))

    def test_signer_refuses_malformed_manifests(self):
        def variant(edit):
            m = copy.deepcopy(self.unsigned)
            edit(m)
            return m
        pk = lambda i: (lambda m: m["packages"][i])  # noqa: E731
        cases = {
            "path traversal in filename": lambda m: pk(0)(m).update(filename="../evil.bin"),
            "directory in filename": lambda m: pk(0)(m).update(filename="sub/mathd.bin"),
            "hidden filename": lambda m: pk(0)(m).update(filename=".mathd.bin"),
            "wildcard capability": lambda m: pk(0)(m).update(capabilities=["compute:*"]),
            "capability without namespace": lambda m: pk(0)(m).update(capabilities=["verify"]),
            "uppercase capability": lambda m: pk(0)(m).update(capabilities=["Compute:verify"]),
            "duplicate capability": lambda m: pk(0)(m).update(capabilities=["a:b", "a:b"]),
            "duplicate id": lambda m: pk(1)(m).update(id="mathd"),
            "filenames differing only in case": lambda m: pk(1)(m).update(filename="MATHD.BIN"),
            "sequence gap": lambda m: pk(1)(m).update(sequence=3),
            "negative size": lambda m: pk(0)(m).update(size=-1),
            "sha256 uppercase": lambda m: pk(0)(m).update(sha256=pk(0)(m)["sha256"].upper()),
            "release_sequence 0": lambda m: m["release"].update(release_sequence=0),
            "release_sequence as bool": lambda m: m["release"].update(release_sequence=True),
            "timestamp not UTC": lambda m: m["release"].update(timestamp="2026-09-25 10:00"),
            "unknown package field": lambda m: pk(0)(m).update(entrypoint="/bin/sh"),
            "unknown top-level field": lambda m: m.update(extra=1),
            "manifest_version 2": lambda m: m.update(manifest_version=2),
            "no packages": lambda m: m.update(packages=[]),
            "algorithm not Ed25519": lambda m: m["trust"].update(algorithm="RSA"),
        }
        for desc, edit in cases.items():
            with self.subTest(desc):
                with self.assertRaises(nt.ManifestError):
                    nt.sign_manifest(variant(edit), SEED)

    def test_signer_refuses_a_manifest_declared_for_another_key(self):
        with self.assertRaises(nt.ManifestError):
            nt.sign_manifest(self.unsigned, OTHER_SEED)

    def test_strict_json(self):
        for text in ('{"a": 1, "a": 2}', '{"a": 1.5}', '{"a": NaN}', '{"a": Infinity}', "{"):
            with self.subTest(text=text):
                with self.assertRaises(nt.ManifestError):
                    nt.parse_json(text)

    def test_state_file(self):
        state = self.files / "state.json"
        with self.assertRaises(FileNotFoundError):
            nt.read_state(state)
        for bad in ('{"release_sequence": -1}', '{"release_sequence": "7"}', "{}",
                    '{"release_sequence": 7, "x": 1}'):
            state.write_text(bad)
            with self.subTest(content=bad):
                with self.assertRaises(nt.ManifestError):
                    nt.read_state(state)
        nt.write_state(state, 42)
        self.assertEqual(nt.read_state(state), 42)


def run_tool(tool: str, *args, cwd: Path):
    r = subprocess.run([sys.executable, str(TOOLS / tool), *map(str, args)],
                       cwd=cwd, capture_output=True, timeout=120)
    # ASCII only: a Windows console on cp1252 crashes on arrows and check marks.
    return r.returncode, (r.stdout + r.stderr).decode("ascii")


class CommandLine(unittest.TestCase):
    def test_full_release_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            key, other = t / "key", t / "other-key"
            self.assertEqual(run_tool("keygen.py", "--out-dir", key, cwd=t)[0], 0)
            self.assertEqual(run_tool("keygen.py", "--out-dir", key, cwd=t)[0], 1,
                             "keygen must refuse to overwrite a root key")
            self.assertEqual(run_tool("keygen.py", "--out-dir", other, cwd=t)[0], 0)

            rel = t / "release"
            rel.mkdir()
            (rel / "mathd.bin").write_bytes(b"mathd release bytes")
            manifest = t / "r.manifest"
            rc, out = run_tool("create-manifest.py", "--version", "0.2.0", "--sequence", "5",
                               "--package", f"mathd:0.2.0:{rel / 'mathd.bin'}",
                               "--capabilities", "mathd:compute:verify",
                               "--public-key", key / "root.pub", "--output", manifest, cwd=t)
            self.assertEqual(rc, 0, out)
            state = t / "accepted.json"
            state.write_text('{"release_sequence": 4}\n')
            verify = ("verify-manifest.py", manifest, "--public-key", key / "root.pub",
                      "--files", rel, "--state", state)

            rc, out = run_tool(*verify, cwd=t)
            self.assertEqual(rc, 1, "an unsigned manifest must be rejected:\n" + out)
            rc, out = run_tool("sign-manifest.py", manifest, other / "root.priv", cwd=t)
            self.assertEqual(rc, 1, "signing with a key the manifest does not name must fail:\n" + out)
            rc, out = run_tool("sign-manifest.py", manifest, key / "root.priv", cwd=t)
            self.assertEqual(rc, 0, out)

            rc, out = run_tool(*verify, "--accept", cwd=t)
            self.assertEqual(rc, 0, out)
            self.assertEqual(json.loads(state.read_text()), {"release_sequence": 5})
            rc, out = run_tool(*verify, "--accept", cwd=t)
            self.assertEqual(rc, 1, "the same release twice is a replay:\n" + out)
            self.assertIn("replay", out)
            self.assertEqual(json.loads(state.read_text()), {"release_sequence": 5},
                             "a rejected manifest must not move the state")

            state.write_text('{"release_sequence": 4}\n')
            (rel / "mathd.bin").write_bytes(b"mathd release bytez")
            self.assertEqual(run_tool(*verify, cwd=t)[0], 1, "an altered package must be rejected")
            (rel / "mathd.bin").write_bytes(b"mathd release bytes")
            self.assertEqual(run_tool(*verify, cwd=t)[0], 0)

            text = manifest.read_text()
            manifest.write_text(text.replace('"0.2.0"', '"0.2.1"', 1))
            self.assertEqual(run_tool(*verify, cwd=t)[0], 1, "an edited manifest must be rejected")
            manifest.write_text(text)

            state.unlink()
            self.assertEqual(run_tool(*verify, cwd=t)[0], 2,
                             "a missing state file is an error, never an implicit 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
