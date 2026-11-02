#!/usr/bin/env python3
"""Robustness tests for verify-fat32.py and verify-manifest.py.

Contract:
  Derives test cases directly from the byte-level reproductions in docs/logs/d3-findings.log:
  - F1: root directory name corrupted (byte 545762: 0x00 -> 0x7a) -> exits 1 with findings, zero traceback
  - F2: BPB_SecPerClus set to 0 (byte 13: 0x01 -> 0x00) -> exits 1 with findings, zero traceback
  - F3: BPB_RootClus set to 3 (byte 44: 0x02 -> 0x03) -> exits 1 with findings, zero traceback
  - F4: BPB_RootClus high byte set (byte 47: 0x00 -> 0x01) -> exits 1 with findings, zero traceback
  - F5: Short name base byte non-ASCII (byte 544800: 0x53 -> 0xcf) -> exits 1 with findings, zero traceback
  - N1: FAT2 entry of root cluster zeroed (byte 280584: 0x03 -> 0x00) -> exits 1 with FAT1/FAT2 mismatch finding
  - N2: Multi-cluster file chain truncated to EOC (byte 16420: 0a000000 -> ffffff0f) -> exits 1 with extent mismatch finding
  - Manifest deep arrays: 20,000 nested JSON arrays -> exits 1 with clean rejection, zero traceback
  - Manifest deep objects: 5,000 nested JSON objects -> exits 1 with clean rejection, zero traceback
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FAT32_IMAGE = REPO / "kernel" / "src" / "fat32_corpus.img"
VERIFY_FAT32 = REPO / "tools" / "verify-fat32.py"
VERIFY_MANIFEST = REPO / "tools" / "verify-manifest.py"
OPENSSL_FIXTURE = REPO / "tests" / "fixtures" / "ed25519-openssl.json"

sys.path.insert(0, str(REPO / "tools"))
import nova_trust as nt  # noqa: E402


class TestFat32VerifierRobustness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image_bytes = FAT32_IMAGE.read_bytes()

    def run_fat32_verifier(self, mutated_data: bytes) -> tuple[int, str, str]:
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
            tmp_path = Path(f.name)
            tmp_path.write_bytes(mutated_data)
        try:
            proc = subprocess.run(
                [sys.executable, str(VERIFY_FAT32), str(tmp_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return proc.returncode, proc.stdout, proc.stderr
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def test_f1_root_name_corrupted(self):
        """F1: root name corrupted (byte 545762: 0x00 -> 0x7a) must not crash with KeyError."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[545762], 0x00)
        data[545762] = 0x7A
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("KeyError", err)
        self.assertIn("FINDING", out)

    def test_f2_secperclus_zero(self):
        """F2: BPB_SecPerClus = 0 (byte 13: 0x01 -> 0x00) must not crash with ZeroDivisionError."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[13], 0x01)
        data[13] = 0x00
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("ZeroDivisionError", err)
        self.assertIn("FINDING BPB_SecPerClus 0 is not a power of 2", out)

    def test_f3_rootclus_corrupted_lfn(self):
        """F3: BPB_RootClus = 3 (byte 44: 0x02 -> 0x03) must not crash with IndexError."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[44], 0x02)
        data[44] = 0x03
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("IndexError", err)
        self.assertIn("FINDING", out)

    def test_f4_rootclus_high_byte(self):
        """F4: BPB_RootClus high byte set (byte 47: 0x00 -> 0x01) must not crash with struct.error."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[47], 0x00)
        data[47] = 0x01
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("struct.error", err)
        self.assertIn("FINDING", out)

    def test_f5_non_ascii_short_name(self):
        """F5: non-ASCII short name byte (byte 544800: 0x53 -> 0xcf) must not crash with UnicodeDecodeError."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[544800], 0x53)
        data[544800] = 0xCF
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("UnicodeDecodeError", err)
        self.assertIn("FINDING", out)

    def test_n1_fat2_mismatch(self):
        """N1: FAT2 entry of root cluster zeroed (byte 280584: 0x03 -> 0x00) must be detected as FAT1/FAT2 mismatch."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[280584], 0x03)
        data[280584] = 0x00
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertIn("FAT1 and FAT2 mismatch", out)

    def test_n2_truncated_multicluster_chain(self):
        """N2: Multi-cluster file chain truncated to EOC (byte 16420: 0a000000 -> ffffff0f) must be detected."""
        data = bytearray(self.image_bytes)
        self.assertEqual(data[16420:16424], b"\x0a\x00\x00\x00")
        data[16420:16424] = b"\xff\xff\xff\x0f"
        rc, out, err = self.run_fat32_verifier(bytes(data))
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stdout: {out}")
        self.assertNotIn("Traceback", err)
        self.assertIn("requires 2 clusters, chain has 1", out)


class TestManifestVerifierRobustness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fix = json.loads(OPENSSL_FIXTURE.read_text(encoding="ascii"))
        pub_bytes = bytes.fromhex(fix["manifest"]["public"])
        cls.pem_text = nt.public_key_pem(pub_bytes)

    def run_manifest_verifier(self, manifest_bytes: bytes) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory(prefix="test-manifest-") as tmp:
            work = Path(tmp)
            pub_path = work / "key.pub"
            manifest_path = work / "release.manifest"
            pub_path.write_text(self.pem_text, encoding="ascii")
            manifest_path.write_bytes(manifest_bytes)
            proc = subprocess.run(
                [sys.executable, str(VERIFY_MANIFEST), str(manifest_path), "--public-key", str(pub_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return proc.returncode, proc.stdout, proc.stderr

    def test_deep_arrays_rejection(self):
        """Manifest with 20,000 nested JSON arrays must exit 1 without RecursionError traceback."""
        deep_arrays = b"[" * 20000 + b"]" * 20000
        rc, out, err = self.run_manifest_verifier(deep_arrays)
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stderr: {err}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("RecursionError", err)
        self.assertIn("manifest rejected:", err)

    def test_deep_objects_rejection(self):
        """Manifest with 5,000 nested JSON objects must exit 1 without RecursionError traceback."""
        deep_objects = b'{"a":' * 5000 + b"1" + b"}" * 5000
        rc, out, err = self.run_manifest_verifier(deep_objects)
        self.assertEqual(rc, 1, f"Expected rc 1, got {rc}. stderr: {err}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("RecursionError", err)
        self.assertIn("manifest rejected:", err)


if __name__ == "__main__":
    unittest.main()
