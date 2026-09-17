"""Header widget — hostname, OS, uptime, clock."""

from __future__ import annotations

import datetime

from rich.text import Text
from textual.widgets import Static

from sentinella.core.models import SystemSnapshot
from sentinella.tui.theme import palette_for


class HeaderWidget(Static):
    """Top bar showing system identity and time."""

    def update_data(self, snap: SystemSnapshot) -> None:
        si = snap.system_info
        uptime = str(datetime.timedelta(seconds=int(si.uptime_seconds)))
        now = datetime.datetime.now().strftime("%H:%M:%S")

        palette = palette_for(self)
        text = Text()
        # Was `bold bright_white`, which sat at 1.44:1 on the light theme's
        # #D8D8D8 surface — effectively invisible.
        text.append("  🐦‍⬛ SENTINELLA", style=f"bold {palette.value}")
        text.append("  │  ", style=palette.muted)
        text.append(f"{si.hostname}", style=f"bold {palette.title}")
        text.append(f"  │  {si.os_name} {si.os_version} ({si.architecture})", style=palette.value)
        text.append(f"  │  ⏱ {uptime}", style=palette.value)
        text.append(f"  │  🕐 {now}", style=f"bold {palette.value}")

        self.update(text)
