"""R10 side-note check: `_test.stub` is out of the production `CATALOGUE`.

A test fixture does not belong in the security boundary (docs/work-orders.md
R10 "Also"). Tests that need a confirmed, *available* capability inject
their own catalogue with `unittest.mock.patch.dict`, so a real confirmed
request never writes an outbox event for a fixture.

The earlier production stub with `available_now=True` made a confirmed
request for it write a real outbox event — a test fixture inside the
boundary. `test_prompt_catalogue.py` additionally pins that no catalogue
entry starts with `_`.

Covers W4 item 4 n/a, W6 outbox transfer via a patched entry.
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
