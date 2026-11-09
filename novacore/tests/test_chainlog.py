"""Direct tests for chainlog.py: the hash vector, the anchor round trip, check_tail.

Oracle for chain_hash: a digest from `sha256sum`, not from chainlog. Command:
    printf '%s' '{"a": 1, "b": "x"}'"$(printf '0%.0s' $(seq 64))" | sha256sum
The input is the canonical JSON of the fields (sorted keys, ", " and ": "
separators) followed by the previous hash, here GENESIS (64 zeros), as the
module docstring specifies.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from novacore import chainlog

SHA256SUM_VECTOR = "7565822e7383d0e5b00ccbddc1683a2e6a106991b6126e16c2a53833e999cf2a"
OTHER_HEAD = "ab" * 32


class TestChainHash(unittest.TestCase):
    def test_matches_sha256sum_vector(self) -> None:
        self.assertEqual(chainlog.chain_hash(chainlog.GENESIS, {"b": "x", "a": 1}), SHA256SUM_VECTOR)

    def test_previous_hash_changes_result(self) -> None:
        self.assertNotEqual(chainlog.chain_hash(OTHER_HEAD, {"a": 1, "b": "x"}), SHA256SUM_VECTOR)

    def test_field_change_changes_result(self) -> None:
        self.assertNotEqual(chainlog.chain_hash(chainlog.GENESIS, {"a": 2, "b": "x"}), SHA256SUM_VECTOR)


class TestAnchor(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log = Path(self._tmp.name) / "events.jsonl"

    def test_anchor_path_sits_next_to_log(self) -> None:
        self.assertEqual(chainlog.anchor_path(self.log), self.log.with_name("events.jsonl.anchor"))

    def test_write_anchor_file_content(self) -> None:
        self.log.write_text("x\n", encoding="utf-8")
        chainlog.write_anchor(self.log, 3, OTHER_HEAD)
        stored = json.loads(chainlog.anchor_path(self.log).read_text(encoding="utf-8"))
        self.assertEqual(stored, {"count": 3, "head": OTHER_HEAD})
        self.assertFalse(chainlog.anchor_path(self.log).with_name("events.jsonl.anchor.tmp").exists())

    def test_matching_anchor_passes(self) -> None:
        self.log.write_text("x\n", encoding="utf-8")
        chainlog.write_anchor(self.log, 3, OTHER_HEAD)
        chainlog.check_tail(self.log, 3, OTHER_HEAD, 3)

    def test_dropped_record_raises_with_line(self) -> None:
        self.log.write_text("x\n", encoding="utf-8")
        chainlog.write_anchor(self.log, 3, OTHER_HEAD)
        with self.assertRaisesRegex(ValueError, r"anchor mismatch at .*:2"):
            chainlog.check_tail(self.log, 2, "cd" * 32, 2)

    def test_same_count_different_head_raises(self) -> None:
        self.log.write_text("x\n", encoding="utf-8")
        chainlog.write_anchor(self.log, 3, OTHER_HEAD)
        with self.assertRaisesRegex(ValueError, "anchor mismatch"):
            chainlog.check_tail(self.log, 3, "cd" * 32, 3)

    def test_missing_anchor_with_records_raises(self) -> None:
        self.log.write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no anchor"):
            chainlog.check_tail(self.log, 1, OTHER_HEAD, 1)

    def test_missing_anchor_and_empty_log_passes(self) -> None:
        chainlog.check_tail(self.log, 0, chainlog.GENESIS, 0)

    def test_unreadable_anchor_raises(self) -> None:
        self.log.write_text("x\n", encoding="utf-8")
        for bad in ("not json", "{}", '{"count": "n", "head": "h"}', "[1]"):
            chainlog.anchor_path(self.log).write_text(bad, encoding="utf-8")
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "unreadable anchor"):
                chainlog.check_tail(self.log, 1, OTHER_HEAD, 1)

    def test_deleted_log_with_anchor_raises(self) -> None:
        chainlog.write_anchor(self.log, 3, OTHER_HEAD)
        with self.assertRaisesRegex(ValueError, "is missing"):
            chainlog.check_tail(self.log, 0, chainlog.GENESIS, 0)

    def test_adopt_writes_then_refuses_overwrite(self) -> None:
        chainlog.adopt(self.log, 2, OTHER_HEAD)
        self.assertTrue(chainlog.anchor_path(self.log).exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            chainlog.adopt(self.log, 5, "cd" * 32)
        stored = json.loads(chainlog.anchor_path(self.log).read_text(encoding="utf-8"))
        self.assertEqual(stored["count"], 2)


if __name__ == "__main__":
    unittest.main()
