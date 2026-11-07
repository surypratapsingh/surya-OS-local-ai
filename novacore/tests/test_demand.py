"""Tests for novacore.demand (Demand List and Shuttle export)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from novacore.demand import DemandEntry, DemandStore, DemandList


class TestDemandStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp_dir.name) / "demands.jsonl"
        self.store = DemandStore(self.path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_add_and_list_demand(self) -> None:
        entry = self.store.add(
            title="Class 11 Matrices study pack",
            description="Need syllabus notes, formulas, and practice problems for matrix arithmetic.",
            urls=["https://example.org/study-packs/matrices-11.pkg"],
            instructions=[
                "On an internet PC, download matrices-11.pkg from the URL.",
                "Verify the SHA-256 hash matches the expected value.",
                "Copy matrices-11.pkg to the shuttle pendrive under /packages.",
            ],
            item_type="study-pack",
            target_filename="matrices-11.pkg",
            expected_sha256="a" * 64,
        )

        self.assertTrue(entry.entry_id.startswith("demand-"))
        self.assertEqual(entry.title, "Class 11 Matrices study pack")
        self.assertEqual(entry.status, "open")
        self.assertEqual(entry.item_type, "study-pack")
        self.assertEqual(len(entry.instructions), 3)
        self.assertEqual(entry.urls, ("https://example.org/study-packs/matrices-11.pkg",))

        # Check retrieval
        open_entries = self.store.list(status="open")
        self.assertEqual(len(open_entries), 1)
        self.assertEqual(open_entries[0].entry_id, entry.entry_id)

        got = self.store.get(entry.entry_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.title, entry.title)

    def test_validation_rules(self) -> None:
        # Empty title
        with self.assertRaises(ValueError):
            self.store.add("", "desc", ["http://x"], ["step 1"])

        # Empty description
        with self.assertRaises(ValueError):
            self.store.add("title", "", ["http://x"], ["step 1"])

        # Empty instructions
        with self.assertRaises(ValueError):
            self.store.add("title", "desc", ["http://x"], [])

        # Non-string URL
        with self.assertRaises(TypeError):
            self.store.add("title", "desc", [123], ["step 1"])  # type: ignore

        # Overlong title
        with self.assertRaises(ValueError):
            self.store.add("a" * 201, "desc", ["http://x"], ["step 1"])

        # Overlong description
        with self.assertRaises(ValueError):
            self.store.add("title", "a" * 4001, ["http://x"], ["step 1"])

        # Bad SHA-256 format
        with self.assertRaises(ValueError):
            self.store.add("title", "desc", ["http://x"], ["step 1"], expected_sha256="not-a-hash")

    def test_persistence_across_instances(self) -> None:
        e1 = self.store.add("Item 1", "Desc 1", ["http://1"], ["Step 1"])
        e2 = self.store.add("Item 2", "Desc 2", ["http://2"], ["Step 2"])

        # Reopen with a new DemandStore instance
        store2 = DemandStore(self.path)
        entries = store2.all()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].entry_id, e1.entry_id)
        self.assertEqual(entries[1].entry_id, e2.entry_id)

    def test_status_lifecycle(self) -> None:
        e = self.store.add("Item 1", "Desc 1", ["http://1"], ["Step 1"])
        self.assertEqual(e.status, "open")

        # Resolve
        self.assertTrue(self.store.resolve(e.entry_id))
        self.assertEqual(self.store.list(status="open"), [])
        self.assertEqual(len(self.store.list(status="fulfilled")), 1)

        # Dismiss another
        e2 = self.store.add("Item 2", "Desc 2", ["http://2"], ["Step 2"])
        self.assertTrue(self.store.dismiss(e2.entry_id))
        self.assertEqual(len(self.store.list(status="dismissed")), 1)

    def test_corrupted_file_detection(self) -> None:
        self.store.add("Item 1", "Desc 1", ["http://1"], ["Step 1"])
        with self.path.open("a", encoding="utf-8") as f:
            f.write("corrupted non-json line\n")

        store2 = DemandStore(self.path)
        with self.assertRaises(ValueError):
            store2.list()

    def test_export_to_directory(self) -> None:
        open_entry = self.store.add(
            title="Qwen 1.5B Q4 model weights",
            description="Required for higher-tier reasoning on offline study prompts.",
            urls=["https://models.example.org/qwen-1.5b-q4.bin"],
            instructions=[
                "Download qwen-1.5b-q4.bin from the URL.",
                "Verify file size is approximately 950 MB.",
                "Copy to pendrive under /models/qwen-1.5b-q4.bin.",
            ],
            item_type="model",
            target_filename="qwen-1.5b-q4.bin",
        )

        resolved_entry = self.store.add(
            title="Resolved Math Library Binary",
            description="Already fetched and installed.",
            urls=["https://example.org/mathd"],
            instructions=["None"],
            item_type="binary",
        )
        self.store.resolve(resolved_entry.entry_id)

        dismissed_entry = self.store.add(
            title="Dismissed Outdated Tool",
            description="No longer needed.",
            urls=["https://example.org/oldtool"],
            instructions=["None"],
            item_type="tool",
        )
        self.store.dismiss(dismissed_entry.entry_id)

        # Verify that all 3 exist in the store under their respective statuses
        self.assertEqual(len(self.store.list(status="open")), 1)
        self.assertEqual(len(self.store.list(status="fulfilled")), 1)
        self.assertEqual(len(self.store.list(status="dismissed")), 1)
        self.assertEqual(len(self.store.list(status=None)), 3)

        export_dir = Path(self.tmp_dir.name) / "pendrive_shuttle"
        md_path, json_path = self.store.export(export_dir)

        self.assertTrue(md_path.exists())
        self.assertTrue(json_path.exists())

        md_content = md_path.read_text(encoding="utf-8")
        self.assertIn("# NOVA Shuttle Demand List", md_content)
        self.assertIn("Qwen 1.5B Q4 model weights", md_content)
        self.assertIn("https://models.example.org/qwen-1.5b-q4.bin", md_content)
        self.assertIn("1. Download qwen-1.5b-q4.bin from the URL.", md_content)

        # F3.12 F1: Neither resolved nor dismissed demands must appear in exported DEMAND.md
        self.assertNotIn("Resolved Math Library Binary", md_content)
        self.assertNotIn("Dismissed Outdated Tool", md_content)

        json_content = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(json_content["format"], "nova-demand-export-v1")
        # Invariant: export with default status_filter='open' exports strictly open demands
        self.assertEqual(json_content["count"], 1)
        exported_titles = [d["title"] for d in json_content["demands"]]
        self.assertEqual(exported_titles, ["Qwen 1.5B Q4 model weights"])
        self.assertNotIn("Resolved Math Library Binary", exported_titles)
        self.assertNotIn("Dismissed Outdated Tool", exported_titles)

    def test_alias_equivalence(self) -> None:
        self.assertIs(DemandList, DemandStore)


if __name__ == "__main__":
    unittest.main()
