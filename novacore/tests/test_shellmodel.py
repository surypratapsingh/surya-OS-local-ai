"""Unit tests for NOVA Shell Model (work orders A3.10, A3.17, roadmap E5).

Verifies:
1. Clock tile fed by injected clock (zero dependency on real time).
2. NOVA status and messages.
3. In-memory notification list in RAM: newest first, bounded by max size.
4. Privacy Shutter Invariant:
   - When the privacy shutter is closed, every tile that could show camera or
     microphone state must say so in every frame.
   - Whenever camera capability is not granted, the shutter tile MUST show off.
5. Plant tests:
   - Shutter tile ignoring capability fails verification.
   - Leaving one tile out of privacy shutter coverage fails verification.
6. GUI smoke test with tkinter skip guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import shutil
import tempfile
import unittest

from novacore import capabilities
from novacore.demand import DemandStore
from novacore.shellmodel import (
    CameraTile,
    ClockTile,
    MicrophoneTile,
    SensoryTile,
    ShellModel,
    ShellNotification,
    ShutterTile,
    is_camera_granted,
)


def verify_shutter_coverage(model: ShellModel) -> None:
    """Verify invariant: when shutter is closed, every sensory tile says so."""
    if model.shutter_tile.state == "off":
        for channel_name, tile in [("camera", model.camera_tile), ("microphone", model.microphone_tile)]:
            if not tile.shutter_closed:
                raise AssertionError(f"Invariant violation: {channel_name} tile shutter_closed is False!")
            if "SHUTTER CLOSED" not in tile.label:
                raise AssertionError(
                    f"Invariant violation: {channel_name} tile label '{tile.label}' missing 'SHUTTER CLOSED'!"
                )


class TestShellModel(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nova_test_shell_"))
        self.demand_path = self.temp_dir / "demands.jsonl"
        self.demand_store = DemandStore(self.demand_path)

        # Standard model with baseline capabilities
        self.model = ShellModel(demand_store=self.demand_store)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clock_tile_with_injected_clock(self) -> None:
        """Clock tile reflects injected clock without relying on wall-clock time."""
        fixed_dt = datetime(2026, 11, 7, 9, 30, 15, tzinfo=UTC)
        model = ShellModel(clock_fn=lambda: fixed_dt)

        self.assertEqual(model.clock_tile.time_str, "09:30:15 UTC")
        self.assertEqual(model.clock_tile.date_str, "2026-11-07")
        self.assertEqual(model.clock_time, "09:30:15 UTC")
        self.assertEqual(model.clock_date, "2026-11-07")

        # Advance injected clock
        later_dt = datetime(2026, 11, 7, 10, 45, 0, tzinfo=UTC)
        model.clock_tile.clock_fn = lambda: later_dt
        model.update_clock()
        self.assertEqual(model.clock_tile.time_str, "10:45:00 UTC")

    def test_nova_status_updates(self) -> None:
        """NOVA status and message update as expected."""
        self.model.set_nova_status("Processing", "Evaluating study quiz")
        self.assertEqual(self.model.nova_status, "Processing")
        self.assertEqual(self.model.nova_message, "Evaluating study quiz")

    def test_notifications_ram_newest_first_and_max_size(self) -> None:
        """RAM notification list is ordered newest first and bounded by max_notifications."""
        self.model.max_notifications = 3
        self.assertEqual(len(self.model.ram_notifications), 0)

        self.model.add_notification("First", "Oldest message", category="info", timestamp="10:00:00 UTC")
        self.model.add_notification("Second", "Middle message", category="warn", timestamp="10:01:00 UTC")
        self.model.add_notification("Third", "Newest message", category="alert", timestamp="10:02:00 UTC")

        # Must be newest first: Third, Second, First
        titles = [n.title for n in self.model.ram_notifications]
        self.assertEqual(titles, ["Third", "Second", "First"])
        self.assertEqual(self.model.ram_notifications[0].title, "Third")

        # Adding fourth notification must drop the oldest ("First")
        self.model.add_notification("Fourth", "Latest message", category="info", timestamp="10:03:00 UTC")
        self.assertEqual(len(self.model.ram_notifications), 3)
        titles_after = [n.title for n in self.model.ram_notifications]
        self.assertEqual(titles_after, ["Fourth", "Third", "Second"])

    def test_demand_notifications_sync(self) -> None:
        """Demand notifications update dynamically from DemandStore."""
        self.assertEqual(len(self.model.notifications), 0)

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

    def test_privacy_shutter_closed_shown_on_every_sensory_tile(self) -> None:
        """When privacy shutter is closed, every sensory tile says so in every frame."""
        self.model.refresh_state()
        self.assertEqual(self.model.shutter_tile.state, "off")

        # Invariant check across all sensory tiles (camera, microphone)
        sensory_tiles = self.model.get_sensory_tiles()
        self.assertGreaterEqual(len(sensory_tiles), 2)
        for tile in sensory_tiles:
            self.assertTrue(tile.shutter_closed, f"{tile.channel} must have shutter_closed=True")
            self.assertIn("SHUTTER CLOSED", tile.label, f"{tile.channel} label must contain 'SHUTTER CLOSED'")
            self.assertEqual(tile.status, "SHUTTER CLOSED")

        # Verify helper passes on clean model
        verify_shutter_coverage(self.model)

    def test_shutter_tile_shows_off_when_camera_not_granted(self) -> None:
        """Whenever camera capability is not granted, the shutter tile MUST show off."""
        cap = capabilities.CATALOGUE.get("camera.enable")
        self.assertIsNotNone(cap)
        self.assertFalse(cap.available_now)

        self.assertFalse(is_camera_granted())

        self.model.refresh_state()
        self.assertEqual(self.model.shutter_tile.state, "off")
        self.assertIn("OFF", self.model.shutter_tile.label)

        toggled = self.model.toggle_privacy_shutter()
        self.assertFalse(toggled)
        self.assertEqual(self.model.shutter_tile.state, "off")

    def test_shutter_tile_allows_on_only_when_capability_granted(self) -> None:
        """Shutter tile can only transition to 'on' when capability is explicitly granted."""
        granted_catalogue = dict(capabilities.CATALOGUE)
        granted_catalogue["camera.enable"] = capabilities.Capability(
            name="camera.enable",
            risk=capabilities.Risk.HIGH,
            requires_confirmation=True,
            available_now=True,
            description="Granted camera capability",
        )

        model_confirmed = ShellModel(
            catalogue_override=granted_catalogue,
            owner_confirmed_camera=True,
        )
        toggled = model_confirmed.toggle_privacy_shutter()
        self.assertTrue(toggled)
        self.assertEqual(model_confirmed.shutter_tile.state, "on")

        # When shutter is 'on', sensory tiles show streaming/active
        self.assertFalse(model_confirmed.camera_tile.shutter_closed)
        self.assertEqual(model_confirmed.camera_tile.status, "STREAMING")

    def test_plant_leaving_one_sensory_tile_out_fails(self) -> None:
        """Plant check: omitting a sensory tile inside refresh_state violates invariant."""
        # Clean model passes
        self.model.refresh_state()
        verify_shutter_coverage(self.model)

        # Planted model: refresh_state omits the microphone tile inside model logic
        class PlantedOmissionModel(ShellModel):
            def refresh_state(self) -> None:
                is_closed = self.shutter_tile.state == "off"
                self.camera_tile.update(is_shutter_closed=is_closed, is_streaming=(not is_closed))

        planted_model = PlantedOmissionModel(demand_store=self.demand_store)
        planted_model.refresh_state()

        with self.assertRaises(AssertionError) as ctx:
            verify_shutter_coverage(planted_model)
        self.assertIn("microphone", str(ctx.exception).lower())

    def test_plant_ignoring_capability_fails(self) -> None:
        """Plant check: a shutter tile that allows 'on' without capability violates invariant."""
        tile = ShutterTile(state="on")
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
        except Exception as err:
            self.skipTest(f"SKIP LOUDLY: Graphical display unavailable: {err}")

        # Executed outside display-catch guard so real GUI failures fail loudly per F3.15-F1
        try:
            gui = ShellGUI(self.model, root)
            gui.update_view()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
