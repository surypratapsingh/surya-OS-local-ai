"""NOVA Shell State Model (work order A3.10, roadmap E5).

Holds plain state for the NOVA Shell GUI:
1. Clock: formatted time and UTC representation.
2. NOVA Status: operational readiness and state indicators.
3. Demand Notifications: open items from the DemandStore.
4. Privacy Shutter Tile: physical/logical camera privacy shutter state.

Project Invariant:
- Whenever the camera capability is not granted, as `capabilities.py` decides,
  the shutter tile MUST show off.
- Plant test: A tile that ignores the capability must fail.
"""

from __future__ import annotations

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
        """Attempt to toggle shutter state.

        If camera capability is not granted, toggle to 'on' is refused and state remains 'off'.
        """
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

        # If granted, toggle
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
    clock_time: str = ""
    clock_date: str = ""
    nova_status: str = "Ready"
    nova_message: str = "NOVA Core running"
    notifications: list[DemandEntry] = field(default_factory=list)
    shutter_tile: ShutterTile = field(default_factory=ShutterTile)

    demand_store: DemandStore | None = None
    broker: capabilities.CapabilityBroker | None = None
    owner_confirmed_camera: bool = False
    catalogue_override: dict[str, capabilities.Capability] | None = None

    def __post_init__(self) -> None:
        self.update_clock()
        self.refresh_state()

    def update_clock(self, now: datetime | None = None) -> str:
        """Update clock display strings from current UTC / local time."""
        dt = now or datetime.now(UTC)
        self.clock_time = dt.strftime("%H:%M:%S UTC")
        self.clock_date = dt.strftime("%Y-%m-%d")
        return self.clock_time

    def set_nova_status(self, status: str, message: str = "") -> None:
        """Update NOVA operational status."""
        self.nova_status = status.strip()
        self.nova_message = message.strip()

    def refresh_notifications(self) -> list[DemandEntry]:
        """Fetch open demands from DemandStore if available."""
        if self.demand_store is not None:
            try:
                self.notifications = self.demand_store.list(status="open")
            except Exception:
                self.notifications = []
        else:
            self.notifications = []
        return self.notifications

    def refresh_state(self) -> None:
        """Refresh all dynamic model components."""
        self.update_clock()
        self.refresh_notifications()
        # Enforce capability constraint on privacy shutter tile
        self.shutter_tile.update_from_capability(
            broker=self.broker,
            owner_confirmed=self.owner_confirmed_camera,
            catalogue=self.catalogue_override,
        )

    def toggle_privacy_shutter(self) -> bool:
        """Request toggle on privacy shutter tile."""
        result = self.shutter_tile.request_toggle(
            broker=self.broker,
            owner_confirmed=self.owner_confirmed_camera,
            catalogue=self.catalogue_override,
        )
        return result
