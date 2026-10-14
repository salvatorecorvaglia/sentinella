"""Tests for the behaviour the docs promise: theming, process limits,
container stats, and export formatting."""

from unittest.mock import MagicMock, patch

import pytest

from sentinella.config import GeneralConfig, ModulesConfig, ProcessesConfig, SentinellaConfig
from sentinella.core.models import ProcessInfo, SystemSnapshot
from sentinella.core.utils import color_for_temp
from sentinella.export import EXPORT_FORMATS, get_exporter
from sentinella.plugins.containers import ContainersPlugin
from sentinella.plugins.processes import ProcessesPlugin
from sentinella.tui.app import SentinellaApp


def _snapshot_with(n: int) -> SystemSnapshot:
    """Snapshot with *n* processes of descending CPU and ascending memory."""
    return SystemSnapshot(
        processes=[
            ProcessInfo(
                pid=i,
                name=f"proc{i:03d}",
                cpu_percent=float(n - i),
                memory_percent=float(i),
            )
            for i in range(n)
        ],
        process_count=n * 10,
    )


# ── M1: the plugin must sort by the configured key before truncating ─────────


def test_processes_plugin_sorts_by_configured_key():
    cfg = SentinellaConfig(processes=ProcessesConfig(sort_by="memory", max_display=5))
    plugin = ProcessesPlugin(cfg)
    procs = plugin.collect()
    mem = [p.memory_percent for p in procs]
    assert mem == sorted(mem, reverse=True), "memory sort not applied before truncation"


def test_processes_plugin_sort_by_pid_is_ascending():
    cfg = SentinellaConfig(processes=ProcessesConfig(sort_by="pid"))
    pids = [p.pid for p in ProcessesPlugin(cfg).collect()]
    assert pids == sorted(pids)


def test_processes_plugin_reports_untruncated_total():
    plugin = ProcessesPlugin(SentinellaConfig(processes=ProcessesConfig(max_display=1)))
    procs = plugin.collect()
    assert procs.total >= len(procs)


# ── U3: process_count is the host total, not the truncated length ────────────


def test_snapshot_process_count_is_independent_of_list_length():
    snap = _snapshot_with(3)
    assert len(snap.processes) == 3
    assert snap.process_count == 30


def test_remote_client_round_trips_process_count():
    from sentinella.core.utils import serialize_model
    from sentinella.remote.client import RemoteCollector

    parsed = RemoteCollector._parse(serialize_model(_snapshot_with(4)))
    assert parsed.process_count == 40


# ── M5: exporters honour max_display and sort_by ─────────────────────────────


def test_text_exporter_respects_max_display():
    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=3, sort_by="cpu"))
    out = get_exporter("text", cfg).format(_snapshot_with(50), modules=["processes"])
    listed = [ln for ln in out.splitlines() if "proc" in ln]
    assert len(listed) == 3


def test_text_exporter_respects_sort_by_memory():
    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=3, sort_by="memory"))
    out = get_exporter("text", cfg).format(_snapshot_with(50), modules=["processes"])
    listed = [ln for ln in out.splitlines() if "proc" in ln]
    # Highest memory_percent is the highest index.
    assert "proc049" in listed[0]


def test_text_exporter_shows_shown_of_total():
    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=2))
    out = get_exporter("text", cfg).format(_snapshot_with(10), modules=["processes"])
    assert "(2 of 100)" in out


def test_exporter_registry_covers_all_documented_formats():
    for fmt in EXPORT_FORMATS:
        exporter = get_exporter(fmt)
        assert exporter.name == fmt
        assert isinstance(exporter.format(_snapshot_with(2)), str)


def test_unknown_format_falls_back_to_text():
    assert get_exporter("nonsense").name == "text"


# ── M4: theme is applied, not merely validated ───────────────────────────────


@pytest.mark.asyncio
async def test_tui_applies_light_theme(mock_config, mock_collector):
    mock_config.general.theme = "light"
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test():
        assert app.theme == "textual-light"


@pytest.mark.asyncio
async def test_tui_applies_dark_theme(mock_config, mock_collector):
    mock_config.general.theme = "dark"
    app = SentinellaApp(collector=mock_collector, config=mock_config)
    async with app.run_test():
        assert app.theme == "textual-dark"


def test_health_reports_configured_theme():
    from loopback_client import TestClient

    from sentinella.web.server import create_app

    cfg = SentinellaConfig(general=GeneralConfig(theme="light"))
    with TestClient(create_app(cfg)) as client:
        assert client.get("/health").json()["theme"] == "light"


# ── M5 (web): the dashboard honours the agent's max_display ──────────────────


def test_health_reports_max_display():
    from loopback_client import TestClient

    from sentinella.web.server import create_app

    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=7))
    with TestClient(create_app(cfg)) as client:
        assert client.get("/health").json()["max_display"] == 7


def test_dashboard_takes_its_process_limit_from_the_agent():
    """The dashboard must render max_display rows, not a number of its own.

    Asserted through /health's contract rather than by grepping app.js for an
    expression, which broke whenever the line was reformatted.
    """
    from loopback_client import TestClient

    from sentinella.web.server import create_app

    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=9))
    with TestClient(create_app(cfg)) as client:
        health = client.get("/health").json()
        snapshot = client.get("/api/v1/snapshot").json()

    assert health["max_display"] == 9
    # And the agent actually trims to it, so the dashboard cannot show more.
    assert len(snapshot["processes"]) <= 9


# ── U7: temperatures use trip points, not the percentage scale ───────────────


def test_temp_color_prefers_sensor_trip_points():
    # 55 °C would be "warn" on the 50/80 percentage scale, but it is well
    # under this sensor's 90 °C high point.
    assert color_for_temp(55.0, high=90.0, critical=100.0) == "#00d2ff"
    assert color_for_temp(95.0, high=90.0, critical=100.0) == "#f59e0b"
    assert color_for_temp(101.0, high=90.0, critical=100.0) == "#ef4444"


def test_temp_color_fallback_when_no_trip_points():
    assert color_for_temp(40.0) == "#00d2ff"
    assert color_for_temp(75.0) == "#f59e0b"
    assert color_for_temp(90.0) == "#ef4444"


# ── M9: container CPU/memory ─────────────────────────────────────────────────


def _docker_mock(stats_payload):
    container = MagicMock()
    container.name = "web"
    container.short_id = "abc123"
    container.status = "running"
    container.attrs = {"Config": {"Image": "nginx:latest"}}
    container.stats.return_value = stats_payload

    client = MagicMock()
    client.containers.list.return_value = [container]
    docker_mod = MagicMock()
    docker_mod.from_env.return_value = client
    return docker_mod, container


_STATS = {
    "cpu_stats": {
        "cpu_usage": {"total_usage": 2_000_000},
        "system_cpu_usage": 20_000_000,
        "online_cpus": 4,
    },
    "precpu_stats": {
        "cpu_usage": {"total_usage": 1_000_000},
        "system_cpu_usage": 10_000_000,
    },
    "memory_stats": {"usage": 200_000_000, "limit": 1_000_000_000, "stats": {"cache": 50_000_000}},
}


def test_container_stats_populated_when_enabled():
    docker_mod, _ = _docker_mock(_STATS)
    cfg = SentinellaConfig(modules=ModulesConfig(container_stats=True))
    with (
        patch("shutil.which", return_value=None),
        patch.dict("sys.modules", {"docker": docker_mod}),
    ):
        info = ContainersPlugin(cfg).collect().containers[0]

    # cpu_delta/system_delta = 1e6/1e7 = 0.1; × 4 CPUs × 100 = 40%
    assert info.cpu_percent == 40.0
    assert info.memory_usage == 150_000_000  # usage minus page cache
    assert info.memory_limit == 1_000_000_000


def test_container_stats_absent_when_disabled():
    docker_mod, container = _docker_mock(_STATS)
    cfg = SentinellaConfig(modules=ModulesConfig(container_stats=False))
    with (
        patch("shutil.which", return_value=None),
        patch.dict("sys.modules", {"docker": docker_mod}),
    ):
        info = ContainersPlugin(cfg).collect().containers[0]

    assert info.cpu_percent is None
    container.stats.assert_not_called()


def test_container_stats_failure_leaves_values_none():
    docker_mod, container = _docker_mock(_STATS)
    container.stats.side_effect = RuntimeError("daemon busy")
    cfg = SentinellaConfig(modules=ModulesConfig(container_stats=True))
    with (
        patch("shutil.which", return_value=None),
        patch.dict("sys.modules", {"docker": docker_mod}),
    ):
        info = ContainersPlugin(cfg).collect().containers[0]

    assert info.cpu_percent is None
    assert info.name == "web"  # the rest of the record still arrives


def test_container_stats_tolerates_malformed_payload():
    docker_mod, _ = _docker_mock({"cpu_stats": {}, "precpu_stats": {}, "memory_stats": {}})
    cfg = SentinellaConfig(modules=ModulesConfig(container_stats=True))
    with (
        patch("shutil.which", return_value=None),
        patch.dict("sys.modules", {"docker": docker_mod}),
    ):
        info = ContainersPlugin(cfg).collect().containers[0]

    assert info.cpu_percent is None
    assert info.memory_usage is None


# ── CPU frequency units ──────────────────────────────────────────────────────


def test_cpu_frequency_ghz_is_normalised_to_mhz():
    """psutil reports GHz on Apple Silicon; "4 MHz" is not a real frequency."""
    from sentinella.plugins.cpu import CpuPlugin

    assert CpuPlugin._normalise_mhz(4) == 4000
    assert CpuPlugin._normalise_mhz(3.5) == 3500


def test_cpu_frequency_mhz_is_left_alone():
    from sentinella.plugins.cpu import CpuPlugin

    assert CpuPlugin._normalise_mhz(2400.0) == 2400.0
    assert CpuPlugin._normalise_mhz(0) is None
    assert CpuPlugin._normalise_mhz(None) is None


def test_fetch_reports_untruncated_process_count(capsys, mock_config):
    """`sentinella fetch` printed the truncated list length, not the host total."""
    from sentinella.fetch import run_fetch

    run_fetch(mock_config)
    out = capsys.readouterr().out
    assert "Procs" in out


# ── A8: widget history is rendered, not collected and discarded ──────────────


def _render(widget, snapshots):
    """Drive update_data and return the Text the widget would display."""
    captured = {}
    with patch.object(type(widget), "update", lambda self, t: captured.__setitem__("t", t)):
        for snap in snapshots:
            widget.update_data(snap)
    return captured["t"].plain


def test_cpu_widget_renders_sparkline():
    from sentinella.core.models import CpuMetrics
    from sentinella.tui.widgets.cpu_widget import CpuWidget

    out = _render(
        CpuWidget(),
        [
            SystemSnapshot(timestamp=t, cpu=CpuMetrics(percent_overall=p, percent_per_core=[p]))
            for t, p in enumerate((5, 90, 40))
        ],
    )
    assert "History" in out
    assert any(ch in out for ch in "▂▃▄▅▆▇█"), "sparkline blocks not rendered"


def test_memory_widget_renders_sparkline():
    from sentinella.core.models import MemoryMetrics
    from sentinella.tui.widgets.memory_widget import MemoryWidget

    out = _render(
        MemoryWidget(),
        [
            SystemSnapshot(timestamp=t, memory=MemoryMetrics(total=100, used=p, percent=p))
            for t, p in enumerate((10, 80, 50))
        ],
    )
    assert "Trend" in out
    assert any(ch in out for ch in "▂▃▄▅▆▇█")


def test_widget_history_is_not_stored_on_the_app():
    """History used to be monkey-patched onto the App object."""
    from sentinella.core.models import CpuMetrics
    from sentinella.tui.widgets.cpu_widget import CpuWidget

    widget = CpuWidget()
    _render(widget, [SystemSnapshot(cpu=CpuMetrics(percent_overall=1.0))])
    assert len(widget._history) == 1


def test_single_sample_produces_no_sparkline():
    from sentinella.core.models import CpuMetrics
    from sentinella.tui.widgets.cpu_widget import CpuWidget

    out = _render(CpuWidget(), [SystemSnapshot(cpu=CpuMetrics(percent_overall=50.0))])
    assert "History" not in out


# ── P3-10: sensor/container widgets previously had no dedicated tests ────────


def test_sensor_widget_renders_temps_fans_battery_and_users():
    from sentinella.core.models import (
        BatteryInfo,
        FanReading,
        SensorMetrics,
        TemperatureReading,
        UserInfo,
    )
    from sentinella.tui.widgets.sensor_widget import SensorWidget

    snap = SystemSnapshot(
        sensors=SensorMetrics(
            temperatures=[TemperatureReading(label="Core 0", current=55.0, high=90.0)],
            fans=[FanReading(label="Fan 1", current=2200)],
            battery=BatteryInfo(percent=42.0, power_plugged=False),
        ),
        users=[UserInfo(name="alice"), UserInfo(name="bob")],
    )
    out = _render(SensorWidget(), [snap])
    assert "Temps" in out and "Core 0" in out and "55" in out
    assert "Fans" in out and "2200 RPM" in out
    assert "Battery" in out and "42%" in out and "🔋" in out
    assert "Users" in out and "alice" in out and "bob" in out


def test_sensor_widget_reports_no_data_when_all_empty():
    from sentinella.tui.widgets.sensor_widget import SensorWidget

    out = _render(SensorWidget(), [SystemSnapshot()])
    assert "No sensor data" in out


def test_container_widget_hides_panel_when_no_runtime_available():
    from sentinella.tui.widgets.container_widget import ContainerWidget

    widget = ContainerWidget()
    widget.update_data(SystemSnapshot())  # docker_available=False, lxc_available=False
    assert widget.display is False


def test_container_widget_shows_running_containers():
    from sentinella.core.models import ContainerInfo, ContainerMetrics
    from sentinella.tui.widgets.container_widget import ContainerWidget

    snap = SystemSnapshot(
        containers=ContainerMetrics(
            containers=[
                ContainerInfo(name="web", image="nginx", status="running", runtime="docker"),
                ContainerInfo(name="db", image="postgres", status="exited", runtime="docker"),
            ],
            docker_available=True,
        )
    )
    out = _render(ContainerWidget(), [snap])
    assert "Containers" in out
    assert "1/2 running" in out
    assert "web" in out and "nginx" in out
    assert "db" in out and "postgres" in out


def test_container_widget_reports_empty_state_when_runtime_available_but_idle():
    from sentinella.core.models import ContainerMetrics
    from sentinella.tui.widgets.container_widget import ContainerWidget

    snap = SystemSnapshot(containers=ContainerMetrics(containers=[], docker_available=True))
    out = _render(ContainerWidget(), [snap])
    assert "No containers detected" in out


# ── A6: plugins share a uniform constructor ──────────────────────────────────


def test_every_builtin_plugin_accepts_config():
    """get_enabled_plugins no longer inspects signatures to decide how to build."""
    import importlib

    from sentinella.core.plugin_manager import _BUILTIN_PLUGINS, _load_plugin_module

    for mod_name in _BUILTIN_PLUGINS:
        importlib.import_module(f"sentinella.plugins.{mod_name}")
        cls = _load_plugin_module(mod_name)
        assert cls is not None, mod_name
        assert cls(config=SentinellaConfig()) is not None, mod_name


# ── M8: the no-op --once flag is gone ────────────────────────────────────────


def test_once_flag_removed():
    from sentinella.cli import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["print", "--once"])


# ── A4: the two declared versions must not drift ────────────────────────────


def test_fallback_version_matches_pyproject():
    """__init__.py carries a hardcoded fallback for editable installs without
    metadata. Nothing kept it in step with pyproject.toml."""
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).parent.parent
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    source = (root / "sentinella/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', source)
    assert match, "no fallback __version__ literal found in sentinella/__init__.py"
    assert match.group(1) == declared, (
        f"fallback version {match.group(1)!r} != pyproject {declared!r}"
    )


# ── A2: the dashboard must take its thresholds from the agent ───────────────


def test_health_reports_severity_thresholds():
    from loopback_client import TestClient

    from sentinella.core.utils import PERCENT_THRESHOLDS, TEMP_THRESHOLDS
    from sentinella.web.server import create_app

    with TestClient(create_app(SentinellaConfig())) as client:
        thresholds = client.get("/health").json()["thresholds"]
    assert thresholds["percent"] == list(PERCENT_THRESHOLDS)
    assert thresholds["temp"] == list(TEMP_THRESHOLDS)


# ── Forced refreshes must not distort rates or history ──────────────────────


def _net_snap(ts, sent, recv):
    from sentinella.core.models import NetworkInterface, NetworkMetrics

    return SystemSnapshot(
        timestamp=ts,
        network=NetworkMetrics(
            interfaces=[NetworkInterface(name="eth0", bytes_sent=sent, bytes_recv=recv)]
        ),
    )


def test_network_rate_uses_the_time_between_snapshots():
    """The rate divided by refresh_interval, so a refresh that arrived early
    (the `r`/`t`/`p` keys) or late understated or overstated it."""
    from sentinella.tui.widgets.network_widget import NetworkWidget

    out = _render(NetworkWidget(), [_net_snap(100.0, 0, 0), _net_snap(101.0, 1000, 3000)])
    assert "▲ 1000.0 B/s" in out
    assert "▼ 2.9 KB/s" in out


def test_network_rate_survives_a_repeated_snapshot():
    """A forced refresh inside the collector's cache TTL returns the same
    snapshot again; that used to render every interface as 0 B/s."""
    from sentinella.tui.widgets.network_widget import NetworkWidget

    second = _net_snap(101.0, 1000, 3000)
    out = _render(NetworkWidget(), [_net_snap(100.0, 0, 0), second, second])
    assert "▲ 1000.0 B/s" in out


def test_sparkline_history_skips_a_repeated_snapshot():
    from sentinella.core.models import CpuMetrics, MemoryMetrics
    from sentinella.tui.widgets.cpu_widget import CpuWidget
    from sentinella.tui.widgets.memory_widget import MemoryWidget

    snap = SystemSnapshot(
        timestamp=5.0, cpu=CpuMetrics(percent_overall=1.0), memory=MemoryMetrics(percent=1.0)
    )
    cpu, mem = CpuWidget(), MemoryWidget()
    _render(cpu, [snap, snap])
    _render(mem, [snap, snap])
    assert len(cpu._history) == 1
    assert len(mem._history) == 1


def test_fetch_art_keeps_its_first_line_indented():
    """str.strip() also ate the first row's leading spaces, shearing the logo."""
    from sentinella.fetch import _art_lines

    lines = _art_lines()
    assert lines[0].startswith(" "), lines[0]
    assert all(line.strip() for line in lines)


def test_user_lists_keep_first_seen_order():
    from sentinella.core.models import UserInfo
    from sentinella.tui.widgets.sensor_widget import SensorWidget

    names = ["zed", "amy", "zed", "mo"]
    snap = SystemSnapshot(users=[UserInfo(name=n) for n in names])
    out = _render(SensorWidget(), [snap])
    assert "zed, amy, mo" in out
