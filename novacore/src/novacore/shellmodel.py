"""NOVA Shell State Model (work orders A3.10, A3.17, roadmap E5).

Holds plain state for the NOVA Shell GUI (pure, no tkinter dependencies):
1. Clock Tile: Injected clock provider so tests never depend on real time.
2. RAM Notification List: In-memory notification entries with max size, newest first.
3. Privacy Shutter Invariant:
   - When the privacy shutter is closed, every tile that could show camera or
     microphone state must say so in every frame.
   - Whenever camera capability is not granted, the shutter tile MUST show off.
4. Sensory Tiles: CameraTile and MicrophoneTile strictly bound to privacy shutter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from . import capabilities
from .demand import DemandEntry, DemandStore


def is_camera_granted(
    broker: capabilities.CapabilityBroker | None = None,
    owner_confirmed: bool = False,
    catalogue: dict[str, capabilities.Capability] | None = None,
) -> bool:
    """Decide whether the camera capability is granted according to capabilities.py.

    Fails closed: returns False unless the capability exists in the catalogue,
    is marked available_now, owner confirmation requirements are met, and
    broker request (if provided) is not denied or unavailable.
    """
    cat = catalogue if catalogue is not None else capabilities.CATALOGUE
    cap = cat.get("camera.enable")
    if cap is None:
        return False

    if not cap.available_now:
        return False

    if cap.requires_confirmation and not owner_confirmed:
        return False

    if broker is not None:
        result = broker.request("camera.enable", {}, owner_confirmed=owner_confirmed)
        if result.state in ("denied", "unavailable", "confirmation_required"):
            return False

    return True


@dataclass
class ClockTile:
    """Clock tile fed by an injected clock function to avoid reliance on wall-clock time."""

    clock_fn: Callable[[], datetime] | None = None
    time_str: str = ""
    date_str: str = ""
    iso_str: str = ""

    def update(self, now: datetime | None = None) -> str:
        """Update time and date strings via injected clock or explicit timestamp."""
        if now is not None:
            dt = now
        elif self.clock_fn is not None:
            dt = self.clock_fn()
        else:
            dt = datetime.now(UTC)

        self.time_str = dt.strftime("%H:%M:%S UTC")
        self.date_str = dt.strftime("%Y-%m-%d")
        self.iso_str = dt.isoformat()
        return self.time_str


@dataclass(frozen=True)
class ShellNotification:
    """In-memory notification entry in RAM."""

    title: str
    message: str
    timestamp: str
    category: str = "info"


@dataclass
class SensoryTile:
    """Base tile representing a sensory input channel (camera, microphone)."""

    channel: str  # "camera" or "microphone"
    shutter_closed: bool = True
    status: str = "SHUTTER CLOSED"
    label: str = ""

    def update(self, is_shutter_closed: bool, is_streaming: bool = False) -> None:
        self.shutter_closed = is_shutter_closed
        if is_shutter_closed:
            self.status = "SHUTTER CLOSED"
            self.label = f"{self.channel.capitalize()}: SHUTTER CLOSED"
        elif is_streaming:
            self.status = "STREAMING"
            self.label = f"{self.channel.capitalize()}: STREAMING"
        else:
            self.status = "STANDBY"
            self.label = f"{self.channel.capitalize()}: STANDBY"


@dataclass
class CameraTile(SensoryTile):
    channel: str = "camera"
    label: str = "Camera: SHUTTER CLOSED"


@dataclass
class MicrophoneTile(SensoryTile):
    channel: str = "microphone"
    label: str = "Microphone: SHUTTER CLOSED"


@dataclass
class ShutterTile:
    """Privacy shutter tile state bound strictly to the camera capability."""

    state: str = "off"  # "off" (closed/private) or "on" (open/streaming)
    label: str = "Privacy Shutter: OFF"
    error_message: str | None = None

    def update_from_capability(
        self,
        broker: capabilities.CapabilityBroker | None = None,
        owner_confirmed: bool = False,
        catalogue: dict[str, capabilities.Capability] | None = None,
    ) -> str:
        """Enforce: Whenever camera capability is not granted, shutter tile MUST show off."""
        granted = is_camera_granted(
            broker=broker,
            owner_confirmed=owner_confirmed,
            catalogue=catalogue,
        )
        if not granted:
            self.state = "off"
            self.label = "Privacy Shutter: OFF"
            self.error_message = "Camera capability not granted by capabilities.py"
        elif self.state == "on":
            self.label = "Privacy Shutter: ON"
            self.error_message = None
        else:
            self.label = "Privacy Shutter: OFF"
            self.error_message = None
        return self.state

    def request_toggle(
        self,
        broker: capabilities.CapabilityBroker | None = None,
        owner_confirmed: bool = False,
        catalogue: dict[str, capabilities.Capability] | None = None,
    ) -> bool:
        granted = is_camera_granted(
            broker=broker,
            owner_confirmed=owner_confirmed,
            catalogue=catalogue,
        )
        if not granted:
            self.state = "off"
            self.label = "Privacy Shutter: OFF"
            self.error_message = "Denied: Camera capability is not granted."
            return False

        if self.state == "off":
            self.state = "on"
            self.label = "Privacy Shutter: ON"
            self.error_message = None
        else:
            self.state = "off"
            self.label = "Privacy Shutter: OFF"
            self.error_message = None
        return True


@dataclass
class ShellModel:
    """Plain state model holding clock, status, notifications, and privacy shutter."""

    clock_fn: Callable[[], datetime] | None = None
    clock_tile: ClockTile = field(default_factory=ClockTile)
    clock_time: str = ""
    clock_date: str = ""
    nova_status: str = "Ready"
    nova_message: str = "NOVA Core running"

    max_notifications: int = 20
    ram_notifications: list[ShellNotification] = field(default_factory=list)
    notifications: list[DemandEntry] = field(default_factory=list)

    shutter_tile: ShutterTile = field(default_factory=ShutterTile)
    camera_tile: CameraTile = field(default_factory=CameraTile)
    microphone_tile: MicrophoneTile = field(default_factory=MicrophoneTile)

    demand_store: DemandStore | None = None
    broker: capabilities.CapabilityBroker | None = None
    owner_confirmed_camera: bool = False
    catalogue_override: dict[str, capabilities.Capability] | None = None

    def __post_init__(self) -> None:
        if self.clock_fn is not None:
            self.clock_tile.clock_fn = self.clock_fn
        self.update_clock()
        self.refresh_state()

    def get_sensory_tiles(self) -> list[SensoryTile]:
        """Return all sensory tiles that could show camera or microphone state."""
        return [self.camera_tile, self.microphone_tile]

    def update_clock(self, now: datetime | None = None) -> str:
        """Update clock display strings from current UTC / local time or injected clock."""
        self.clock_tile.update(now)
        self.clock_time = self.clock_tile.time_str
        self.clock_date = self.clock_tile.date_str
        return self.clock_time

    def set_nova_status(self, status: str, message: str = "") -> None:
        self.nova_status = status.strip()
        self.nova_message = message.strip()

    def add_notification(
        self,
        title: str,
        message: str,
        category: str = "info",
        timestamp: str | None = None,
    ) -> ShellNotification:
        """Add an in-memory notification in RAM. Newest first, capped by max_notifications."""
        ts = timestamp or self.clock_tile.time_str or datetime.now(UTC).strftime("%H:%M:%S UTC")
        entry = ShellNotification(
            title=title.strip(),
            message=message.strip(),
            timestamp=ts,
            category=category,
        )
        self.ram_notifications.insert(0, entry)
        if len(self.ram_notifications) > self.max_notifications:
            self.ram_notifications = self.ram_notifications[: self.max_notifications]
        return entry

    def refresh_notifications(self) -> list[DemandEntry]:
        if self.demand_store is not None:
            try:
                self.notifications = self.demand_store.list(status="open")
            except Exception:
                self.notifications = []
        else:
            self.notifications = []
        return self.notifications

    def refresh_state(self) -> None:
        """Refresh all dynamic model components in every frame.

        Enforces project invariant:
        When the privacy shutter is closed (shutter_tile.state == 'off'), every tile
        that could show camera or microphone state must say so in every frame.
        """
        self.update_clock()
        self.refresh_notifications()

        # Enforce capability constraint on privacy shutter tile
        self.shutter_tile.update_from_capability(
            broker=self.broker,
            owner_confirmed=self.owner_confirmed_camera,
            catalogue=self.catalogue_override,
        )

        # Invariant: Privacy shutter coverage across sensory tiles
        is_closed = self.shutter_tile.state == "off"
        for tile in self.get_sensory_tiles():
            tile.update(is_shutter_closed=is_closed, is_streaming=(not is_closed))

    def toggle_privacy_shutter(self) -> bool:
        result = self.shutter_tile.request_toggle(
            broker=self.broker,
            owner_confirmed=self.owner_confirmed_camera,
            catalogue=self.catalogue_override,
        )
        self.refresh_state()
        return result
