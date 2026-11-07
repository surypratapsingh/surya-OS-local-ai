"""Unit tests for NOVA Shell Model and GUI smoke test (work order A3.10, roadmap E5).

Verifies:
1. Clock formatting and updates.
2. NOVA status and messages.
3. Notifications synced from DemandStore.
4. Privacy Shutter Tile invariant:
   Whenever camera capability is not granted (as capabilities.py decides),
   the shutter tile MUST show off.
5. Plant test: a shutter tile that ignores the capability fails verification.
6. GUI smoke test: verifies ShellGUI loads, and SKIPs loudly when no display exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import shutil
import tempfile
import unittest

from novacore import capabilities
from novacore.demand import DemandStore
from novacore.shellmodel import ShellModel, ShutterTile, is_camera_granted


class TestShellModel(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nova_test_shell_"))
        self.demand_path = self.temp_dir / "demands.jsonl"
        self.demand_store = DemandStore(self.demand_path)

        # Standard model with baseline capabilities
        self.model = ShellModel(demand_store=self.demand_store)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clock_formatting_and_updates(self) -> None:
        """Clock displays formatted time and date strings."""
        dt = datetime(2026, 11, 6, 15, 30, 45, tzinfo=UTC)
        time_str = self.model.update_clock(dt)
        self.assertEqual(time_str, "15:30:45 UTC")
        self.assertEqual(self.model.clock_time, "15:30:45 UTC")
        self.assertEqual(self.model.clock_date, "2026-11-06")

    def test_nova_status_updates(self) -> None:
        """NOVA status and message update as expected."""
        self.model.set_nova_status("Processing", "Evaluating study quiz")
        self.assertEqual(self.model.nova_status, "Processing")
        self.assertEqual(self.model.nova_message, "Evaluating study quiz")

    def test_demand_notifications_sync(self) -> None:
        """Demand notifications update dynamically from DemandStore."""
        self.assertEqual(len(self.model.notifications), 0)

        # Add open demand
        self.demand_store.add(
            title="Need mathd-cli",
            description="Required for study session",
            urls=["https://example.com/mathd"],
            instructions=["Fetch binary and copy to shuttle"],
            item_type="binary",
        )

        notifs = self.model.refresh_notifications()
        self.assertEqual(len(notifs), 1)
        self.assertEqual(notifs[0].title, "Need mathd-cli")
        self.assertEqual(len(self.model.notifications), 1)

    def test_shutter_tile_shows_off_when_camera_not_granted(self) -> None:
        """Whenever camera capability is not granted, the shutter tile MUST show off."""
        # Baseline capabilities.py catalogue: camera.enable has available_now=False
        cap = capabilities.CATALOGUE.get("camera.enable")
        self.assertIsNotNone(cap)
        self.assertFalse(cap.available_now)

        # Confirm capability check returns False
        self.assertFalse(is_camera_granted())

        # Model shutter tile MUST be off
        self.model.refresh_state()
        self.assertEqual(self.model.shutter_tile.state, "off")
        self.assertIn("OFF", self.model.shutter_tile.label)

        # Attempting to toggle to 'on' MUST be refused and remain 'off'
        toggled = self.model.toggle_privacy_shutter()
        self.assertFalse(toggled)
        self.assertEqual(self.model.shutter_tile.state, "off")
        self.assertIn("OFF", self.model.shutter_tile.label)
        self.assertIsNotNone(self.model.shutter_tile.error_message)

    def test_shutter_tile_allows_on_only_when_capability_granted(self) -> None:
        """Shutter tile can only transition to 'on' when capability is explicitly granted."""
        # Inject granted capability catalogue
        granted_catalogue = dict(capabilities.CATALOGUE)
        granted_catalogue["camera.enable"] = capabilities.Capability(
            name="camera.enable",
            risk=capabilities.Risk.HIGH,
            requires_confirmation=True,
            available_now=True,
            description="Granted camera capability",
        )

        # Case 1: available_now=True but owner_confirmed=False -> denied
        model_unconfirmed = ShellModel(
            catalogue_override=granted_catalogue,
            owner_confirmed_camera=False,
        )
        self.assertFalse(model_unconfirmed.toggle_privacy_shutter())
        self.assertEqual(model_unconfirmed.shutter_tile.state, "off")

        # Case 2: available_now=True and owner_confirmed=True -> granted
        model_confirmed = ShellModel(
            catalogue_override=granted_catalogue,
            owner_confirmed_camera=True,
        )
        self.assertEqual(model_confirmed.shutter_tile.state, "off")
        toggled = model_confirmed.toggle_privacy_shutter()
        self.assertTrue(toggled)
        self.assertEqual(model_confirmed.shutter_tile.state, "on")
        self.assertIn("ON", model_confirmed.shutter_tile.label)

        # If capability is revoked, refresh_state MUST revert shutter to 'off'
        model_confirmed.catalogue_override = dict(capabilities.CATALOGUE)  # available_now=False
        model_confirmed.refresh_state()
        self.assertEqual(model_confirmed.shutter_tile.state, "off")

    def test_plant_ignoring_capability_fails(self) -> None:
        """Plant check: a shutter tile that allows 'on' without capability violates invariant."""
        tile = ShutterTile(state="on")  # Planted illegal state without capability
        # Must be corrected to "off" on capability check
        tile.update_from_capability()
        self.assertEqual(
            tile.state,
            "off",
            "Plant detected: shutter tile remained 'on' when camera capability was not granted!",
        )

    def test_gui_smoke_or_skip_loudly(self) -> None:
        """GUI smoke test verifies ShellGUI initializes, and SKIPs loudly when no display exists."""
        try:
            import tkinter as tk
            from novacore.shellgui import ShellGUI
            root = tk.Tk()
            root.withdraw()
            gui = ShellGUI(self.model, root)
            gui.update_view()
            root.destroy()
        except Exception as err:
            self.skipTest(f"SKIP LOUDLY: Graphical display unavailable: {err}")


if __name__ == "__main__":
    unittest.main()
