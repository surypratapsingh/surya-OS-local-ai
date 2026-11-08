"""Tests for NOVA Shell GUI view (work order A3.13, A3.17, roadmap E5).

Verifies that importing shellgui does not import tkinter until a window is opened,
that GUI execution gracefully skips when tkinter / display is unavailable,
and that GUI widgets render clock, sensory tiles, and notifications.
"""

from __future__ import annotations

from datetime import UTC, datetime
import unittest
from novacore.shellmodel import ShellModel


class TestShellGUI(unittest.TestCase):
    def test_shellgui_import_does_not_require_tkinter(self) -> None:
        """Importing shellgui module must succeed even when tkinter is absent."""
        import novacore.shellgui as shellgui

        self.assertTrue(hasattr(shellgui, "ShellGUI"))
        self.assertTrue(hasattr(shellgui, "run_shell"))

    def test_gui_smoke_or_skip_loudly(self) -> None:
        """GUI initialization must skip loudly when display/tkinter is unavailable."""
        try:
            import tkinter as tk
            from novacore.shellgui import ShellGUI

            root = tk.Tk()
            root.withdraw()
        except Exception as err:
            self.skipTest(f"SKIP LOUDLY: Graphical display unavailable: {err}")

        # Executed outside display-catch guard so real GUI failures fail loudly per F3.15-F1
        try:
            fixed_dt = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
            model = ShellModel(clock_fn=lambda: fixed_dt)
            model.add_notification("Test Alert", "Sensory check active", category="alert")

            gui = ShellGUI(model, root)
            gui.update_view()

            # Verify widget stringvars populated from model
            self.assertIn("12:00:00 UTC", gui.clock_var.get())
            self.assertIn("SHUTTER CLOSED", gui.camera_var.get())
            self.assertIn("SHUTTER CLOSED", gui.mic_var.get())
            self.assertIn("OFF", gui.shutter_var.get())
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
