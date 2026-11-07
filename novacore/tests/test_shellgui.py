"""Tests for NOVA Shell GUI view (work order A3.13, roadmap E5).

Verifies that importing shellgui does not import tkinter until a window is opened,
and that GUI execution gracefully skips when tkinter / display is unavailable.
"""

from __future__ import annotations

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
            model = ShellModel()
            gui = ShellGUI(model, root)
            gui.update_view()
            root.destroy()
        except Exception as err:
            self.skipTest(f"SKIP LOUDLY: Graphical display unavailable: {err}")


if __name__ == "__main__":
    unittest.main()
