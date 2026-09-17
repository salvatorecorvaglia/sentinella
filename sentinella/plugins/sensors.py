"""Sensor monitoring plugin (temperatures, fans, battery)."""

from __future__ import annotations

import psutil

from sentinella.core.models import (
    BatteryInfo,
    FanReading,
    SensorMetrics,
    TemperatureReading,
)
from sentinella.plugins.base import MonitorPlugin


class SensorsPlugin(MonitorPlugin):
    name = "sensors"
    category = "sensors"

    def is_available(self) -> bool:
        # Sensors are partially available on most platforms; we always try.
        return True

    def collect(self) -> SensorMetrics:
        temperatures = self._collect_temperatures()
        fans = self._collect_fans()
        battery = self._collect_battery()
        return SensorMetrics(temperatures=temperatures, fans=fans, battery=battery)

    # ── private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _collect_temperatures() -> list[TemperatureReading]:
        readings: list[TemperatureReading] = []
        try:
            temps = psutil.sensors_temperatures()
            if not temps:
                return readings
            for group_name, entries in temps.items():
                for entry in entries:
                    label = entry.label or group_name
                    readings.append(
                        TemperatureReading(
                            label=label,
                            current=entry.current,
                            high=entry.high,
                            critical=entry.critical,
                        )
                    )
        except Exception:
            pass
        return readings

    @staticmethod
    def _collect_fans() -> list[FanReading]:
        readings: list[FanReading] = []
        try:
            fans = psutil.sensors_fans()
            if not fans:
                return readings
            for group_name, entries in fans.items():
                for entry in entries:
                    label = entry.label or group_name
                    readings.append(FanReading(label=label, current=entry.current))
        except Exception:
            pass
        return readings

    @staticmethod
    def _collect_battery() -> BatteryInfo | None:
        try:
            bat = psutil.sensors_battery()
            if bat is None:
                return None
            unknown = getattr(psutil, "POWER_TIME_UNKNOWN", -1)
            unlimited = getattr(psutil, "POWER_TIME_UNLIMITED", -2)
            secs_left = bat.secsleft
            is_invalid = secs_left in (unlimited, unknown)
            if is_invalid or (secs_left is not None and secs_left < 0):
                secs_left = None
            return BatteryInfo(
                percent=bat.percent,
                power_plugged=bat.power_plugged,
                secs_left=secs_left,
            )
        except Exception:
            return None
