"""Tests for tools/pack-shuttle.py using tools/verify-manifest.py as the oracle."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

PACK_SHUTTLE = TOOLS / "pack-shuttle.py"
VERIFY_MANIFEST = TOOLS / "verify-manifest.py"

spec = importlib.util.spec_from_file_location("pack_shuttle", PACK_SHUTTLE)
pack_shuttle = importlib.util.module_from_spec(spec)
sys.modules["pack_shuttle"] = pack_shuttle
spec.loader.exec_module(pack_shuttle)


def run_cmd(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class TestPackShuttle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.src_dir = self.tmp_dir / "src"
        self.src_dir.mkdir()
        self.pkg_file = self.src_dir / "test-module.bin"
        self.pkg_file.write_bytes(b"NOVA test module payload bytes " * 16)

        self.out_dir = self.tmp_dir / "shuttle_pkg"
        self.state_file = self.tmp_dir / "accepted.json"
        self.state_file.write_text('{"release_sequence": 4}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pack_shuttle_accepted_by_verify_manifest(self) -> None:
        """Prove that tools/verify-manifest.py accepts a cleanly packed shuttle package."""
        manifest_path, pub_path = pack_shuttle.pack_shuttle(
            out_dir=self.out_dir,
            packages=[("test-module", "1.0.0", self.pkg_file, ["test:execute"])],
            sequence=5,
            version="1.0.0",
            description="Clean test package",
        )

        self.assertTrue(manifest_path.exists())
        self.assertTrue(pub_path.exists())

        # Oracle check: verify-manifest.py
        proc = run_cmd(
            str(VERIFY_MANIFEST),
            str(manifest_path),
            "--public-key", str(pub_path),
            "--files", str(self.out_dir),
            "--state", str(self.state_file),
            "--accept",
        )
        self.assertEqual(proc.returncode, 0, f"verify-manifest rejected valid package:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("verify-manifest: PASS", proc.stdout)

        # Check that state was updated to sequence 5
        state_data = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state_data.get("release_sequence"), 5)

    def test_tamper_payload_byte_rejected(self) -> None:
        """Tamper 1: Prove that altering a payload byte causes verify-manifest.py to exit 1."""
        manifest_path, pub_path = pack_shuttle.pack_shuttle(
            out_dir=self.out_dir,
            packages=[("test-module", "1.0.0", self.pkg_file, ["test:execute"])],
            sequence=5,
            version="1.0.0",
        )

        # Alter one byte of payload in the output package directory
        target_payload = self.out_dir / "test-module.bin"
        payload_bytes = bytearray(target_payload.read_bytes())
        payload_bytes[0] ^= 0xFF
        target_payload.write_bytes(bytes(payload_bytes))

        # Oracle check
        proc = run_cmd(
            str(VERIFY_MANIFEST),
            str(manifest_path),
            "--public-key", str(pub_path),
            "--files", str(self.out_dir),
            "--state", str(self.state_file),
        )
        self.assertEqual(proc.returncode, 1, f"verify-manifest must reject altered payload:\n{proc.stdout}")
        self.assertIn("REJECTED", proc.stdout)
        self.assertIn("sha256", proc.stdout)

    def test_tamper_version_rollback_rejected(self) -> None:
        """Tamper 2: Prove that version rollback/replay causes verify-manifest.py to exit 1."""
        manifest_path, pub_path = pack_shuttle.pack_shuttle(
            out_dir=self.out_dir,
            packages=[("test-module", "1.0.0", self.pkg_file, ["test:execute"])],
            sequence=5,
            version="1.0.0",
        )

        # Set state to a higher sequence (10) -> manifest sequence 5 is a rollback
        self.state_file.write_text('{"release_sequence": 10}\n', encoding="utf-8")

        # Oracle check
        proc = run_cmd(
            str(VERIFY_MANIFEST),
            str(manifest_path),
            "--public-key", str(pub_path),
            "--files", str(self.out_dir),
            "--state", str(self.state_file),
        )
        self.assertEqual(proc.returncode, 1, f"verify-manifest must reject version rollback:\n{proc.stdout}")
        self.assertIn("REJECTED", proc.stdout)
        self.assertIn("replay", proc.stdout)

    def test_tamper_added_capability_rejected(self) -> None:
        """Tamper 3: Prove that adding an unauthorized capability causes verify-manifest.py to exit 1."""
        manifest_path, pub_path = pack_shuttle.pack_shuttle(
            out_dir=self.out_dir,
            packages=[("test-module", "1.0.0", self.pkg_file, ["test:execute"])],
            sequence=5,
            version="1.0.0",
        )

        # Tamper manifest JSON by adding an extra capability without re-signing
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_data["packages"][0]["capabilities"].append("system:root_admin")
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        # Oracle check
        proc = run_cmd(
            str(VERIFY_MANIFEST),
            str(manifest_path),
            "--public-key", str(pub_path),
            "--files", str(self.out_dir),
            "--state", str(self.state_file),
        )
        self.assertEqual(proc.returncode, 1, f"verify-manifest must reject tampered capability:\n{proc.stdout}")
        self.assertIn("REJECTED", proc.stdout)
        self.assertIn("signature", proc.stdout)

    def test_cli_invocation_and_stub_banner(self) -> None:
        """Verify the CLI tool runs, prints STUB banner, and produces a valid package."""
        proc = run_cmd(
            str(PACK_SHUTTLE),
            "--out-dir", str(self.out_dir),
            "--package", f"test-mod:1.0.0:{self.pkg_file}",
            "--capabilities", "test-mod:test:read,test:write",
            "--sequence", "8",
        )
        self.assertEqual(proc.returncode, 0, f"CLI pack-shuttle failed:\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("pack-shuttle: STUB test root key used", proc.stdout)

        manifest_path = self.out_dir / "release.manifest"
        pub_path = self.out_dir / "test-keys" / "TEST-root.pub"
        self.assertTrue(manifest_path.exists())
        self.assertTrue(pub_path.exists())

        # Verify CLI-created package with oracle
        verify_proc = run_cmd(
            str(VERIFY_MANIFEST),
            str(manifest_path),
            "--public-key", str(pub_path),
            "--files", str(self.out_dir),
            "--state", str(self.state_file),
        )
        self.assertEqual(verify_proc.returncode, 0, verify_proc.stdout)
        self.assertIn("verify-manifest: PASS", verify_proc.stdout)


if __name__ == "__main__":
    unittest.main()
