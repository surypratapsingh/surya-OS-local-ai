from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from novacore.memory import MemoryStore


def _chain(fields: dict, previous: str) -> str:
    """The chain format as the MemoryStore docstring states it, written from that text."""
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((canonical + previous).encode("utf-8")).hexdigest()


def _record(text: str, previous: str, number: int) -> dict:
    fields = {
        "memory_id": f"hand-{number}",
        "created_at": "2026-10-13T00:00:00+00:00",
        "text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return {**fields, "chain_hash": _chain(fields, previous)}


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
            self.assertIn(f"{path}:1;", str(cm.exception))

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
            self.assertIn(f"{path}:2;", str(cm.exception))

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
            self.assertIn(f"{path}:1;", str(cm.exception))

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
            self.assertIn(f"{path}:1;", str(cm.exception))

    def test_chain_detects_stale_chain_link(self) -> None:
        """Mutation: append a record whose digest is correctly computed over a stale link."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")

            # A real digest, made the documented way, but over record 1's
            # chain_hash instead of record 2's.
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            injected = _record("Injected", first["chain_hash"], 3)
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(injected, ensure_ascii=False, sort_keys=True) + "\n")

            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("chain hash mismatch", str(cm.exception))
            self.assertIn(f"{path}:3;", str(cm.exception))

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

    def test_truncation_at_record_boundary_is_detected(self) -> None:
        """Mutation: drop the last records at a line boundary. What is left is a valid, shorter chain."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            for text in ("First", "Second", "Third"):
                store.add(text)

            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n", encoding="utf-8", newline="\n")

            with self.assertRaises(ValueError) as cm:
                MemoryStore(path).all()
            self.assertIn("anchor mismatch", str(cm.exception))
            self.assertIn(f"{path}:1;", str(cm.exception))

    def test_deleted_log_is_detected(self) -> None:
        """Deleting the log but not its anchor must not read as an empty store."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            MemoryStore(path).add("First")
            path.unlink()

            with self.assertRaises(ValueError) as cm:
                MemoryStore(path).all()
            self.assertIn("missing", str(cm.exception))

    def test_cache_notices_size_change_with_restored_mtime(self) -> None:
        """W6 item 3: the cache is keyed on mtime and size, so restoring mtime is not enough."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            reader = MemoryStore(path)
            for text in ("First", "Second", "Third"):
                reader.add(text)
            self.assertEqual(3, len(reader.all()))

            before = path.stat()
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8", newline="\n")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)
            self.assertNotEqual(before.st_size, path.stat().st_size)

            with self.assertRaises(ValueError) as cm:
                reader.all()
            self.assertIn(f"{path}:2;", str(cm.exception))

    def test_add_refuses_to_extend_a_broken_chain(self) -> None:
        """A broken log must not get a fresh chain appended to it."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(lines[1] + "\n" + lines[0] + "\n", encoding="utf-8", newline="\n")
            broken = path.read_bytes()

            with self.assertRaises(ValueError):
                store.add("Third")
            self.assertEqual(broken, path.read_bytes())

    def test_unanchored_log_is_refused_until_the_owner_anchors_it(self) -> None:
        """Migration: a log written outside add() has no anchor; anchor() adopts it once."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            first = _record("First", "0" * 64, 1)
            second = _record("Second", first["chain_hash"], 2)
            path.write_text(
                "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in (first, second)),
                encoding="utf-8",
                newline="\n",
            )
            store = MemoryStore(path)
            with self.assertRaises(ValueError) as cm:
                store.all()
            self.assertIn("no anchor", str(cm.exception))

            store.anchor()
            self.assertEqual(["First", "Second"], [memory.text for memory in store.all()])
            with self.assertRaises(ValueError):
                store.anchor()

    def test_anchor_refuses_a_broken_chain(self) -> None:
        """anchor() must not bless a log that does not verify."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            first = _record("First", "0" * 64, 1)
            second = _record("Second", "0" * 64, 2)  # links to genesis, not to record 1
            path.write_text(
                "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in (first, second)),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(ValueError) as cm:
                MemoryStore(path).anchor()
            self.assertIn(f"{path}:2;", str(cm.exception))
            self.assertEqual(["memories.jsonl"], sorted(p.name for p in Path(directory).iterdir()))

    def test_v1_record_is_refused(self) -> None:
        """W6 item 5: a v1 record (no chain_hash) is refused, not silently accepted."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            v1 = {
                "memory_id": "v1-id",
                "created_at": "2026-09-01T00:00:00+00:00",
                "text": "Old note",
                "content_sha256": hashlib.sha256(b"Old note").hexdigest(),
            }
            path.write_text(json.dumps(v1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

            with self.assertRaises(ValueError) as cm:
                MemoryStore(path).all()
            self.assertIn("v1 memory record", str(cm.exception))
            self.assertIn(f"{path}:1;", str(cm.exception))


class IterSummariesTests(unittest.TestCase):
    """R5 item 3: an accessor typed Iterable must not do eager I/O.

    W6 (084fb0d) wrapped the return value in iter(), which fixed the type half
    of the deferred item but left iter_summaries() reading and verifying the
    whole log on the call itself. A broken chain therefore raised ValueError
    from a call that only asked for an iterator, before anything was consumed.
    """

    def test_iter_summaries_does_not_read_the_log_until_iterated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            store.add("First")
            store.add("Second")

            # Swap the two records: the chain no longer verifies.
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text(
                lines[1] + "\n" + lines[0] + "\n", encoding="utf-8", newline="\n"
            )

            summaries = store.iter_summaries()
            with self.assertRaises(ValueError):
                list(summaries)

    def test_iter_summaries_yields_the_verified_records_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memories.jsonl"
            store = MemoryStore(path)
            for text in ("First", "Second", "Third"):
                store.add(text)
            self.assertEqual(
                ["First", "Second", "Third"],
                [memory.text for memory in store.iter_summaries()],
            )
