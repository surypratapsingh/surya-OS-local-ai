from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from novacore.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def test_memory_is_created_only_by_explicit_add(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            self.assertEqual([], store.all())
            self.assertFalse(path.exists())
            created = store.add("Remember that I prefer short answers.")
            self.assertTrue(path.exists())
            self.assertEqual([created], store.search("short answer", limit=3))

    def test_tampered_memory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("A trusted note")
            text = path.read_text(encoding="utf-8").replace("trusted", "altered")
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                store.all()

    def test_chain_detects_text_change_bypassing_content_hash(self) -> None:
        """Chain detection: attacker changes text and updates content_sha256 but forgets chain."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            import hashlib
            store = MemoryStore(path)
            store.add("First note")
            store.add("Second note")

            # Sophisticated tampering: change text, update its hash, but leave chain invalid.
            lines = path.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["text"] = "Modified text"
            record["content_sha256"] = hashlib.sha256(b"Modified text").hexdigest()
            # Intentionally don't update chain_hash; it's now stale.
            lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            # Content hash passes, but chain hash fails — proving chain is the detection here.
            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("chain hash mismatch", str(cm.exception))

    def test_chain_detects_record_deletion(self) -> None:
        """Mutation: delete a record from the middle."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")
            store.add("Third")

            # Remove middle record.
            lines = path.read_text(encoding="utf-8").splitlines()
            lines.pop(1)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("chain hash mismatch", str(cm.exception))

    def test_chain_detects_record_swap(self) -> None:
        """Mutation: swap two records."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")

            # Swap records.
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[0], lines[1] = lines[1], lines[0]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("chain hash mismatch", str(cm.exception))

    def test_chain_detects_timestamp_change(self) -> None:
        """Mutation: alter a record's timestamp."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("Note")

            # Change timestamp.
            lines = path.read_text(encoding="utf-8").splitlines()
            record = json.loads(lines[0])
            record["created_at"] = "2000-01-01T00:00:00+00:00"
            lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("chain hash mismatch", str(cm.exception))

    def test_chain_detects_stale_chain_link(self) -> None:
        """Mutation: append a record with valid self-hash but stale chain link."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")

            # Append a fake record with recomputed self-hash but wrong chain.
            import hashlib
            fake_record = {
                "memory_id": "fake-id",
                "created_at": "2024-01-01T00:00:00+00:00",
                "text": "Injected",
                "content_sha256": hashlib.sha256(b"Injected").hexdigest(),
                "chain_hash": "0" * 64,  # Wrong: should link to previous record
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(fake_record, ensure_ascii=False, sort_keys=True) + "\n")

            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("chain hash mismatch", str(cm.exception))

    def test_chain_detects_truncation(self) -> None:
        """Mutation: truncate the file mid-chain."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")
            store.add("Third")

            # Truncate file in the middle of the third record.
            text = path.read_text(encoding="utf-8")
            path.write_text(text[: len(text) // 2], encoding="utf-8")

            with self.assertRaises(ValueError):
                store.all()
