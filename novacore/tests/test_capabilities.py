from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from novacore.capabilities import CapabilityBroker


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
            result = CapabilityBroker(event_path).request(
                "_test.stub", {}, owner_confirmed=True
            )
            self.assertEqual("queued_stub", result.state)
            event = json.loads(event_path.read_text(encoding="utf-8"))
            self.assertEqual("STUB_CAPABILITY_REQUEST", event["kind"])
            self.assertFalse(event["applied"])
            self.assertTrue(event["owner_confirmed"])

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

            result = CapabilityBroker(event_path).request(
                "_test.stub", {"bad": NonSerializable()}, owner_confirmed=True
            )
            self.assertEqual("denied", result.state)
            self.assertFalse(event_path.exists())
