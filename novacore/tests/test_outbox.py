"""W6 / R12: the capability outbox is verified on every write and fails closed.

R8 (`docs/logs/r8-w6-tamper.log`) found that `request()` read only the last
line and, on a corrupt line, restarted the chain at sequence 1. These tests
state what R12 asks instead: a broken outbox raises, names the line, and is
left unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from novacore.capabilities import CATALOGUE, Capability, CapabilityBroker, Risk


# CATALOGUE has no capability that is available now and writes an event, so
# the tests inject one. Not part of the product boundary.
FIXTURE = Capability("_fixture.write", Risk.LOW, False, True, "test fixture")


def _chain(fields: dict, previous: str) -> str:
    """The chain format as the capabilities module docstring states it, written from that text."""
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256((canonical + previous).encode("utf-8")).hexdigest()


def _event(sequence: int, previous: str) -> dict:
    fields = {
        "sequence": sequence,
        "event_id": f"hand-{sequence}",
        "created_at": "2026-10-13T00:00:00+00:00",
        "kind": "STUB_CAPABILITY_REQUEST",
        "capability": {"name": "_fixture.write"},
        "arguments": {},
        "owner_confirmed": False,
        "applied": False,
    }
    return {**fields, "chain_hash": _chain(fields, previous)}


@mock.patch.dict(CATALOGUE, {FIXTURE.name: FIXTURE})
class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "events.jsonl"
        self.broker = CapabilityBroker(self.path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _write(self, count: int) -> None:
        for number in range(count):
            self.assertEqual("queued_stub", self.broker.request(FIXTURE.name, {"n": number}).state)

    def _assert_refused(self, *fragments: str) -> None:
        """The next request raises with every fragment in its message and leaves the outbox as it was."""
        before = self.path.read_bytes() if self.path.exists() else None
        with self.assertRaises(ValueError) as caught:
            self.broker.request(FIXTURE.name, {})
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(before, self.path.read_bytes() if self.path.exists() else None)

    def test_events_are_chained_numbered_and_anchored(self) -> None:
        self._write(3)
        events = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([1, 2, 3], [event["sequence"] for event in events])
        previous = "0" * 64
        for event in events:
            fields = {key: value for key, value in event.items() if key != "chain_hash"}
            self.assertEqual(_chain(fields, previous), event["chain_hash"])
            previous = event["chain_hash"]
        anchor = json.loads(self.path.with_name("events.jsonl.anchor").read_text(encoding="utf-8"))
        self.assertEqual({"count": 3, "head": previous}, anchor)

    def test_cut_short_event_is_refused_with_its_line(self) -> None:
        self._write(3)
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text[: len(text) - 40], encoding="utf-8", newline="\n")
        self._assert_refused(f"{self.path}:3")

    def test_event_dropped_at_a_line_boundary_is_refused(self) -> None:
        self._write(3)
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.path.write_text("".join(lines[:2]), encoding="utf-8", newline="\n")
        self._assert_refused("anchor mismatch", f"{self.path}:2;")

    def test_deleted_middle_event_is_refused_with_its_line(self) -> None:
        self._write(3)
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.path.write_text(lines[0] + lines[2], encoding="utf-8", newline="\n")
        self._assert_refused(f"{self.path}:2;")

    def test_tampered_event_is_refused_with_its_line(self) -> None:
        # Sequence and JSON stay valid; only the chain can see this.
        self._write(3)
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertIn('"owner_confirmed": false', lines[1])
        lines[1] = lines[1].replace('"owner_confirmed": false', '"owner_confirmed": true')
        self.path.write_text("".join(lines), encoding="utf-8", newline="\n")
        self._assert_refused("chain hash mismatch", f"{self.path}:2;")

    def test_sequence_gap_in_a_valid_chain_is_refused(self) -> None:
        first = _event(1, "0" * 64)
        third = _event(3, first["chain_hash"])
        lines = [json.dumps(event, sort_keys=True) + "\n" for event in (first, third)]
        self.path.write_text("".join(lines), encoding="utf-8", newline="\n")
        self._assert_refused("sequence gap", f"{self.path}:2;")

    def test_malformed_events_are_refused_with_their_line(self) -> None:
        for bad in ("{}", "[1]", '"text"', '{"sequence": 2'):
            with self.subTest(bad=bad):
                self.path.write_text(json.dumps(_event(1, "0" * 64)) + "\n" + bad + "\n", encoding="utf-8", newline="\n")
                self._assert_refused(f"{self.path}:2")

    def test_deleted_outbox_with_anchor_is_refused(self) -> None:
        self._write(2)
        self.path.unlink()
        self._assert_refused("missing")

    def test_unanchored_outbox_is_refused_until_the_owner_anchors_it(self) -> None:
        self._write(2)
        self.path.with_name("events.jsonl.anchor").unlink()
        self._assert_refused("no anchor")
        self.broker.anchor()
        self.assertEqual("queued_stub", self.broker.request(FIXTURE.name, {}).state)
        last = json.loads(self.path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(3, last["sequence"])


if __name__ == "__main__":
    unittest.main()
