"""Unit tests for NOVA Thrift Resource Probe and Governor (work order A3.14, A3.15, roadmap E3).

Verifies:
1. Battery state and capacity parsed accurately from Linux sysfs power_supply class.
2. Thermal zones and temperatures parsed from Linux sysfs thermal class (millidegrees to degrees).
3. Fail-safe UNKNOWN returned when sysfs entries are absent, unreadable, or on non-Linux platform.
4. Zero guessed numbers: unknown values strictly represented as UNKNOWN / None.
5. Real /sys probe executed safely without uncaught exceptions.
6. Governor 4 properties verified across entire input grid:
   - Property 1: An UNKNOWN probe never returns "large".
   - Property 2: "interactive" jobs are never refused.
   - Property 3: When battery is below threshold (< 20%) and not charging, "heavy" job is never "run".
   - Property 4: Going from cooler to hotter state never raises the tier.
7. Pure advisory contract: zero process or network module imports in thrift.py.
"""

from __future__ import annotations

import ast
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import novacore.thrift as thrift
from novacore.thrift import (
    UNKNOWN,
    BatteryState,
    GovernorDecision,
    ResourceProbe,
    ThermalState,
    govern,
    probe_resources,
)


def make_probe(
    battery_status: str = "Discharging",
    capacity_pct: int | None = 80,
    is_charging: bool | None = False,
    battery_present: bool = True,
    temp_celsius: float | None = 40.0,
    is_unknown: bool = False,
) -> ResourceProbe:
    return ResourceProbe(
        platform="linux",
        is_linux=True,
        battery=BatteryState(
            present=battery_present,
            status=battery_status,
            capacity_pct=capacity_pct,
            is_charging=is_charging,
        ),
        thermal=ThermalState(
            zones={"thermal_zone0": temp_celsius} if temp_celsius is not None else {},
            max_temp_celsius=temp_celsius,
        ),
        is_unknown=is_unknown,
        summary="Test probe",
    )


class TestThriftProbe(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="nova_test_sysfs_"))
        self.sysfs_root = self.temp_dir / "sys"
        self.power_supply_dir = self.sysfs_root / "class" / "power_supply"
        self.thermal_dir = self.sysfs_root / "class" / "thermal"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_battery_discharging_parsed_from_sysfs(self) -> None:
        """Battery state, capacity, and discharging status correctly parsed."""
        bat0 = self.power_supply_dir / "BAT0"
        bat0.mkdir(parents=True)
        (bat0 / "type").write_text("Battery\n", encoding="utf-8")
        (bat0 / "status").write_text("Discharging\n", encoding="utf-8")
        (bat0 / "capacity").write_text("78\n", encoding="utf-8")

        probe = probe_resources(sysfs_root=self.sysfs_root)
        self.assertTrue(probe.is_linux)
        self.assertTrue(probe.battery.present)
        self.assertEqual(probe.battery.status, "Discharging")
        self.assertEqual(probe.battery.capacity_pct, 78)
        self.assertFalse(probe.battery.is_charging)

    def test_battery_charging_parsed_from_sysfs(self) -> None:
        """Battery charging state recognized correctly."""
        bat0 = self.power_supply_dir / "BAT0"
        bat0.mkdir(parents=True)
        (bat0 / "type").write_text("Battery\n", encoding="utf-8")
        (bat0 / "status").write_text("Charging\n", encoding="utf-8")
        (bat0 / "capacity").write_text("42\n", encoding="utf-8")

        probe = probe_resources(sysfs_root=self.sysfs_root)
        self.assertTrue(probe.battery.present)
        self.assertEqual(probe.battery.status, "Charging")
        self.assertEqual(probe.battery.capacity_pct, 42)
        self.assertTrue(probe.battery.is_charging)

    def test_non_battery_power_supply_ignored(self) -> None:
        """AC / Mains power supplies are not reported as battery."""
        ac = self.power_supply_dir / "ACAD"
        ac.mkdir(parents=True)
        (ac / "type").write_text("Mains\n", encoding="utf-8")
        (ac / "online").write_text("1\n", encoding="utf-8")

        probe = probe_resources(sysfs_root=self.sysfs_root)
        self.assertFalse(probe.battery.present)
        self.assertEqual(probe.battery.status, UNKNOWN)
        self.assertIsNone(probe.battery.capacity_pct)

    def test_thermal_zones_millidegrees_to_degrees(self) -> None:
        """Thermal zone millidegrees must be converted to degrees Celsius (dividing by 1000)."""
        tz0 = self.thermal_dir / "thermal_zone0"
        tz0.mkdir(parents=True)
        (tz0 / "temp").write_text("45000\n", encoding="utf-8")

        tz1 = self.thermal_dir / "thermal_zone1"
        tz1.mkdir(parents=True)
        (tz1 / "temp").write_text("62500\n", encoding="utf-8")

        probe = probe_resources(sysfs_root=self.sysfs_root)
        self.assertIn("thermal_zone0", probe.thermal.zones)
        self.assertIn("thermal_zone1", probe.thermal.zones)

        # Invariant: 45000 millidegrees must equal 45.0 degrees Celsius
        self.assertAlmostEqual(probe.thermal.zones["thermal_zone0"], 45.0, places=2)
        self.assertAlmostEqual(probe.thermal.zones["thermal_zone1"], 62.5, places=2)
        self.assertAlmostEqual(probe.thermal.max_temp_celsius, 62.5, places=2)

    def test_absent_sysfs_entries_return_unknown_without_guessing(self) -> None:
        """When sysfs directories do not exist, returns UNKNOWN without guessing values."""
        empty_root = self.temp_dir / "empty_sys"
        empty_root.mkdir()

        probe = probe_resources(sysfs_root=empty_root)
        self.assertTrue(probe.is_unknown)
        self.assertFalse(probe.battery.present)
        self.assertEqual(probe.battery.status, UNKNOWN)
        self.assertIsNone(probe.battery.capacity_pct)
        self.assertEqual(len(probe.thermal.zones), 0)
        self.assertIsNone(probe.thermal.max_temp_celsius)

    def test_corrupted_sysfs_files_handled_gracefully(self) -> None:
        """Non-integer sysfs file content must not crash or invent numbers."""
        tz0 = self.thermal_dir / "thermal_zone0"
        tz0.mkdir(parents=True)
        (tz0 / "temp").write_text("not_a_number\n", encoding="utf-8")

        bat0 = self.power_supply_dir / "BAT0"
        bat0.mkdir(parents=True)
        (bat0 / "type").write_text("Battery\n", encoding="utf-8")
        (bat0 / "status").write_text("Discharging\n", encoding="utf-8")
        (bat0 / "capacity").write_text("invalid_pct\n", encoding="utf-8")

        probe = probe_resources(sysfs_root=self.sysfs_root)
        self.assertTrue(probe.battery.present)
        self.assertEqual(probe.battery.status, "Discharging")
        self.assertIsNone(probe.battery.capacity_pct)
        self.assertIsNone(probe.thermal.max_temp_celsius)

    def test_non_linux_platform_defaults_to_unknown(self) -> None:
        """On non-Linux platform without sysfs_root, probe returns UNKNOWN fail-safe."""
        if sys.platform != "linux":
            probe = probe_resources()
            self.assertTrue(probe.is_unknown)
            self.assertFalse(probe.is_linux)
            self.assertEqual(probe.battery.status, UNKNOWN)
            self.assertIsNone(probe.battery.capacity_pct)
            self.assertIsNone(probe.thermal.max_temp_celsius)

    def test_real_sys_probe_execution(self) -> None:
        """Live probe execution on current host runs without uncaught exceptions."""
        probe = probe_resources()
        self.assertIsInstance(probe, ResourceProbe)
        self.assertIsInstance(probe.summary, str)
        self.assertGreater(len(probe.summary), 0)


class TestThriftGovernor(unittest.TestCase):
    TIER_RANK = {"small": 0, "medium": 1, "large": 2}

    def _generate_grid_probes(self) -> list[tuple[str, str, ResourceProbe]]:
        """Generate probes spanning all battery and thermal categories."""
        probes = []
        battery_configs = [
            ("CHARGING", {"is_charging": True, "battery_status": "Charging", "capacity_pct": 80, "battery_present": True}),
            ("BATT_HIGH", {"is_charging": False, "battery_status": "Discharging", "capacity_pct": 75, "battery_present": True}),
            ("BATT_MED", {"is_charging": False, "battery_status": "Discharging", "capacity_pct": 35, "battery_present": True}),
            ("BATT_LOW", {"is_charging": False, "battery_status": "Discharging", "capacity_pct": 12, "battery_present": True}),
            ("BATT_UNKNOWN", {"is_charging": None, "battery_status": UNKNOWN, "capacity_pct": None, "battery_present": False}),
        ]
        thermal_configs = [
            ("COOL", 42.0),
            ("WARM", 65.0),
            ("HOT", 82.0),
            ("THERMAL_UNKNOWN", None),
        ]
        for b_name, b_kw in battery_configs:
            for t_name, t_val in thermal_configs:
                is_un = (b_name == "BATT_UNKNOWN" and t_name == "THERMAL_UNKNOWN")
                p = make_probe(temp_celsius=t_val, is_unknown=is_un, **b_kw)
                probes.append((b_name, t_name, p))
        return probes

    def test_full_input_grid_coverage(self) -> None:
        """Every cell in the 5x4x3 grid has a valid verdict and tier."""
        grid = self._generate_grid_probes()
        job_classes = ["interactive", "heavy", "nightly"]

        count = 0
        for b_name, t_name, probe in grid:
            for job in job_classes:
                decision = govern(probe, job)
                self.assertIn(decision.verdict, ("run", "defer", "refuse"))
                self.assertIn(decision.tier, ("small", "medium", "large"))
                self.assertIsInstance(decision.reason, str)
                count += 1
        self.assertEqual(count, 60, "Must evaluate all 60 combinations in input grid")

    def test_property_1_unknown_probe_never_returns_large(self) -> None:
        """Property 1: An UNKNOWN probe never returns 'large'."""
        grid = self._generate_grid_probes()
        job_classes = ["interactive", "heavy", "nightly"]

        for b_name, t_name, probe in grid:
            if probe.is_unknown or b_name == "BATT_UNKNOWN" or t_name == "THERMAL_UNKNOWN":
                for job in job_classes:
                    decision = govern(probe, job)
                    self.assertNotEqual(
                        decision.tier,
                        "large",
                        f"Property 1 violation: UNKNOWN probe ({b_name}, {t_name}, {job}) returned 'large'!",
                    )

    def test_property_2_interactive_jobs_never_refused(self) -> None:
        """Property 2: 'interactive' jobs are never refused across entire grid."""
        grid = self._generate_grid_probes()
        for b_name, t_name, probe in grid:
            decision = govern(probe, "interactive")
            self.assertNotEqual(
                decision.verdict,
                "refuse",
                f"Property 2 violation: interactive job was refused for ({b_name}, {t_name})!",
            )

    def test_property_3_heavy_job_never_run_when_battery_low_and_not_charging(self) -> None:
        """Property 3: When battery is below threshold (< 20%) and not charging, heavy is never run."""
        grid = self._generate_grid_probes()
        for b_name, t_name, probe in grid:
            if b_name == "BATT_LOW":
                decision = govern(probe, "heavy")
                self.assertNotEqual(
                    decision.verdict,
                    "run",
                    f"Property 3 violation: heavy job was given 'run' on BATT_LOW ({t_name})!",
                )

    def test_property_4_cooler_to_hotter_never_raises_tier(self) -> None:
        """Property 4: Going from a cooler state to a hotter one never raises the tier."""
        battery_configs = [
            ("CHARGING", {"is_charging": True, "battery_status": "Charging", "capacity_pct": 80, "battery_present": True}),
            ("BATT_HIGH", {"is_charging": False, "battery_status": "Discharging", "capacity_pct": 75, "battery_present": True}),
            ("BATT_MED", {"is_charging": False, "battery_status": "Discharging", "capacity_pct": 35, "battery_present": True}),
            ("BATT_LOW", {"is_charging": False, "battery_status": "Discharging", "capacity_pct": 12, "battery_present": True}),
            ("BATT_UNKNOWN", {"is_charging": None, "battery_status": UNKNOWN, "capacity_pct": None, "battery_present": False}),
        ]
        job_classes = ["interactive", "heavy", "nightly"]

        for b_name, b_kw in battery_configs:
            for job in job_classes:
                probe_cool = make_probe(temp_celsius=40.0, **b_kw)
                probe_warm = make_probe(temp_celsius=65.0, **b_kw)
                probe_hot = make_probe(temp_celsius=85.0, **b_kw)

                rank_cool = self.TIER_RANK[govern(probe_cool, job).tier]
                rank_warm = self.TIER_RANK[govern(probe_warm, job).tier]
                rank_hot = self.TIER_RANK[govern(probe_hot, job).tier]

                self.assertGreaterEqual(
                    rank_cool,
                    rank_warm,
                    f"Property 4 violation: tier raised from COOL to WARM for ({b_name}, {job})",
                )
                self.assertGreaterEqual(
                    rank_warm,
                    rank_hot,
                    f"Property 4 violation: tier raised from WARM to HOT for ({b_name}, {job})",
                )

    def test_governor_imports_no_process_or_network_modules(self) -> None:
        """The governor only advises: starts no jobs, imports no process or network modules."""
        thrift_source_path = Path(thrift.__file__)
        tree = ast.parse(thrift_source_path.read_text(encoding="utf-8"))

        forbidden_prefixes = (
            "subprocess",
            "os.system",
            "pty",
            "multiprocessing",
            "threading",
            "socket",
            "http",
            "urllib",
            "xmlrpc",
            "ftplib",
            "asyncio",
            "smtplib",
        )

        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_names.add(node.module)

        for name in imported_names:
            for forbidden in forbidden_prefixes:
                self.assertFalse(
                    name == forbidden or name.startswith(forbidden + "."),
                    f"Forbidden module '{name}' imported in thrift.py!",
                )

    def test_invalid_job_class_raises_value_error(self) -> None:
        """Passing an unrecognized job class raises ValueError fail-closed."""
        probe = make_probe()
        with self.assertRaises(ValueError):
            govern(probe, "unrecognized_batch_type")


if __name__ == "__main__":
    unittest.main()
