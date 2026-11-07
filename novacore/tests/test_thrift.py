"""Unit tests for NOVA Thrift Resource Probe (work order A3.14, roadmap E3).

Verifies:
1. Battery state and capacity parsed accurately from Linux sysfs power_supply class.
2. Thermal zones and temperatures parsed from Linux sysfs thermal class (millidegrees to degrees).
3. Fail-safe UNKNOWN returned when sysfs entries are absent, unreadable, or on non-Linux platform.
4. Zero guessed numbers: unknown values strictly represented as UNKNOWN / None.
5. Real /sys probe executed safely without uncaught exceptions.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from novacore.thrift import (
    UNKNOWN,
    BatteryState,
    ResourceProbe,
    ThermalState,
    probe_resources,
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


if __name__ == "__main__":
    unittest.main()
