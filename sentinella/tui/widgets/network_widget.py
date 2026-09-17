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

    def update_data(self, snap: SystemSnapshot, refresh_interval: float = 2.0) -> None:
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

        limit = DASHBOARD_LIMITS["interfaces"]
        for iface in non_lo_interfaces[:limit]:
            # Calculate per-second rates
            prev_s = self._prev_sent.get(iface.name, iface.bytes_sent)
            prev_r = self._prev_recv.get(iface.name, iface.bytes_recv)
            delta_s = max(0, iface.bytes_sent - prev_s)
            delta_r = max(0, iface.bytes_recv - prev_r)
            # Avoid division by zero; convert delta-per-interval to delta-per-second
            interval = max(refresh_interval, 0.1)
            rate_s = delta_s / interval
            rate_r = delta_r / interval
            self._prev_sent[iface.name] = iface.bytes_sent
            self._prev_recv[iface.name] = iface.bytes_recv

            addr = iface.addrs[0] if iface.addrs else "—"
            text.append(f"  {iface.name:<10} ", style="bold")
            text.append(f"▲ {human_bytes(rate_s)}/s ", style=palette.title)
            text.append(f"▼ {human_bytes(rate_r)}/s", style=palette.good)
            text.append(f"  {addr}\n", style=palette.muted)
        more_row(text, len(non_lo_interfaces) - limit, "interfaces", palette)

        text.append(f"  Connections: {net.connections_count}\n", style=palette.muted)
        self.update(text)
