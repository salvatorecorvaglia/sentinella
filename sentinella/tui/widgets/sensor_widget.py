"""Sensor widget — temperatures, fans, battery, users."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from sentinella.core.limits import DASHBOARD_LIMITS
from sentinella.core.models import SystemSnapshot
from sentinella.tui.theme import palette_for
from sentinella.tui.widgets._common import more_row, section_header


class SensorWidget(Static):
    """Sensors panel — temperatures, fans, battery, users."""

    def update_data(self, snap: SystemSnapshot) -> None:
        sensors = snap.sensors
        palette = palette_for(self)
        text = Text()

        # Temperatures
        if sensors.temperatures:
            section_header(text, "Temps", palette)
            limit = DASHBOARD_LIMITS["temperatures"]
            for t in sensors.temperatures[:limit]:
                color = palette.for_temp(t.current, t.high, t.critical)
                text.append(f"  {t.label:<18} ")
                text.append(f"{t.current:.0f}°C", style=color)
                if t.high:
                    text.append(f" (high: {t.high:.0f}°C)", style=palette.muted)
                text.append("\n")
            more_row(text, len(sensors.temperatures) - limit, "temperatures", palette)

        # Fans
        if sensors.fans:
            section_header(text, "Fans", palette)
            limit = DASHBOARD_LIMITS["fans"]
            for f in sensors.fans[:limit]:
                text.append(f"  {f.label:<18} {f.current} RPM\n")
            more_row(text, len(sensors.fans) - limit, "fans", palette)

        # Battery
        if sensors.battery:
            bat = sensors.battery
            section_header(text, "Battery", palette)
            plugged = "⚡ Plugged" if bat.power_plugged else "🔋 Battery"
            # An unknown charge is not a flat battery — don't paint it red.
            if bat.percent is None:
                pct_str, color = "Unknown", palette.muted
            else:
                pct_str = f"{bat.percent:.0f}%"
                color = palette.good if bat.percent > 20 else palette.crit
            text.append(f"  {pct_str} {plugged}\n", style=color)

        # Users
        if snap.users:
            user_names = list({u.name for u in snap.users})
            limit = DASHBOARD_LIMITS["users"]
            section_header(text, "Users", palette)
            text.append(f"  {', '.join(user_names[:limit])}\n")
            more_row(text, len(user_names) - limit, "users", palette)

        if not text.plain.strip():
            text.append("  No sensor data\n", style=palette.muted)

        self.update(text)
