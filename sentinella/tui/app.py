"""Sentinella TUI — full-screen Textual dashboard.

Keybindings:
    q / ctrl-c  Quit
    r           Force refresh
    p           Cycle process sort (cpu → memory → pid → name)
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer

from sentinella.config import SentinellaConfig
from sentinella.core.collector import Collector
from sentinella.core.models import SystemSnapshot
from sentinella.core.protocols import MetricCollector
from sentinella.tui.widgets.container_widget import ContainerWidget
from sentinella.tui.widgets.cpu_widget import CpuWidget
from sentinella.tui.widgets.disk_widget import DiskWidget
from sentinella.tui.widgets.header_widget import HeaderWidget
from sentinella.tui.widgets.memory_widget import MemoryWidget
from sentinella.tui.widgets.network_widget import NetworkWidget
from sentinella.tui.widgets.process_table import ProcessTable
from sentinella.tui.widgets.sensor_widget import SensorWidget

log = logging.getLogger(__name__)

# Sort cycle for processes
_SORT_CYCLE = ["cpu", "memory", "pid", "name"]

CSS_PATH = Path(__file__).parent / "dashboard.tcss"


class SentinellaApp(App):
    """Sentinella system monitor TUI application."""

    TITLE = "🔔 Sentinella System Monitor"
    CSS_PATH = CSS_PATH

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "cycle_sort", "Sort procs"),
        # The web dashboard has had a runtime theme toggle since 1.3.0; the TUI
        # could only be re-themed by editing sentinella.toml and restarting.
        Binding("t", "toggle_theme", "Theme"),
    ]

    def __init__(
        self,
        collector: MetricCollector | None = None,
        config: SentinellaConfig | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        from sentinella.config import load_config

        self._config = config or load_config()
        self._collector: MetricCollector = collector or Collector(self._config)
        sort_by = self._config.processes.sort_by
        if sort_by in _SORT_CYCLE:
            self._sort_index = _SORT_CYCLE.index(sort_by)
        else:
            self._sort_index = 0
        # Tracks whether the last tick failed, so a persistent failure notifies
        # once rather than on every tick.
        self._collection_failing = False

    def compose(self) -> ComposeResult:
        with Container(id="dashboard"):
            yield HeaderWidget(id="sentinella-header")
            yield CpuWidget(id="cpu-panel")
            yield MemoryWidget(id="memory-panel")
            yield ProcessTable(id="process-panel")
            yield DiskWidget(id="disk-panel")
            yield NetworkWidget(id="network-panel")
            yield SensorWidget(id="sensor-panel")
            yield ContainerWidget(id="container-panel")
        yield Footer()

    def on_mount(self) -> None:
        """Apply the configured theme and start the periodic refresh timer."""
        # dashboard.tcss uses design tokens ($surface, $accent, …), so setting
        # the theme here repaints the whole dashboard.
        self.theme = "textual-light" if self._config.general.theme == "light" else "textual-dark"

        interval = self._config.general.refresh_interval
        self.set_interval(interval, self._tick)
        # Do an initial collection immediately
        self._tick()

    def _tick(self) -> None:
        """Collect metrics and push to all widgets."""
        # Textual types run_worker's callable as returning Never; a plain
        # `async def ... -> None` is a valid worker despite the annotation.
        self.run_worker(self._async_tick, exclusive=True)  # type: ignore[arg-type]

    async def _async_tick(self) -> None:
        try:
            snap = await self._collector.collect_async()
            snap = await self._reconcile_process_sort(snap)
            self._update_widgets(snap)
        except Exception:
            log.exception("Metric collection failed")
            # Notify on the *transition* only. Firing every tick meant a
            # remote agent that was down produced a toast every
            # refresh_interval seconds, indefinitely, burying the dashboard.
            if not self._collection_failing:
                self._collection_failing = True
                self.notify("⚠ Collection failed — data may be stale", severity="warning")
            self._set_stale(True)
        else:
            if self._collection_failing:
                self._collection_failing = False
                self.notify("✓ Collection recovered", severity="information")
            self._set_stale(False)

    def _set_stale(self, stale: bool) -> None:
        """Mark the dashboard as showing stale data.

        A persistent marker beats a stream of toasts: it says the same thing
        without competing for the screen, and it mirrors the web dashboard's
        disconnected badge.
        """
        try:
            self.query_one("#dashboard").set_class(stale, "stale")
        except Exception:
            log.debug("Could not toggle the stale class", exc_info=True)

    async def _reconcile_process_sort(self, snap: SystemSnapshot) -> SystemSnapshot:
        """Re-truncate the process list if the active sort isn't the configured default.

        The snapshot's process list is truncated by the plugin using
        ``config.processes.sort_by``. When the user cycles to a different
        sort with `p`, re-sorting that already-truncated slice can hide a
        process that would rank in range under the new key. Only local
        ``Collector`` instances support the on-demand re-collect this needs
        (``RemoteCollector`` has no way to ask the remote agent for a
        different sort); other collectors fall back to the old best-effort
        re-sort in ``ProcessTable``.
        """
        active_sort = _SORT_CYCLE[self._sort_index]
        if active_sort == self._config.processes.sort_by:
            return snap
        collect_processes_async = getattr(self._collector, "collect_processes_async", None)
        if collect_processes_async is None:
            return snap
        try:
            procs = await collect_processes_async(active_sort)
        except Exception:
            log.debug("Failed to refresh processes for sort=%s", active_sort, exc_info=True)
            return snap
        return dataclasses.replace(snap, processes=procs)

    def _update_widgets(self, snap: SystemSnapshot) -> None:
        """Push a snapshot to all dashboard widgets."""
        header: HeaderWidget = self.query_one("#sentinella-header", HeaderWidget)
        header.update_data(snap)

        cpu: CpuWidget = self.query_one("#cpu-panel", CpuWidget)
        cpu.update_data(snap)

        mem: MemoryWidget = self.query_one("#memory-panel", MemoryWidget)
        mem.update_data(snap)

        disk: DiskWidget = self.query_one("#disk-panel", DiskWidget)
        disk.update_data(snap)

        net: NetworkWidget = self.query_one("#network-panel", NetworkWidget)
        net.update_data(snap, refresh_interval=self._config.general.refresh_interval)

        sensor: SensorWidget = self.query_one("#sensor-panel", SensorWidget)
        sensor.update_data(snap)

        container: ContainerWidget = self.query_one("#container-panel", ContainerWidget)
        container.update_data(snap)

        proc: ProcessTable = self.query_one("#process-panel", ProcessTable)
        sort_by = _SORT_CYCLE[self._sort_index]
        proc.update_data(
            snap,
            max_display=self._config.processes.max_display,
            sort_by=sort_by,
        )

    # ── Actions ──────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._tick()

    def action_cycle_sort(self) -> None:
        self._sort_index = (self._sort_index + 1) % len(_SORT_CYCLE)
        self.notify(f"Sorting processes by: {_SORT_CYCLE[self._sort_index]}")
        self._tick()

    def action_toggle_theme(self) -> None:
        """Flip between the light and dark themes for this session.

        Not persisted: ``general.theme`` in sentinella.toml stays the default.
        """
        light = self.theme == "textual-light"
        self.theme = "textual-dark" if light else "textual-light"
        self.notify(f"Theme: {'dark' if light else 'light'}")
        # Widgets resolve their palette from the app's theme when they repaint,
        # so push a frame rather than waiting for the next refresh tick.
        self._tick()

    async def on_unmount(self) -> None:
        """Teardown the collector on app exit.

        Async so that ``close_async()`` can be awaited: the TUI drives
        ``collect_async()``, so for a ``RemoteCollector`` it is the *async*
        HTTP client that holds open connections.
        """
        close_async = getattr(self._collector, "close_async", None)
        if close_async is not None:
            await close_async()
        else:
            self._collector.close()
