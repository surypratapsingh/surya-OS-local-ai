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

from collections import deque
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


@dataclass(frozen=True)
class GovernorDecision:
    """Decision returned by the Thrift Governor. Pure advisory."""

    verdict: str  # "run", "defer", "refuse"
    tier: str  # "small", "medium", "large"
    reason: str


# Discretized categories for deterministic rule table matching
BATT_CHARGING = "CHARGING"
BATT_HIGH = "BATT_HIGH"  # >= 50%
BATT_MED = "BATT_MED"  # 20% - 49%
BATT_LOW = "BATT_LOW"  # < 20%
BATT_UNKNOWN = "BATT_UNKNOWN"

THERMAL_COOL = "COOL"  # < 50.0°C
THERMAL_WARM = "WARM"  # 50.0°C - 74.9°C
THERMAL_HOT = "HOT"  # >= 75.0°C
THERMAL_UNKNOWN = "THERMAL_UNKNOWN"

# Single unified rule table: (battery_category, thermal_category, job_class) -> (verdict, tier, reason)
# Strict Invariants:
# 1. UNKNOWN probe never returns "large".
# 2. "interactive" jobs are never refused.
# 3. When battery is below threshold (< 20%) and not charging, "heavy" job is never "run".
# 4. Going from cooler to hotter state never raises the tier.
GOVERNOR_RULES: dict[tuple[str, str, str], tuple[str, str, str]] = {
    # --- INTERACTIVE JOBS (Never refused) ---
    (BATT_CHARGING, THERMAL_COOL, "interactive"): ("run", "large", "AC charging, optimal thermal: run full tier"),
    (BATT_CHARGING, THERMAL_WARM, "interactive"): ("run", "medium", "AC charging, elevated thermal: moderate tier"),
    (BATT_CHARGING, THERMAL_HOT, "interactive"): ("run", "small", "AC charging, high thermal: throttled tier"),
    (BATT_CHARGING, THERMAL_UNKNOWN, "interactive"): ("run", "medium", "AC charging, thermal unknown: safe tier"),

    (BATT_HIGH, THERMAL_COOL, "interactive"): ("run", "large", "Battery healthy, optimal thermal: run full tier"),
    (BATT_HIGH, THERMAL_WARM, "interactive"): ("run", "medium", "Battery healthy, elevated thermal: moderate tier"),
    (BATT_HIGH, THERMAL_HOT, "interactive"): ("run", "small", "Battery healthy, high thermal: throttled tier"),
    (BATT_HIGH, THERMAL_UNKNOWN, "interactive"): ("run", "medium", "Battery healthy, thermal unknown: safe tier"),

    (BATT_MED, THERMAL_COOL, "interactive"): ("run", "medium", "Battery moderate, cool thermal: moderate tier"),
    (BATT_MED, THERMAL_WARM, "interactive"): ("run", "small", "Battery moderate, warm thermal: small tier"),
    (BATT_MED, THERMAL_HOT, "interactive"): ("run", "small", "Battery moderate, hot thermal: throttled tier"),
    (BATT_MED, THERMAL_UNKNOWN, "interactive"): ("run", "small", "Battery moderate, thermal unknown: small tier"),

    (BATT_LOW, THERMAL_COOL, "interactive"): ("run", "small", "Battery low (<20%): interactive minimal tier"),
    (BATT_LOW, THERMAL_WARM, "interactive"): ("run", "small", "Battery low (<20%): interactive minimal tier"),
    (BATT_LOW, THERMAL_HOT, "interactive"): ("run", "small", "Battery low (<20%): interactive minimal tier"),
    (BATT_LOW, THERMAL_UNKNOWN, "interactive"): ("run", "small", "Battery low (<20%): interactive minimal tier"),

    (BATT_UNKNOWN, THERMAL_COOL, "interactive"): ("run", "medium", "Battery unknown, cool thermal: moderate tier"),
    (BATT_UNKNOWN, THERMAL_WARM, "interactive"): ("run", "small", "Battery unknown, warm thermal: small tier"),
    (BATT_UNKNOWN, THERMAL_HOT, "interactive"): ("run", "small", "Battery unknown, hot thermal: throttled tier"),
    (BATT_UNKNOWN, THERMAL_UNKNOWN, "interactive"): ("run", "small", "Probe unknown: fail-safe minimal tier"),

    # --- HEAVY JOBS (Never run when battery < 20% and not charging) ---
    (BATT_CHARGING, THERMAL_COOL, "heavy"): ("run", "large", "AC charging, cool: execute heavy task at full capacity"),
    (BATT_CHARGING, THERMAL_WARM, "heavy"): ("run", "medium", "AC charging, warm: execute heavy task at medium tier"),
    (BATT_CHARGING, THERMAL_HOT, "heavy"): ("defer", "small", "AC charging, hot: defer heavy task until cooled"),
    (BATT_CHARGING, THERMAL_UNKNOWN, "heavy"): ("run", "medium", "AC charging, thermal unknown: execute at medium tier"),

    (BATT_HIGH, THERMAL_COOL, "heavy"): ("run", "medium", "Battery healthy, cool: execute heavy task at medium tier"),
    (BATT_HIGH, THERMAL_WARM, "heavy"): ("run", "small", "Battery healthy, warm: execute heavy task at small tier"),
    (BATT_HIGH, THERMAL_HOT, "heavy"): ("defer", "small", "Battery healthy, hot: defer heavy task until cooled"),
    (BATT_HIGH, THERMAL_UNKNOWN, "heavy"): ("run", "small", "Battery healthy, thermal unknown: conservative tier"),

    (BATT_MED, THERMAL_COOL, "heavy"): ("defer", "small", "Battery moderate: defer heavy task to preserve battery"),
    (BATT_MED, THERMAL_WARM, "heavy"): ("defer", "small", "Battery moderate, warm: defer heavy task"),
    (BATT_MED, THERMAL_HOT, "heavy"): ("refuse", "small", "Battery moderate, hot: refuse heavy task"),
    (BATT_MED, THERMAL_UNKNOWN, "heavy"): ("defer", "small", "Battery moderate: defer heavy task"),

    (BATT_LOW, THERMAL_COOL, "heavy"): ("defer", "small", "Battery <20%: heavy task deferred until charging"),
    (BATT_LOW, THERMAL_WARM, "heavy"): ("refuse", "small", "Battery <20% and warm: refuse heavy task"),
    (BATT_LOW, THERMAL_HOT, "heavy"): ("refuse", "small", "Battery <20% and hot: refuse heavy task"),
    (BATT_LOW, THERMAL_UNKNOWN, "heavy"): ("refuse", "small", "Battery <20% and unknown thermal: refuse heavy task"),

    (BATT_UNKNOWN, THERMAL_COOL, "heavy"): ("run", "medium", "Battery unknown, cool: execute heavy task at medium tier"),
    (BATT_UNKNOWN, THERMAL_WARM, "heavy"): ("defer", "small", "Battery unknown, warm: defer heavy task"),
    (BATT_UNKNOWN, THERMAL_HOT, "heavy"): ("refuse", "small", "Battery unknown, hot: refuse heavy task"),
    (BATT_UNKNOWN, THERMAL_UNKNOWN, "heavy"): ("defer", "small", "Probe unknown: defer heavy task"),

    # --- NIGHTLY JOBS (Idle/batch operations) ---
    (BATT_CHARGING, THERMAL_COOL, "nightly"): ("run", "large", "AC charging, cool: execute nightly batch at full tier"),
    (BATT_CHARGING, THERMAL_WARM, "nightly"): ("run", "medium", "AC charging, warm: execute nightly batch at medium tier"),
    (BATT_CHARGING, THERMAL_HOT, "nightly"): ("defer", "small", "AC charging, hot: defer nightly batch"),
    (BATT_CHARGING, THERMAL_UNKNOWN, "nightly"): ("run", "medium", "AC charging, thermal unknown: moderate batch"),

    (BATT_HIGH, THERMAL_COOL, "nightly"): ("defer", "medium", "Discharging: defer nightly batch until on AC power"),
    (BATT_HIGH, THERMAL_WARM, "nightly"): ("defer", "small", "Discharging: defer nightly batch until on AC power"),
    (BATT_HIGH, THERMAL_HOT, "nightly"): ("defer", "small", "Discharging and hot: defer nightly batch"),
    (BATT_HIGH, THERMAL_UNKNOWN, "nightly"): ("defer", "small", "Discharging: defer nightly batch until on AC power"),

    (BATT_MED, THERMAL_COOL, "nightly"): ("defer", "small", "Battery moderate: defer nightly batch"),
    (BATT_MED, THERMAL_WARM, "nightly"): ("refuse", "small", "Battery moderate and warm: refuse nightly batch"),
    (BATT_MED, THERMAL_HOT, "nightly"): ("refuse", "small", "Battery moderate and hot: refuse nightly batch"),
    (BATT_MED, THERMAL_UNKNOWN, "nightly"): ("refuse", "small", "Battery moderate and thermal unknown: refuse nightly batch"),

    (BATT_LOW, THERMAL_COOL, "nightly"): ("refuse", "small", "Battery low (<20%): refuse nightly batch"),
    (BATT_LOW, THERMAL_WARM, "nightly"): ("refuse", "small", "Battery low (<20%): refuse nightly batch"),
    (BATT_LOW, THERMAL_HOT, "nightly"): ("refuse", "small", "Battery low (<20%): refuse nightly batch"),
    (BATT_LOW, THERMAL_UNKNOWN, "nightly"): ("refuse", "small", "Battery low (<20%): refuse nightly batch"),

    (BATT_UNKNOWN, THERMAL_COOL, "nightly"): ("defer", "medium", "Battery unknown: defer nightly batch"),
    (BATT_UNKNOWN, THERMAL_WARM, "nightly"): ("defer", "small", "Battery unknown: defer nightly batch"),
    (BATT_UNKNOWN, THERMAL_HOT, "nightly"): ("refuse", "small", "Battery unknown, hot: refuse nightly batch"),
    (BATT_UNKNOWN, THERMAL_UNKNOWN, "nightly"): ("defer", "small", "Probe unknown: defer nightly batch"),
}


def classify_battery(battery: BatteryState) -> str:
    """Classify battery state into discrete categories."""
    if not battery.present or battery.status == UNKNOWN:
        return BATT_UNKNOWN
    if battery.is_charging:
        return BATT_CHARGING
    if battery.capacity_pct is None:
        return BATT_UNKNOWN
    if battery.capacity_pct >= 50:
        return BATT_HIGH
    if battery.capacity_pct >= 20:
        return BATT_MED
    return BATT_LOW


def classify_thermal(thermal: ThermalState) -> str:
    """Classify thermal state into discrete categories."""
    if thermal.max_temp_celsius is None:
        return THERMAL_UNKNOWN
    t = thermal.max_temp_celsius
    if t < 50.0:
        return THERMAL_COOL
    if t < 75.0:
        return THERMAL_WARM
    return THERMAL_HOT


def govern(probe: ResourceProbe, job_class: str) -> GovernorDecision:
    """Pure advisory governor evaluating resource probe and job class.

    Args:
        probe: ResourceProbe from probe_resources().
        job_class: 'interactive', 'heavy', or 'nightly'.

    Returns:
        GovernorDecision(verdict, tier, reason)
        verdict in {"run", "defer", "refuse"}
        tier in {"small", "medium", "large"}

    Strict Invariants:
    1. An UNKNOWN probe never returns "large".
    2. "interactive" jobs are never refused.
    3. When battery is below threshold (< 20%) and not charging, "heavy" job is never "run".
    4. Going from cooler to hotter state never raises the tier.

    Pure function: Starts no jobs, initiates no I/O, imports no process or network modules.
    """
    valid_jobs = ("interactive", "heavy", "nightly")
    if job_class not in valid_jobs:
        raise ValueError(f"Unknown job_class '{job_class}'; expected one of {valid_jobs}")

    b_cat = classify_battery(probe.battery)
    t_cat = classify_thermal(probe.thermal)

    rule = GOVERNOR_RULES.get((b_cat, t_cat, job_class))
    if rule is None:
        verdict = "run" if job_class == "interactive" else "defer"
        return GovernorDecision(verdict=verdict, tier="small", reason="Fail-safe fallback")

    verdict, tier, reason = rule
    return GovernorDecision(verdict=verdict, tier=tier, reason=reason)



@dataclass(frozen=True)
class DeferredJob:
    """In-memory metadata for a deferred job.

    Invariant: Holds only job name and job class. Never contains owner content,
    prompts, documents, or personal data.
    """

    job_name: str
    job_class: str


class IdleDeferralQueue:
    """FIFO queue in RAM holding deferred jobs for idle/resource-ready execution.

    Invariants:
    - FIFO ordering: releases jobs strictly in the order they arrived.
    - Capacity limit: bounded by max_size.
    - Refusal on saturation: when full, refuses new jobs and reports explicit refusal message.
    - Privacy: stores only job_name and job_class; never owner content.
    """

    def __init__(self, max_size: int = 16) -> None:
        if max_size <= 0:
            raise ValueError(f"max_size must be positive; got {max_size}")
        self._max_size = max_size
        self._queue: deque[DeferredJob] = deque()

    @property
    def max_size(self) -> int:
        return self._max_size

    def __len__(self) -> int:
        return len(self._queue)

    def is_full(self) -> bool:
        return len(self._queue) >= self._max_size

    def is_empty(self) -> bool:
        return len(self._queue) == 0

    def enqueue(self, job_name: str, job_class: str) -> tuple[bool, str]:
        """Attempt to enqueue a deferred job.

        Returns:
            (True, "Enqueued: <job_name>") on success.
            (False, "Refused: queue full (<max_size>)") on capacity overflow.
        """
        valid_jobs = ("interactive", "heavy", "nightly")
        if job_class not in valid_jobs:
            raise ValueError(f"Unknown job_class '{job_class}'; expected one of {valid_jobs}")

        if len(self._queue) >= self._max_size:
            return False, f"Refused: deferral queue is full (max size {self._max_size})"

        self._queue.append(DeferredJob(job_name=job_name.strip(), job_class=job_class))
        return True, f"Enqueued: {job_name.strip()}"

    def release_runnable(self, probe: ResourceProbe) -> list[DeferredJob]:
        """Release runnable jobs in FIFO order when governor verdict evaluates to 'run'.

        Jobs that are still deferred or refused by current governor conditions remain
        in the queue in their original FIFO arrival order.
        """
        runnable: list[DeferredJob] = []
        remaining: deque[DeferredJob] = deque()

        while self._queue:
            job = self._queue.popleft()
            decision = govern(probe, job.job_class)
            if decision.verdict == "run":
                runnable.append(job)
            else:
                remaining.append(job)

        self._queue = remaining
        return runnable

    def list_jobs(self) -> list[DeferredJob]:
        """Return a snapshot list of currently queued jobs in FIFO order."""
        return list(self._queue)

    def clear(self) -> None:
        """Clear all queued jobs."""
        self._queue.clear()
