"""Quick-fetch command — neofetch-style system summary.

Usage::

    sentinella fetch
"""

from __future__ import annotations

import datetime
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from sentinella.config import SentinellaConfig
from sentinella.core.collector import Collector
from sentinella.core.utils import color_for_percent, human_bytes

# ── ASCII Art ────────────────────────────────────────────────────────────────

_SENTINELLA_ART = r"""
       ▄▄▄
      ▀█▀██▄
       ▀█▄███▄
        ██████▄
        ████████
       ▄█▀ █████
      ▄██   ▀████
     ████    ▀███
    █████     ███▌
   ▐████      ██▌
    ▀▀██▄    ▄██
       ▀▀▄▄▄▀▀
"""


def _color_percent(percent: float) -> str:
    """Return a colored percentage string."""
    color = color_for_percent(percent)
    return f"[{color}]{percent:.1f}%[/{color}]"


def run_fetch(config: SentinellaConfig | None = None, collector: Any = None) -> None:
    """Print a quick system summary to the console.

    *collector* lets the CLI pass a ``RemoteCollector`` so ``sentinella --remote
    HOST fetch`` summarises the remote agent instead of this machine.  When it
    is supplied the caller owns its lifecycle; a locally built one is closed
    here, since ``fetch`` is a one-shot command.
    """
    console = Console()
    owns_collector = collector is None
    collector = collector or Collector(config)
    try:
        snap = collector.collect()
    finally:
        if owns_collector:
            collector.close()  # release the thread pool

    si = snap.system_info
    cpu = snap.cpu
    mem = snap.memory
    disk = snap.disk
    net = snap.network
    sensors = snap.sensors
    containers = snap.containers

    uptime = str(datetime.timedelta(seconds=int(si.uptime_seconds)))

    # Build info lines
    lines: list[str] = []
    lines.append(
        f"[bold cyan]{escape(si.username)}[/bold cyan]@[bold cyan]{escape(si.hostname)}[/bold cyan]"
    )
    lines.append(f"[dim]{'─' * 30}[/dim]")
    lines.append(
        f"[bold]OS[/bold]       {escape(si.os_name)} {escape(si.os_version)} "
        f"({escape(si.architecture)})"
    )
    lines.append(f"[bold]Kernel[/bold]   {escape(si.kernel)}")
    lines.append(f"[bold]Uptime[/bold]   {uptime}")

    # CPU
    freq_str = f" @ {cpu.frequency_current_mhz:.0f} MHz" if cpu.frequency_current_mhz else ""
    phys = f"{cpu.core_count_physical}P/" if cpu.core_count_physical else ""
    lines.append(
        f"[bold]CPU[/bold]      {phys}{cpu.core_count_logical} cores{freq_str}"
        f" — {_color_percent(cpu.percent_overall)}"
    )

    # Load average
    if None not in (cpu.load_avg_1, cpu.load_avg_5, cpu.load_avg_15):
        load_str = (
            f"[bold]Load[/bold]     {cpu.load_avg_1:.2f}  "
            f"{cpu.load_avg_5:.2f}  {cpu.load_avg_15:.2f}"
        )
        lines.append(load_str)

    # Memory
    lines.append(
        f"[bold]Memory[/bold]   {human_bytes(mem.used)} / {human_bytes(mem.total)}"
        f" — {_color_percent(mem.percent)}"
    )

    # Swap
    if mem.swap_total > 0:
        lines.append(
            f"[bold]Swap[/bold]     {human_bytes(mem.swap_used)} / {human_bytes(mem.swap_total)}"
            f" — {_color_percent(mem.swap_percent)}"
        )

    # Disk (first partition only for brevity)
    if disk.partitions:
        dp = disk.partitions[0]
        lines.append(
            f"[bold]Disk[/bold]     {human_bytes(dp.used)} / {human_bytes(dp.total)}"
            f" — {_color_percent(dp.percent)}  ({escape(dp.mountpoint)})"
        )

    # Network (first non-loopback interface with an address)
    for iface in net.interfaces:
        if iface.name.startswith("lo"):
            continue
        if iface.addrs:
            lines.append(f"[bold]Network[/bold]  {escape(iface.name)}  {escape(iface.addrs[0])}")
            break

    # Temperatures
    if sensors.temperatures:
        temp_strs = [f"{escape(t.label)}: {t.current:.0f}°C" for t in sensors.temperatures[:4]]
        lines.append(f"[bold]Temps[/bold]    {', '.join(temp_strs)}")

    # Fans
    if sensors.fans:
        fan_strs = [f"{escape(f.label)}: {f.current} RPM" for f in sensors.fans[:3]]
        lines.append(f"[bold]Fans[/bold]     {', '.join(fan_strs)}")

    # Battery
    if sensors.battery:
        plugged = "⚡ plugged" if sensors.battery.power_plugged else "🔋 battery"
        pct_str = (
            f"{sensors.battery.percent:.0f}%" if sensors.battery.percent is not None else "Unknown"
        )
        lines.append(f"[bold]Battery[/bold]  {pct_str} {plugged}")

    # Containers
    docker_count = sum(1 for c in containers.containers if c.runtime == "docker")
    lxc_count = sum(1 for c in containers.containers if c.runtime == "lxc")
    running = sum(1 for c in containers.containers if c.is_running)
    if docker_count or lxc_count:
        parts = []
        if docker_count:
            parts.append(f"Docker: {docker_count}")
        if lxc_count:
            parts.append(f"LXC: {lxc_count}")
        parts.append(f"{running} running")
        lines.append(f"[bold]Containers[/bold] {', '.join(parts)}")

    # Users
    if snap.users:
        user_names = list({escape(u.name) for u in snap.users})
        lines.append(f"[bold]Users[/bold]    {', '.join(user_names)}")

    # Processes count — the host total, not the truncated display list
    lines.append(f"[bold]Procs[/bold]    {snap.process_count or len(snap.processes)}")

    # ── Assemble side-by-side layout ─────────────────────────────────
    art_lines = _SENTINELLA_ART.strip().splitlines()
    info_lines = lines

    # Pad to equal height
    max_height = max(len(art_lines), len(info_lines))
    while len(art_lines) < max_height:
        art_lines.append("")
    while len(info_lines) < max_height:
        info_lines.append("")

    art_width = max(len(line) for line in art_lines) + 4

    combined: list[str] = []
    for art, info in zip(art_lines, info_lines, strict=False):
        padded_art = art.ljust(art_width)
        combined.append(f"[bold magenta]{padded_art}[/bold magenta]{info}")

    output = "\n".join(combined)
    console.print()
    console.print(
        Panel(
            output,
            title="[bold bright_white]🔔 SENTINELLA[/bold bright_white]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print()
