from __future__ import annotations

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
