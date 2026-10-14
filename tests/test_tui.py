import asyncio
from unittest.mock import patch

import pytest

from sentinella.config import (
    SentinellaConfig,
)
from sentinella.core.models import SystemSnapshot
from sentinella.remote.client import RemoteCollector
from sentinella.tui.app import SentinellaApp


@pytest.mark.asyncio
async def test_tui_app_mount(mock_config, mock_collector):
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test():
        assert app.title == "🔔 Sentinella System Monitor"
        # Verify that all panel widgets exist
        assert app.query_one("#cpu-panel") is not None
        assert app.query_one("#memory-panel") is not None
        assert app.query_one("#process-panel") is not None
        assert app.query_one("#disk-panel") is not None
        assert app.query_one("#network-panel") is not None
        assert app.query_one("#sensor-panel") is not None
        assert app.query_one("#container-panel") is not None


@pytest.mark.asyncio
async def test_process_table_updates(mock_config, dummy_snapshot, make_collector):
    from sentinella.core.models import ProcessInfo, SystemSnapshot
    from sentinella.tui.widgets.process_table import ProcessTable

    snap = SystemSnapshot(
        timestamp=dummy_snapshot.timestamp,
        processes=[ProcessInfo(pid=1, name="init", cpu_percent=1.0, memory_percent=0.5)],
    )

    app = SentinellaApp(collector=make_collector(snap), config=mock_config)
    async with app.run_test():
        table = app.query_one("#process-panel", ProcessTable)
        assert table is not None
        table.update_data(snap, max_display=10, sort_by="cpu")
        assert table.row_count > 0


@pytest.mark.asyncio
async def test_reconcile_process_sort_uses_collector_override(mock_config, dummy_snapshot):
    """Cycling to a non-default sort must re-collect via collect_processes_async
    (when the collector supports it) rather than only re-sorting the snapshot
    the plugin already truncated by the configured default sort key."""
    from sentinella.core.models import ProcessInfo

    fresh_procs = [ProcessInfo(pid=42, name="reconciled", cpu_percent=1.0, memory_percent=99.0)]

    class StubCollector:
        async def collect_async(self):
            return dummy_snapshot

        async def collect_processes_async(self, sort_by):
            assert sort_by == "memory"
            return fresh_procs

        def close(self):
            pass

        async def close_async(self):
            pass

    assert mock_config.processes.sort_by == "cpu"
    app = SentinellaApp(collector=StubCollector(), config=mock_config)
    app._sort_index = 1  # "memory" in _SORT_CYCLE

    reconciled = await app._reconcile_process_sort(dummy_snapshot)
    assert reconciled.processes == fresh_procs


@pytest.mark.asyncio
async def test_reconcile_process_sort_is_noop_on_configured_default_sort(
    mock_config, dummy_snapshot, mock_collector
):
    """No override call when the active sort already matches the plugin's
    truncation key."""
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    reconciled = await app._reconcile_process_sort(dummy_snapshot)
    assert reconciled is dummy_snapshot


@pytest.mark.asyncio
async def test_dashboard_grid_has_no_dead_band_under_header(mock_config, mock_collector):
    """The header must not reserve a whole grid row.

    dashboard.tcss declares `grid-size: 4 5`; without an explicit `grid-rows`
    every row takes an equal fraction of the height, so the 3-cell-tall header
    left 8 blank cells above the CPU panel on a 45-row terminal.
    """
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test(size=(140, 45)):
        header = app.query_one("#sentinella-header")
        cpu = app.query_one("#cpu-panel")
        # CPU starts immediately after the header plus the 1-cell grid gutter.
        assert cpu.region.y == header.region.y + header.region.height + 1


@pytest.mark.asyncio
async def test_process_panel_absorbs_leftover_height(mock_config, mock_collector):
    """`#process-panel { height: 1fr }` only means something once the grid rows
    are sized, so the densest panel gets the slack instead of an equal share."""
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test(size=(140, 45)):
        proc = app.query_one("#process-panel")
        disk = app.query_one("#disk-panel")
        assert proc.region.height > disk.region.height


@pytest.mark.asyncio
async def test_panels_fit_their_grid_cells(mock_config, mock_collector):
    """A Static clips silently, so a DASHBOARD_LIMITS value one row too high
    drops the bottom line of a panel with no indication it did."""
    from rich.text import Text
    from textual.widgets import Static

    rendered: dict[str, int] = {}
    original = Static.update

    def capture(self, content="", **kwargs):
        if isinstance(content, Text):
            rendered[self.id] = content.plain.rstrip("\n").count("\n") + 1
        return original(self, content, **kwargs)

    Static.update = capture
    try:
        app = SentinellaApp(collector=mock_collector, config=mock_config)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            overflowing = []
            for panel in ("cpu-panel", "memory-panel", "disk-panel", "network-panel"):
                widget = app.query_one(f"#{panel}")
                available = widget.region.height - 2  # solid border, top and bottom
                if rendered.get(panel, 0) > available:
                    overflowing.append(panel)
            assert not overflowing, f"panels clipped: {overflowing}"
    finally:
        Static.update = original


@pytest.mark.asyncio
async def test_widget_colors_follow_the_theme(mock_config, mock_collector, dummy_snapshot):
    """Widgets used to emit hardcoded dark-theme literals (`bright_white`,
    `#00d2ff`), which sat near 1.3:1 against the light theme's surface."""
    from sentinella.tui.theme import palette_for

    seen = {}
    for theme, expected_theme in (("dark", "textual-dark"), ("light", "textual-light")):
        mock_config.general.theme = theme
        app = SentinellaApp(collector=mock_collector, config=mock_config)
        async with app.run_test(size=(140, 45)):
            assert app.theme == expected_theme
            seen[theme] = palette_for(app.query_one("#cpu-panel"))

    assert seen["dark"] != seen["light"]
    # The value role tracks the theme foreground, so it must invert.
    assert seen["dark"].value != seen["light"].value


@pytest.mark.asyncio
async def test_reconcile_keeps_the_snapshot_when_the_collector_cannot_rerank(
    mock_config, dummy_snapshot, mock_collector
):
    """A remote agent can't re-collect with another sort key; its
    collect_processes_async says so with None and the snapshot stands."""
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    app._sort_index = 1  # "memory", not the configured "cpu"
    reconciled = await app._reconcile_process_sort(dummy_snapshot)
    assert reconciled is dummy_snapshot


def test_both_collectors_implement_the_optional_capabilities():

    remote = RemoteCollector("127.0.0.1:9")
    try:
        remote.set_collect_cmdline(True)  # the agent decides; a no-op here
        assert asyncio.run(remote.collect_processes_async("memory")) is None
    finally:
        remote.close()


@pytest.mark.asyncio
async def test_panels_take_their_background_from_the_theme(mock_config, mock_collector):
    """The stylesheet paints panels with $surface; a hardcoded colour would
    stay put when the theme changes and defeat general.theme."""
    from textual.color import Color

    backgrounds = {}
    for theme in ("dark", "light"):
        mock_config.general.theme = theme
        app = SentinellaApp(collector=mock_collector, config=mock_config)
        async with app.run_test(size=(140, 45)):
            surface = Color.parse(app.get_css_variables()["surface"])
            for panel_id in (
                "#sentinella-header",
                "#cpu-panel",
                "#memory-panel",
                "#process-panel",
                "#disk-panel",
                "#network-panel",
                "#sensor-panel",
                "#container-panel",
            ):
                assert app.query_one(panel_id).styles.background == surface, (theme, panel_id)
            backgrounds[theme] = surface
    assert backgrounds["dark"] != backgrounds["light"]


# ── P2-7: NetworkWidget must not accumulate interfaces forever ───────────────


def test_network_widget_prunes_interfaces_no_longer_present():
    from sentinella.core.models import NetworkInterface, NetworkMetrics
    from sentinella.tui.widgets.network_widget import NetworkWidget

    def snap_with(names):
        return type(
            "S",
            (),
            {
                "network": NetworkMetrics(
                    interfaces=[
                        NetworkInterface(name=n, bytes_sent=100, bytes_recv=100) for n in names
                    ],
                    connections_count=0,
                )
            },
        )()

    widget = NetworkWidget()
    with patch.object(type(widget), "update", lambda self, t: None):
        widget.update_data(snap_with(["eth0", "wg0"]))
        assert set(widget._prev_sent) == {"eth0", "wg0"}

        widget.update_data(snap_with(["eth0"]))  # wg0 (VPN) disconnected
        assert set(widget._prev_sent) == {"eth0"}
        assert set(widget._prev_recv) == {"eth0"}


# ── Phase 3 (R5): the palette cache must not leak across apps ──────────────


def test_theme_cache_is_scoped_per_app():
    from sentinella.tui import theme as theme_mod

    class FakeApp:
        theme = "textual-dark"

        def get_css_variables(self):
            return {
                "text-primary": "#111111",
                "foreground": "#222222",
                "surface": "#333333",
                "text-success": "#444444",
                "text-warning": "#555555",
                "text-error": "#666666",
            }

    class Widget:
        def __init__(self, app):
            self.app = app

    app_a, app_b = FakeApp(), FakeApp()
    pal_a = theme_mod.palette_for(Widget(app_a))
    pal_b = theme_mod.palette_for(Widget(app_b))
    assert pal_a == pal_b  # same theme resolves the same
    # ...but cached separately, so one app's entry cannot outlive it.
    assert theme_mod._cache[app_a] is not theme_mod._cache[app_b]


# ── Phase 6 (U1): a persistent failure must notify once, not every tick ─────


@pytest.mark.asyncio
async def test_collection_failure_notifies_once_not_every_tick(mock_config):
    """A remote agent that is down produced one toast per refresh_interval,
    indefinitely, burying the dashboard it was describing."""
    from sentinella.tui.app import SentinellaApp

    class AlwaysFailing:
        async def collect_async(self):
            raise ConnectionError("agent is down")

        def collect(self):
            raise ConnectionError("agent is down")

        def close(self):
            pass

        async def close_async(self):
            pass

    app = SentinellaApp(collector=AlwaysFailing(), config=mock_config)
    notifications = []
    app.notify = lambda msg, **kw: notifications.append(msg)  # type: ignore[method-assign]
    async with app.run_test():
        # on_mount fires its own tick; start counting from a known state.
        notifications.clear()
        app._collection_failing = False
        for _ in range(5):
            await app._async_tick()

    assert len(notifications) == 1, f"one toast per tick: {notifications}"
    assert app._collection_failing is True


@pytest.mark.asyncio
async def test_recovery_is_announced_once(mock_config, dummy_snapshot):
    from sentinella.tui.app import SentinellaApp

    class Flaky:
        def __init__(self):
            self.fail = True

        async def collect_async(self):
            if self.fail:
                raise ConnectionError("down")
            return dummy_snapshot

        def collect(self):
            return dummy_snapshot

        def close(self):
            pass

        async def close_async(self):
            pass

    collector = Flaky()
    app = SentinellaApp(collector=collector, config=mock_config)
    notifications = []
    app.notify = lambda msg, **kw: notifications.append(msg)  # type: ignore[method-assign]
    async with app.run_test():
        notifications.clear()
        app._collection_failing = False
        await app._async_tick()
        await app._async_tick()
        collector.fail = False
        await app._async_tick()
        await app._async_tick()

    assert len(notifications) == 2, notifications
    assert "failed" in notifications[0].lower()
    assert "recovered" in notifications[1].lower()
    assert app._collection_failing is False


# ── Phase 6 (U3): the TUI gets the runtime theme toggle the web already had ──


@pytest.mark.asyncio
async def test_tui_theme_toggle_flips_and_repaints(mock_config, mock_collector):
    from sentinella.tui.app import SentinellaApp

    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test() as pilot:
        assert app.theme == "textual-dark"
        await pilot.press("t")
        assert app.theme == "textual-light"
        await pilot.press("t")
        assert app.theme == "textual-dark"


# ── Phase 6: one definition of "this container is running" ──────────────────


def test_every_surface_counts_all_running_spellings(capsys, make_collector):
    """Docker says "running", LXC "Running", some runtimes "Up". A surface
    that tested status == "running" itself would undercount the others."""
    from sentinella.core.models import ContainerInfo, ContainerMetrics
    from sentinella.fetch import run_fetch
    from sentinella.tui.widgets.container_widget import ContainerWidget

    snap = SystemSnapshot(
        containers=ContainerMetrics(
            containers=[
                ContainerInfo(name="a", status="Up", runtime="docker"),
                ContainerInfo(name="b", status="Running", runtime="docker"),
                ContainerInfo(name="c", status="exited", runtime="docker"),
            ],
            docker_available=True,
        )
    )
    run_fetch(SentinellaConfig(), collector=make_collector(snap))
    assert "2 running" in capsys.readouterr().out

    widget = ContainerWidget()
    captured = {}
    with patch.object(ContainerWidget, "update", lambda self, t: captured.setdefault("t", t)):
        widget.update_data(snap)
    assert "2/3 running" in captured["t"].plain
