"""NOVA Thrift Resource Probe and Governor (work order A3.14, roadmap E3).

Provides battery and thermal state detection from Linux sysfs, with strict
fail-safe behavior returning UNKNOWN on non-Linux platforms or when sysfs
entries are absent or unreadable. Never returns guessed numbers.

Linux Documentation and Source References:
- Battery/Power Supply sysfs:
  Documentation/power/power_supply_class.rst
  (Linux kernel source tree, not a file in this repository).
  drivers/power/supply/power_supply_sysfs.c
  (Linux kernel source tree, not a file in this repository).
  Formats read:
    /sys/class/power_supply/<supply>/type ('Battery')
    /sys/class/power_supply/<supply>/status ('Charging', 'Discharging', 'Not charging', 'Full', 'Unknown')
    /sys/class/power_supply/<supply>/capacity (integer percentage 0-100)

- Thermal sysfs:
  Documentation/thermal/sysfs-api.rst
  (Linux kernel source tree, not a file in this repository).
  drivers/thermal/thermal_sysfs.c
  (Linux kernel source tree, not a file in this repository).
  Formats read:
    /sys/class/thermal/thermal_zone<X>/temp (integer millidegrees Celsius, divided by 1000.0)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BatteryState:
    """Battery status derived from Linux sysfs power_supply class."""

    present: bool
    status: str  # "Charging", "Discharging", "Not charging", "Full", "UNKNOWN", etc.
    capacity_pct: int | None  # integer 0-100, or None if UNKNOWN / absent
    is_charging: bool | None  # True if Charging/Full, False if Discharging/Not charging, None if UNKNOWN


@dataclass(frozen=True)
class ThermalState:
    """Thermal status derived from Linux sysfs thermal class."""

    zones: dict[str, float]  # zone_name -> temperature in degrees Celsius
    max_temp_celsius: float | None  # highest temperature, or None if UNKNOWN


@dataclass(frozen=True)
class ResourceProbe:
    """Full host resource probe result."""

    platform: str
    is_linux: bool
    battery: BatteryState
    thermal: ThermalState
    is_unknown: bool
    summary: str


def _read_file_stripped(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def probe_resources(sysfs_root: Path | None = None) -> ResourceProbe:
    """Probe host battery and thermal resources via Linux sysfs.

    Args:
        sysfs_root: Optional root directory override pointing to /sys.
                    If None, uses Path('/sys') when running on Linux.

    Returns:
        ResourceProbe containing battery and thermal states.
        If running on a non-Linux platform (without sysfs_root override)
        or if sysfs entries are absent/unreadable, returns UNKNOWN states.
        Never returns guessed numbers.
    """
    is_linux_platform = sys.platform == "linux"

    # If no sysfs_root is supplied and platform is not Linux, fail-safe to UNKNOWN
    if sysfs_root is None and not is_linux_platform:
        return ResourceProbe(
            platform=sys.platform,
            is_linux=False,
            battery=BatteryState(present=False, status=UNKNOWN, capacity_pct=None, is_charging=None),
            thermal=ThermalState(zones={}, max_temp_celsius=None),
            is_unknown=True,
            summary=f"Platform '{sys.platform}' is non-Linux; resource probe UNKNOWN",
        )

    root = sysfs_root if sysfs_root is not None else Path("/sys")

    # 1. Probe Battery via /sys/class/power_supply/*/
    # Linux kernel source: drivers/power/supply/power_supply_sysfs.c
    # (Linux kernel source tree, not a file in this repository).
    power_supply_dir = root / "class" / "power_supply"
    battery_present = False
    battery_status = UNKNOWN
    battery_capacity: int | None = None
    battery_is_charging: bool | None = None

    if power_supply_dir.is_dir():
        try:
            for entry in sorted(power_supply_dir.iterdir()):
                if not entry.is_dir():
                    continue
                type_file = entry / "type"
                supply_type = _read_file_stripped(type_file)
                if supply_type != "Battery":
                    continue

                battery_present = True
                status_raw = _read_file_stripped(entry / "status")
                if status_raw:
                    battery_status = status_raw
                    if status_raw.lower() in ("charging", "full"):
                        battery_is_charging = True
                    elif status_raw.lower() in ("discharging", "not charging"):
                        battery_is_charging = False
                    else:
                        battery_is_charging = None

                cap_raw = _read_file_stripped(entry / "capacity")
                if cap_raw:
                    try:
                        cap_val = int(cap_raw)
                        if 0 <= cap_val <= 100:
                            battery_capacity = cap_val
                    except ValueError:
                        pass
                break  # Read primary battery
        except Exception:
            battery_present = False
            battery_status = UNKNOWN
            battery_capacity = None
            battery_is_charging = None

    # 2. Probe Thermal via /sys/class/thermal/thermal_zone*/temp
    # Linux kernel source: drivers/thermal/thermal_sysfs.c
    # (Linux kernel source tree, not a file in this repository).
    # Sysfs temp is expressed in millidegrees Celsius.
    thermal_dir = root / "class" / "thermal"
    zones: dict[str, float] = {}
    max_temp: float | None = None

    if thermal_dir.is_dir():
        try:
            for entry in sorted(thermal_dir.iterdir()):
                if not entry.is_dir() or not entry.name.startswith("thermal_zone"):
                    continue
                temp_file = entry / "temp"
                temp_raw = _read_file_stripped(temp_file)
                if temp_raw:
                    try:
                        millidegrees = int(temp_raw)
                        temp_celsius = millidegrees / 1000.0
                        zones[entry.name] = temp_celsius
                    except ValueError:
                        continue
            if zones:
                max_temp = max(zones.values())
        except Exception:
            zones = {}
            max_temp = None

    # If neither battery nor thermal could be read, probe is marked UNKNOWN
    is_unknown = battery_status == UNKNOWN and max_temp is None

    summary_parts = []
    if battery_present and battery_capacity is not None:
        summary_parts.append(f"Battery {battery_capacity}% ({battery_status})")
    elif battery_present:
        summary_parts.append(f"Battery ({battery_status})")
    else:
        summary_parts.append("Battery UNKNOWN/absent")

    if max_temp is not None:
        summary_parts.append(f"Thermal max {max_temp:.1f}°C ({len(zones)} zones)")
    else:
        summary_parts.append("Thermal UNKNOWN")

    return ResourceProbe(
        platform="linux" if (sysfs_root is not None or is_linux_platform) else sys.platform,
        is_linux=True if (sysfs_root is not None or is_linux_platform) else False,
        battery=BatteryState(
            present=battery_present,
            status=battery_status,
            capacity_pct=battery_capacity,
            is_charging=battery_is_charging,
        ),
        thermal=ThermalState(
            zones=zones,
            max_temp_celsius=max_temp,
        ),
        is_unknown=is_unknown,
        summary=", ".join(summary_parts),
    )
