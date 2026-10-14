"""Network widget — per-interface TX/RX rates."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from sentinella.core.limits import DASHBOARD_LIMITS
from sentinella.core.models import SystemSnapshot
from sentinella.core.utils import human_bytes
from sentinella.tui.theme import palette_for
from sentinella.tui.widgets._common import more_row, section_header


class NetworkWidget(Static):
    """Network panel with interface traffic."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._prev_sent: dict[str, int] = {}
        self._prev_recv: dict[str, int] = {}
        self._prev_timestamp: float | None = None
        # Last computed (sent, recv) bytes/s per interface, re-shown when the
        # same snapshot arrives twice.
        self._rates: dict[str, tuple[float, float]] = {}

    def update_data(self, snap: SystemSnapshot) -> None:
        net = snap.network
        palette = palette_for(self)
        text = Text()
        section_header(text, "Network", palette)

        non_lo_interfaces = [i for i in net.interfaces if not i.name.startswith("lo")]

        # Forget interfaces no longer present (VPN/tether connect-disconnect
        # churn) so a long-running TUI session doesn't accumulate entries for
        # every interface name it's ever seen.
        current_names = {iface.name for iface in net.interfaces}
        for stale in set(self._prev_sent) - current_names:
            self._prev_sent.pop(stale, None)
            self._prev_recv.pop(stale, None)
            self._rates.pop(stale, None)

        # Rates are per second of *snapshot* time, not per refresh_interval: a
        # forced refresh (`r`, `t`, `p`) arrives early, and inside the
        # collector's cache TTL it is the very same snapshot (dt == 0), which
        # would otherwise render every interface as 0 B/s.
        timestamp = getattr(snap, "timestamp", None)
        dt = None
        if timestamp is not None and self._prev_timestamp is not None:
            dt = timestamp - self._prev_timestamp
        repeated = dt is not None and dt <= 0
        if not repeated:
            self._prev_timestamp = timestamp

        limit = DASHBOARD_LIMITS["interfaces"]
        for iface in non_lo_interfaces[:limit]:
            if repeated:
                rate_s, rate_r = self._rates.get(iface.name, (0.0, 0.0))
            else:
                rate_s = rate_r = 0.0
                if dt is not None and iface.name in self._prev_sent:
                    # A negative delta is a counter reset, not a negative rate.
                    rate_s = max(0, iface.bytes_sent - self._prev_sent[iface.name]) / dt
                    rate_r = max(0, iface.bytes_recv - self._prev_recv[iface.name]) / dt
                self._rates[iface.name] = (rate_s, rate_r)
                self._prev_sent[iface.name] = iface.bytes_sent
                self._prev_recv[iface.name] = iface.bytes_recv

            addr = iface.addrs[0] if iface.addrs else "—"
            text.append(f"  {iface.name:<10} ", style="bold")
            text.append(f"▲ {human_bytes(rate_s)}/s ", style=palette.title)
            text.append(f"▼ {human_bytes(rate_r)}/s", style=palette.good)
            text.append(f"  {addr}\n", style=palette.muted)
        more_row(text, len(non_lo_interfaces) - limit, "interfaces", palette)

        conns = "—" if net.connections_count is None else net.connections_count
        text.append(f"  Connections: {conns}\n", style=palette.muted)
        self.update(text)
