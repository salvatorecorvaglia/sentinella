"""Disk widget — partition usage bars and I/O stats."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from sentinella.core.limits import DASHBOARD_LIMITS
from sentinella.core.models import SystemSnapshot
from sentinella.core.utils import human_bytes, truncate_path
from sentinella.tui.theme import palette_for
from sentinella.tui.widgets._common import more_row, section_header, themed_bar

_MOUNT_WIDTH = 14


class DiskWidget(Static):
    """Disk panel with per-partition usage and I/O."""

    def update_data(self, snap: SystemSnapshot) -> None:
        disk = snap.disk
        palette = palette_for(self)
        text = Text()
        section_header(text, "Disk", palette)

        limit = DASHBOARD_LIMITS["partitions"]
        for dp in disk.partitions[:limit]:
            # Keep the tail: sibling volumes differ only at the end of the path.
            mount = truncate_path(dp.mountpoint, _MOUNT_WIDTH)
            text.append(f"  {mount:<{_MOUNT_WIDTH}} ")
            text.append_text(themed_bar(dp.percent, 12, palette))
            text.append(f"  {human_bytes(dp.used)}/{human_bytes(dp.total)}\n", style=palette.muted)
        more_row(text, len(disk.partitions) - limit, "partitions", palette)

        # I/O
        io = disk.io
        text.append(
            f"  I/O  Read: {human_bytes(io.read_bytes)}  Write: {human_bytes(io.write_bytes)}\n",
            style=palette.muted,
        )

        self.update(text)
