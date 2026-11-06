"""Tests for novacore.shuttle (E2 shuttle package importer).

Verifies fail-closed behavior: an invalid or tampered package raises an error
and leaves the proposal store byte-unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
_tools = Path(__file__).resolve().parent.parent.parent / "tools"
sys.path.insert(0, str(_tools))

import importlib.util
spec = importlib.util.spec_from_file_location("pack_shuttle", _tools / "pack-shuttle.py")
pack_shuttle = importlib.util.module_from_spec(spec)
sys.modules["pack_shuttle"] = pack_shuttle
spec.loader.exec_module(pack_shuttle)

from novacore.shuttle import ShuttleImporter, ShuttleVerificationError
from novacore.updates import UpdateProposalStore


def dir_snapshot(directory: Path) -> dict[str, bytes]:
    """Capture a byte-level snapshot of all files in directory."""
    if not directory.exists():
        return {}
    snapshot = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(directory))
            snapshot[rel] = path.read_bytes()
    return snapshot


class TestShuttleImporter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

        # Source payload for test packages
        self.src_dir = self.tmp_path / "src"
        self.src_dir.mkdir()
        self.payload = self.src_dir / "mathd.bin"
        self.payload.write_bytes(b"NOVA mathd binary payload " * 32)

        # Keys
        self.key_dir = self.tmp_path / "keys"
        self.priv_path, self.pub_path = pack_shuttle.ensure_test_key(self.key_dir)

        # Proposal store
        self.proposals_dir = self.tmp_path / "proposals"
        self.proposal_store = UpdateProposalStore(self.proposals_dir)

        # State file
        self.state_file = self.tmp_path / "accepted_state.json"
        self.state_file.write_text('{"release_sequence": 2}\n', encoding="utf-8")

        self.importer = ShuttleImporter(
            proposal_store=self.proposal_store,
            trusted_public_key=self.pub_path,
            state_path=self.state_file,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_package(self, seq: int = 3, version: str = "0.2.0") -> Path:
        pkg_dir = self.tmp_path / f"pkg_seq_{seq}"
        pack_shuttle.pack_shuttle(
            out_dir=pkg_dir,
            packages=[("mathd", version, self.payload, ["compute:verify"])],
            sequence=seq,
            version=version,
            description="Test release package",
            key_dir=self.key_dir,
        )
        return pkg_dir

    def test_clean_package_import_creates_proposal_with_applied_false(self) -> None:
        pkg_dir = self._make_package(seq=3, version="0.2.0")
        proposal_path = self.importer.import_package(pkg_dir)

        self.assertTrue(proposal_path.exists())
        self.assertTrue(proposal_path.is_file())

        proposal_data = json.loads(proposal_path.read_text(encoding="utf-8"))
        self.assertEqual(proposal_data["status"], "proposed_unverified")
        self.assertFalse(proposal_data["applied"])
        self.assertTrue(proposal_data["requires_owner_confirmation"])
        self.assertTrue(proposal_data["requires_signed_package"])
        self.assertIn("0.2.0", proposal_data["title"])
        self.assertIn("mathd", proposal_data["summary"])

    def test_store_byte_unchanged_on_missing_manifest(self) -> None:
        empty_dir = self.tmp_path / "empty_pkg"
        empty_dir.mkdir()

        before = dir_snapshot(self.proposals_dir)
        with self.assertRaises(ShuttleVerificationError):
            self.importer.import_package(empty_dir)
        after = dir_snapshot(self.proposals_dir)

        self.assertEqual(before, after)
        self.assertFalse(self.proposals_dir.exists())

    def test_store_byte_unchanged_on_payload_tamper(self) -> None:
        # First import a valid proposal so store has existing content
        pkg1 = self._make_package(seq=3, version="0.2.0")
        self.importer.import_package(pkg1)
        self.assertTrue(self.proposals_dir.exists())

        # Now create second package and tamper its payload
        pkg2 = self._make_package(seq=4, version="0.3.0")
        target_payload = pkg2 / "mathd.bin"
        bad_bytes = bytearray(target_payload.read_bytes())
        bad_bytes[5] ^= 0xFF
        target_payload.write_bytes(bytes(bad_bytes))

        before = dir_snapshot(self.proposals_dir)
        with self.assertRaises(ShuttleVerificationError) as ctx:
            self.importer.import_package(pkg2)
        after = dir_snapshot(self.proposals_dir)

        self.assertIn("sha256", str(ctx.exception).lower())
        self.assertEqual(before, after)

    def test_store_byte_unchanged_on_version_rollback(self) -> None:
        # Replay/rollback: package sequence is 2, state file requires > 2
        pkg_rollback = self._make_package(seq=2, version="0.1.9")

        before = dir_snapshot(self.proposals_dir)
        with self.assertRaises(ShuttleVerificationError) as ctx:
            self.importer.import_package(pkg_rollback)
        after = dir_snapshot(self.proposals_dir)

        self.assertIn("replay", str(ctx.exception).lower())
        self.assertEqual(before, after)

    def test_store_byte_unchanged_on_added_capability(self) -> None:
        pkg = self._make_package(seq=5, version="0.5.0")
        manifest_path = pkg / "release.manifest"
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        m["packages"][0]["capabilities"].append("system:root_tamper")
        manifest_path.write_text(json.dumps(m, indent=2), encoding="utf-8")

        before = dir_snapshot(self.proposals_dir)
        with self.assertRaises(ShuttleVerificationError) as ctx:
            self.importer.import_package(pkg)
        after = dir_snapshot(self.proposals_dir)

        self.assertIn("signature", str(ctx.exception).lower())
        self.assertEqual(before, after)

    def test_store_byte_unchanged_on_untrusted_signing_key(self) -> None:
        # Generate another key
        other_key_dir = self.tmp_path / "other_keys"
        other_key_dir.mkdir()
        other_seed = hashlib.sha256(b"OTHER_KEY_SEED").digest()
        import nova_trust as nt
        (other_key_dir / "TEST-root.priv").write_text(nt.private_key_pem(other_seed))
        (other_key_dir / "TEST-root.pub").write_text(nt.public_key_pem(nt.public_key(other_seed)))

        pkg = self.tmp_path / "other_pkg"
        pack_shuttle.pack_shuttle(
            out_dir=pkg,
            packages=[("mathd", "0.2.0", self.payload, ["compute:verify"])],
            sequence=3,
            version="0.2.0",
            key_dir=other_key_dir,
        )

        before = dir_snapshot(self.proposals_dir)
        with self.assertRaises(ShuttleVerificationError) as ctx:
            self.importer.import_package(pkg)
        after = dir_snapshot(self.proposals_dir)

        self.assertIn("not the trusted key", str(ctx.exception).lower())
        self.assertEqual(before, after)

    def test_store_byte_unchanged_on_malformed_manifest_schema(self) -> None:
        """Malformed manifest without release dictionary raises ShuttleVerificationError, not KeyError."""
        pkg = self._make_package(seq=6, version="0.6.0")
        manifest_path = pkg / "release.manifest"
        manifest_path.write_text('{"packages": []}\n', encoding="utf-8")

        before = dir_snapshot(self.proposals_dir)
        with self.assertRaises(ShuttleVerificationError) as ctx:
            self.importer.import_package(pkg)
        after = dir_snapshot(self.proposals_dir)

        self.assertIn("missing", str(ctx.exception).lower())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
