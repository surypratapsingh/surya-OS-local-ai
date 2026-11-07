"""NOVA Shell GUI view (work order A3.10, A3.13, roadmap E5).

Tkinter-based desktop view rendering the plain state held in ShellModel:
- Header clock (time and date)
- NOVA system status orb and readout
- Demand notifications list
- Privacy shutter tile (camera capability gated)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .shellmodel import ShellModel

if TYPE_CHECKING:
    import tkinter as tk


class ShellGUI:
    """Tkinter desktop view of ShellModel."""

    def __init__(self, model: ShellModel, root: Any = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self._ttk = ttk
        self.model = model
        self.root = root or tk.Tk()
        self.root.title("NOVA Shell")
        self.root.geometry("480x420")

        self._build_widgets()
        self.update_view()

    def _build_widgets(self) -> None:
        tk = self._tk
        ttk = self._ttk
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Clock Header
        self.clock_var = tk.StringVar()
        clock_label = ttk.Label(
            main_frame,
            textvariable=self.clock_var,
            font=("Helvetica", 18, "bold"),
        )
        clock_label.pack(anchor=tk.W, pady=(0, 10))

        # 2. NOVA Status Box
        status_frame = ttk.LabelFrame(main_frame, text="System Status", padding="10")
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_var = tk.StringVar()
        status_label = ttk.Label(
            status_frame,
            textvariable=self.status_var,
            font=("Helvetica", 12),
        )
        status_label.pack(anchor=tk.W)

        # 3. Privacy Shutter Tile
        shutter_frame = ttk.LabelFrame(main_frame, text="Hardware Controls", padding="10")
        shutter_frame.pack(fill=tk.X, pady=(0, 10))

        self.shutter_var = tk.StringVar()
        self.shutter_label = ttk.Label(
            shutter_frame,
            textvariable=self.shutter_var,
            font=("Helvetica", 11, "bold"),
        )
        self.shutter_label.pack(side=tk.LEFT, padx=(0, 15))

        self.shutter_button = ttk.Button(
            shutter_frame,
            text="Toggle Shutter",
            command=self._on_toggle_shutter,
        )
        self.shutter_button.pack(side=tk.RIGHT)

        # 4. Demand Notifications Panel
        notif_frame = ttk.LabelFrame(main_frame, text="Demand Notifications", padding="10")
        notif_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.notif_count_var = tk.StringVar()
        notif_count_label = ttk.Label(
            notif_frame,
            textvariable=self.notif_count_var,
            font=("Helvetica", 10, "italic"),
        )
        notif_count_label.pack(anchor=tk.W, pady=(0, 5))

        self.notif_listbox = tk.Listbox(notif_frame, height=5)
        self.notif_listbox.pack(fill=tk.BOTH, expand=True)

        # 5. Bottom Toolbar
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill=tk.X)

        refresh_btn = ttk.Button(bottom_frame, text="Refresh", command=self.update_view)
        refresh_btn.pack(side=tk.RIGHT)

    def _on_toggle_shutter(self) -> None:
        self.model.toggle_privacy_shutter()
        self.update_view()

    def update_view(self) -> None:
        """Synchronize GUI widgets with current model state."""
        tk = self._tk
        self.model.refresh_state()

        # Update clock
        self.clock_var.set(f"{self.model.clock_time}  |  {self.model.clock_date}")

        # Update NOVA status
        self.status_var.set(f"NOVA: {self.model.nova_status} ({self.model.nova_message})")

        # Update Privacy Shutter Tile
        shutter_text = self.model.shutter_tile.label
        if self.model.shutter_tile.error_message:
            shutter_text += f" [{self.model.shutter_tile.error_message}]"
        self.shutter_var.set(shutter_text)

        # Update Notifications
        notifs = self.model.notifications
        self.notif_count_var.set(f"Pending Offline Demands: {len(notifs)}")
        self.notif_listbox.delete(0, tk.END)
        for n in notifs:
            self.notif_listbox.insert(tk.END, f"[{n.item_type}] {n.title}")


def run_shell() -> None:
    """Launch the NOVA Shell GUI."""
    import tkinter as tk
    model = ShellModel()
    root = tk.Tk()
    gui = ShellGUI(model, root)
    root.mainloop()


if __name__ == "__main__":
    run_shell()
