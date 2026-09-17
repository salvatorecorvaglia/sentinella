"""Container widget — Docker/LXC container list."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from sentinella.core.limits import DASHBOARD_LIMITS
from sentinella.core.models import SystemSnapshot
from sentinella.tui.theme import palette_for
from sentinella.tui.widgets._common import more_row, section_header


class ContainerWidget(Static):
    """Container panel showing Docker and LXC containers."""

    def update_data(self, snap: SystemSnapshot) -> None:
        containers = snap.containers

        # Auto-hide if neither runtime is available
        if not containers.docker_available and not containers.lxc_available:
            self.display = False
            try:
                self.app.query_one("#dashboard").add_class("no-containers")
            except Exception:
                pass
            return
        else:
            self.display = True
            try:
                self.app.query_one("#dashboard").remove_class("no-containers")
            except Exception:
                pass

        palette = palette_for(self)
        text = Text()
        section_header(text, "Containers", palette)

        if not containers.containers:
            text.append("  No containers detected\n", style=palette.muted)
            self.update(text)
            return

        running = sum(1 for c in containers.containers if c.is_running)
        text.append(f"  {running}/{len(containers.containers)} running\n\n")

        limit = DASHBOARD_LIMITS["containers"]
        for c in containers.containers[:limit]:
            status_color = palette.good if c.is_running else palette.warn
            text.append(f"  [{c.runtime}] ", style=palette.muted)
            text.append(f"{c.name[:20]:<22}", style="bold")
            text.append(f"{c.status:<12}", style=status_color)
            text.append(f"{c.image[:25]}\n", style=palette.muted)
        more_row(text, len(containers.containers) - limit, "containers", palette)

        self.update(text)
