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

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # (timestamp, read_bytes, write_bytes) of the previous snapshot, and
        # the rates last derived from it, for a snapshot delivered twice.
        self._prev: tuple[float, int, int] | None = None
        self._rates: tuple[float, float] | None = None

    def _io_rates(self, snap: SystemSnapshot) -> tuple[float, float] | None:
        """Bytes/s read and written since the previous snapshot, or ``None``.

        The counters are totals since boot, which is not what a live panel
        should show beside usage bars. Like the network rates, this divides by
        the snapshot times, and a repeated snapshot (a forced refresh inside
        the cache TTL) keeps the previous rates instead of reading as idle.
        """
        io = snap.disk.io
        now = (snap.timestamp, io.read_bytes, io.write_bytes)
        if self._prev is not None:
            dt = now[0] - self._prev[0]
            if dt <= 0:
                return self._rates
            # A negative delta is a counter reset, not a negative rate.
            self._rates = (
                max(0, now[1] - self._prev[1]) / dt,
                max(0, now[2] - self._prev[2]) / dt,
            )
        self._prev = now
        return self._rates

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

        rates = self._io_rates(snap)
        if rates is None:
            read, write = "—", "—"  # one sample is not a rate
        else:
            read, write = f"{human_bytes(rates[0])}/s", f"{human_bytes(rates[1])}/s"
        text.append(f"  I/O  Read: {read}  Write: {write}\n", style=palette.muted)

        self.update(text)
