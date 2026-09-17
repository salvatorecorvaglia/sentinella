import pytest

from sentinella.tui.app import SentinellaApp


@pytest.mark.asyncio
async def test_tui_app_mount(mock_config, mock_collector):
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test():
        assert app.title == "🐦‍⬛ Sentinella System Monitor"
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
    truncation key — MockMetricCollector doesn't even implement
    collect_processes_async, so this also covers collectors that can't."""
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
