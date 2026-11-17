"""Unit tests for tools/gen-cap-manifest.py (stdlib unittest only).

Each test writes a small cap.rs into a temp directory and runs the tool
against it with --cap-src and --out, so the repo's kernel/src/cap.rs and
build/ are never touched. Expected values are the vocabulary the test wrote,
and the package hash comes from hashlib over the payload bytes the tool
writes, read back from disk.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "gen-cap-manifest.py"))

_spec = importlib.util.spec_from_file_location("gen_cap_manifest", TOOL)
gcm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gcm)

VOCAB = ["net:send", "compute:verify", "mem:alloc", "fs:read",
         "fs:write", "cam:read", "audio:read"]


def cap_rs(names=VOCAB, reserved_last=True):
    body = "".join(f'    "{n}",\n' for n in names)
    tail = "    RESERVED_NAME,\n" if reserved_last else ""
    return ("pub const CAP_REFS: &[&str] = &[\n" + body + tail + "];\n")


class GenCapManifestTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="gcm-test-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_src(self, text):
        p = self.dir / "cap.rs"
        p.write_text(text, encoding="utf8")
        return p

    def run_tool(self, src_text, out_name="out"):
        src = self.write_src(src_text)
        out = self.dir / out_name
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gcm.main(["--out", str(out), "--cap-src", str(src)])
        return rc, out

    def test_parse_returns_vocabulary_in_order(self):
        names, raw = gcm.parse_cap_refs(cap_rs())
        self.assertEqual(names, VOCAB)
        self.assertTrue(raw.rstrip().endswith("RESERVED_NAME,"))

    def test_parse_without_table_exits(self):
        with self.assertRaises(SystemExit):
            gcm.parse_cap_refs("pub const OTHER: u8 = 1;\n")

    def test_success_writes_package_caps_and_manifest(self):
        rc, out = self.run_tool(cap_rs())
        self.assertEqual(rc, 0)
        self.assertEqual((out / "package.caps").read_text(encoding="utf8"), "fs:read\n")
        self.assertTrue((out / "manifest.json").exists())
        self.assertTrue((out / "root.pub").exists())

    def test_manifest_package_hash_matches_payload_on_disk(self):
        rc, out = self.run_tool(cap_rs())
        self.assertEqual(rc, 0)
        payload = (out / "oracle-package.bin").read_bytes()
        # Pinned contract: the payload text the kernel oracle expects.
        self.assertEqual(payload, b"NOVA-CAP-ORACLE v1: fs:read-only subject\n")
        m = json.loads((out / "manifest.json").read_text(encoding="utf8"))
        pkgs = m["packages"]
        self.assertEqual(len(pkgs), 1)
        self.assertEqual(pkgs[0]["id"], "cap-oracle")
        self.assertEqual(pkgs[0]["capabilities"], ["fs:read"])
        self.assertEqual(pkgs[0]["sha256"], hashlib.sha256(payload).hexdigest())

    def test_two_runs_are_byte_identical(self):
        rc1, out1 = self.run_tool(cap_rs(), "a")
        rc2, out2 = self.run_tool(cap_rs(), "b")
        self.assertEqual((rc1, rc2), (0, 0))
        for name in ("manifest.json", "oracle-package.bin", "root.pub", "root.priv"):
            self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes(), name)

    def test_missing_reserved_arm_is_rejected(self):
        rc, _ = self.run_tool(cap_rs(reserved_last=False))
        self.assertEqual(rc, 1)

    def test_vocabulary_drift_at_subject_index_is_rejected(self):
        drifted = list(VOCAB)
        drifted[3] = "fs:rd"  # index 3 must stay "fs:read"
        rc, _ = self.run_tool(cap_rs(names=drifted))
        self.assertEqual(rc, 1)

    def test_vocabulary_size_is_checked(self):
        rc, _ = self.run_tool(cap_rs(names=VOCAB[:6]))
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
