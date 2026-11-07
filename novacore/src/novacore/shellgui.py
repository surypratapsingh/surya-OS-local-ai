"""NOVA Shell GUI view (work order A3.10, A3.13, A3.17, roadmap E5).

Tkinter-based desktop view rendering the plain state held in ShellModel:
- Header clock tile (time and date from injected clock)
- NOVA system status orb and readout
- Privacy shutter hardware controls and sensory tiles (camera and microphone tiles)
- In-memory notifications (newest first) and demand notifications list
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
        self.root.geometry("540x520")

        self._build_widgets()
        self.update_view()

    def _build_widgets(self) -> None:
        tk = self._tk
        ttk = self._ttk
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Clock Header Tile
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

        # 3. Privacy & Sensory Controls (Hardware Frame)
        hw_frame = ttk.LabelFrame(main_frame, text="Hardware & Sensory Controls", padding="10")
        hw_frame.pack(fill=tk.X, pady=(0, 10))

        # Privacy shutter master switch
        shutter_row = ttk.Frame(hw_frame)
        shutter_row.pack(fill=tk.X, pady=(0, 5))

        self.shutter_var = tk.StringVar()
        self.shutter_label = ttk.Label(
            shutter_row,
            textvariable=self.shutter_var,
            font=("Helvetica", 11, "bold"),
        )
        self.shutter_label.pack(side=tk.LEFT, padx=(0, 15))

        self.shutter_button = ttk.Button(
            shutter_row,
            text="Toggle Shutter",
            command=self._on_toggle_shutter,
        )
        self.shutter_button.pack(side=tk.RIGHT)

        # Sensory indicators (Camera and Microphone tiles)
        sensory_row = ttk.Frame(hw_frame)
        sensory_row.pack(fill=tk.X, pady=(5, 0))

        self.camera_var = tk.StringVar()
        self.camera_label = ttk.Label(
            sensory_row,
            textvariable=self.camera_var,
            font=("Helvetica", 10),
        )
        self.camera_label.pack(side=tk.LEFT, padx=(0, 20))

        self.mic_var = tk.StringVar()
        self.mic_label = ttk.Label(
            sensory_row,
            textvariable=self.mic_var,
            font=("Helvetica", 10),
        )
        self.mic_label.pack(side=tk.LEFT)

        # 4. In-Memory Notifications (Newest First)
        notif_frame = ttk.LabelFrame(main_frame, text="Notifications (Newest First)", padding="10")
        notif_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.notif_count_var = tk.StringVar()
        notif_count_label = ttk.Label(
            notif_frame,
            textvariable=self.notif_count_var,
            font=("Helvetica", 10, "italic"),
        )
        notif_count_label.pack(anchor=tk.W, pady=(0, 5))

        self.notif_listbox = tk.Listbox(notif_frame, height=6)
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

        # Update clock tile
        self.clock_var.set(f"{self.model.clock_time}  |  {self.model.clock_date}")

        # Update NOVA status
        self.status_var.set(f"NOVA: {self.model.nova_status} ({self.model.nova_message})")

        # Update Privacy Shutter Tile
        shutter_text = self.model.shutter_tile.label
        if self.model.shutter_tile.error_message:
            shutter_text += f" [{self.model.shutter_tile.error_message}]"
        self.shutter_var.set(shutter_text)

        # Update Sensory Tiles (Camera and Microphone)
        self.camera_var.set(self.model.camera_tile.label)
        self.mic_var.set(self.model.microphone_tile.label)

        # Update Notifications (RAM notifications newest first, followed by open demands)
        ram_notifs = self.model.ram_notifications
        demands = self.model.notifications
        total_count = len(ram_notifs) + len(demands)
        self.notif_count_var.set(f"Total Notifications: {total_count} ({len(ram_notifs)} RAM, {len(demands)} Offline Demands)")

        self.notif_listbox.delete(0, tk.END)
        # RAM notifications (already maintained newest first)
        for n in ram_notifs:
            self.notif_listbox.insert(tk.END, f"[{n.timestamp}] [{n.category.upper()}] {n.title}: {n.message}")
        for d in demands:
            self.notif_listbox.insert(tk.END, f"[DEMAND] [{d.item_type}] {d.title}")


def run_shell() -> None:
    """Launch the NOVA Shell GUI."""
    import tkinter as tk

    model = ShellModel()
    root = tk.Tk()
    gui = ShellGUI(model, root)
    root.mainloop()


if __name__ == "__main__":
    run_shell()
