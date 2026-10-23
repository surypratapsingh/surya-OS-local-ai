"""R10 side-note check: `_test.stub` is out of the production `CATALOGUE`.

A test fixture does not belong in the security boundary (docs/work-orders.md
R10 "Also"). Tests that need a confirmed, *available* capability inject
their own catalogue with `unittest.mock.patch.dict`, so a real confirmed
request never writes an outbox event for a fixture.

The earlier production stub with `available_now=True` made a confirmed
request for it write a real outbox event — a test fixture inside the
boundary. `test_stub_capability_is_absent_from_the_production_boundary`
additionally pins that no catalogue entry starts with `_`.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from novacore.capabilities import CATALOGUE, Capability, CapabilityBroker, Risk


def _stub_catalogue() -> dict:
    """The production catalogue + one test-only fixture entry, for patching."""
    return {
        **CATALOGUE,
        "_test.stub": Capability(
            "_test.stub",
            Risk.LOW,
            True,
            True,
            "Test-only capability: requires confirmation, is available. "
            "Injected by the test suite; absent from the production boundary.",
        ),
    }


class CapabilityBrokerTests(unittest.TestCase):
    def test_high_impact_request_needs_confirmation_and_emits_no_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "events.jsonl"
            result = CapabilityBroker(event_path).request("system.reboot", {})
            self.assertEqual("confirmation_required", result.state)
            self.assertFalse(event_path.exists())

    def test_confirmed_request_is_still_only_a_stub_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "events.jsonl"
            with mock.patch.dict(CATALOGUE, _stub_catalogue(), clear=True):
                result = CapabilityBroker(event_path).request(
                    "_test.stub", {}, owner_confirmed=True
                )
            self.assertEqual("queued_stub", result.state)
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual("STUB_CAPABILITY_REQUEST", event["kind"])
            self.assertFalse(event["applied"])
            self.assertTrue(event["owner_confirmed"])

    def test_stub_capability_is_absent_from_the_production_boundary(self) -> None:
        # The fixture cannot be requested without the test injection: the
        # production catalogue holds no test-only entries at all.
        self.assertNotIn("_test.stub", CATALOGUE)
        for name in CATALOGUE:
            self.assertFalse(
                name.startswith("_"),
                f"production CATALOGUE contains test-like entry {name}",
            )
        with tempfile.TemporaryDirectory() as directory:
            result = CapabilityBroker(Path(directory) / "events.jsonl").request(
                "_test.stub", {}
            )
            self.assertEqual("denied", result.state)

    def test_unknown_capability_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = CapabilityBroker(Path(directory) / "events.jsonl").request("disk.erase", {})
            self.assertEqual("denied", result.state)

    def test_unavailable_capability_with_confirmation_returns_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "events.jsonl"
            result = CapabilityBroker(event_path).request(
                "camera.enable", {}, owner_confirmed=True
            )
            self.assertEqual("unavailable", result.state)
            self.assertFalse(event_path.exists())

    def test_non_json_serializable_arguments_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "events.jsonl"

            class NonSerializable:
                pass

            with mock.patch.dict(CATALOGUE, _stub_catalogue(), clear=True):
                result = CapabilityBroker(event_path).request(
                    "_test.stub", {"bad": NonSerializable()}, owner_confirmed=True
                )
            self.assertEqual("denied", result.state)
            self.assertFalse(event_path.exists())


class EventIdTests(unittest.TestCase):
    """R5 item 2: ids must be unique by construction, not by clock resolution.

    The outbox used a microsecond timestamp ("stub-%Y%m%dT%H%M%S%fZ"). Two
    requests inside the same microsecond therefore wrote the same event_id,
    which the chain hash cannot repair: nothing about the field is a
    uniqueness check. A frozen clock makes the collision deterministic.
    """

    class _FrozenDatetime:
        @classmethod
        def now(cls, tz=None) -> datetime:
            return datetime(2026, 10, 23, 12, 0, 0, 123456, tzinfo=UTC)

    def test_two_events_at_the_same_microsecond_get_distinct_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "events.jsonl"
            with (
                mock.patch.dict(CATALOGUE, _stub_catalogue(), clear=True),
                mock.patch("novacore.capabilities.datetime", self._FrozenDatetime),
            ):
                broker = CapabilityBroker(event_path)
                first = broker.request("_test.stub", {}, owner_confirmed=True)
                second = broker.request("_test.stub", {}, owner_confirmed=True)

            self.assertEqual("queued_stub", first.state)
            self.assertEqual("queued_stub", second.state)
            self.assertIsNotNone(first.event_id)
            self.assertIsNotNone(second.event_id)
            self.assertNotEqual(first.event_id, second.event_id)

            events = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(2, len(events))
            self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])
            self.assertEqual(events[0]["event_id"], first.event_id)
            self.assertEqual(events[1]["event_id"], second.event_id)


if __name__ == "__main__":
    unittest.main()
