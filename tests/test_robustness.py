"""Shutdown, fan-out, startup cost and failure reporting."""

import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest

from sentinella.config import RemoteConfig, SentinellaConfig, WebConfig
from sentinella.core.constants import MODULE_NAMES
from sentinella.core.models import NetworkMetrics, SystemSnapshot
from sentinella.plugins.network import NetworkPlugin
from sentinella.remote.client import RemoteCollector
from sentinella.server.broadcast import BroadcastHub
from sentinella.server.runner import start_background_servers

REPO = Path(__file__).resolve().parent.parent


# ── Quitting must not wait on a stuck plugin ────────────────────────────────


def test_collector_close_does_not_wait_for_a_hung_plugin(make_local_collector, mock_config):
    """shutdown(wait=True) blocked on any plugin call still running — a
    disk_usage() on a wedged mount hung the TUI's quit indefinitely."""
    collector = make_local_collector(mock_config)
    release = threading.Event()
    collector._executor.submit(release.wait)
    # close() runs on its own thread so a regression fails here instead of
    # hanging the suite.
    closer = threading.Thread(target=collector.close, daemon=True)
    try:
        closer.start()
        closer.join(timeout=1.0)
        assert not closer.is_alive(), "close() is waiting on the hung plugin call"
    finally:
        release.set()
        closer.join(timeout=5)


# ── One stalled client must not hold up the others ──────────────────────────


class _Socket:
    def __init__(self, hang=False):
        self.hang = hang
        self.sent = []
        self.closed = None

    async def send_text(self, payload):
        if self.hang:
            await asyncio.sleep(3600)
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed = code


@pytest.mark.asyncio
async def test_stalled_clients_time_out_together_not_one_after_another():
    async def source():
        return {"n": 1}

    hub = BroadcastHub(snapshot_source=source, serializer=lambda s: s, interval=1, send_timeout=0.3)
    stalled = [_Socket(hang=True) for _ in range(4)]
    healthy = _Socket()
    for ws in (*stalled, healthy):
        hub.add(ws)

    started = time.monotonic()
    await hub.broadcast_once()
    elapsed = time.monotonic() - started

    # Serially that was 4 × 0.3 s before the frame went out to everyone.
    assert elapsed < 0.9
    assert healthy.sent == ['{"n": 1}']
    assert all(ws.closed == 1011 for ws in stalled)
    assert hub.clients == {healthy}


# ── Start-up cost of the one-shot commands ──────────────────────────────────


def _modules_loaded_by(code):
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        check=True,
    )
    return set(out.stdout.split())


def test_cli_import_does_not_pull_in_the_web_stack():
    """uvicorn alone was ~60% of the CLI's import time, paid by `fetch` and
    `print`, which never serve anything."""
    loaded = _modules_loaded_by(
        "import sys, sentinella.cli\n"
        "print(*[m for m in ('uvicorn', 'fastapi') if m in sys.modules])"
    )
    assert loaded == set()


def test_print_does_not_import_fastapi():
    """Validating module names imported core.api, and with it FastAPI."""
    loaded = _modules_loaded_by(
        "import sys, io, contextlib\n"
        "from sentinella.cli import main\n"
        "with contextlib.redirect_stdout(io.StringIO()):\n"
        "    main(['print', 'cpu', 'memory'])\n"
        "print(*[m for m in ('uvicorn', 'fastapi') if m in sys.modules])"
    )
    assert loaded == set()


# ── Background servers that cannot start must say so ───────────────────────


def _occupied_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock, sock.getsockname()[1]


def test_background_server_on_a_busy_port_is_reported_not_started(make_collector, dummy_snapshot):
    """uvicorn exits inside its daemon thread when the port is taken, so the
    TUI came up claiming nothing and the dashboard simply never answered."""
    sock, port = _occupied_port()
    try:
        cfg = SentinellaConfig(web=WebConfig(enabled=True, port=port))
        with patch("sentinella.server.runner.uvicorn.run") as run:
            failures = start_background_servers(cfg, collector=make_collector(dummy_snapshot))
    finally:
        sock.close()
    run.assert_not_called()
    assert len(failures) == 1
    assert "Web dashboard" in failures[0]
    assert str(port) in failures[0]


def test_one_failed_server_does_not_stop_the_other(make_collector, dummy_snapshot):
    sock, port = _occupied_port()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    free_port = free.getsockname()[1]
    free.close()
    try:
        cfg = SentinellaConfig(
            web=WebConfig(enabled=True, port=port),
            remote=RemoteConfig(enabled=True, port=free_port),
        )
        with (
            patch("sentinella.server.runner.threading.Thread") as thread,
            patch("sentinella.server.runner.uvicorn.run"),
        ):
            failures = start_background_servers(cfg, collector=make_collector(dummy_snapshot))
    finally:
        sock.close()
    assert len(failures) == 1
    assert "Web dashboard" in failures[0]  # the remote agent still started
    assert thread.call_count == 1


def test_background_server_crash_is_logged(caplog):
    from sentinella.server.runner import _run_server

    with patch("sentinella.server.runner.uvicorn.run", side_effect=SystemExit(1)):
        _run_server(MagicMock(), {"host": "127.0.0.1", "port": 1}, "Web dashboard")
    assert "Web dashboard" in caplog.text


@pytest.mark.asyncio
async def test_tui_shows_startup_warnings(mock_collector, mock_config):
    from sentinella.tui.app import SentinellaApp

    app = SentinellaApp(
        collector=mock_collector,
        config=mock_config,
        startup_warnings=["Web dashboard could not start on 127.0.0.1:8080: in use"],
    )
    shown = []
    app.notify = lambda message, **kw: shown.append((message, kw.get("severity")))
    async with app.run_test():
        pass
    assert ("Web dashboard could not start on 127.0.0.1:8080: in use", "error") in shown


def test_tui_command_passes_server_failures_to_the_app():
    from sentinella.cli import main

    with (
        patch("sentinella.server.runner.start_background_servers", return_value=["boom"]),
        patch("sentinella.core.collector.Collector"),
        patch("sentinella.tui.app.SentinellaApp") as app_cls,
    ):
        main([])
    assert app_cls.call_args.kwargs["startup_warnings"] == ["boom"]


# ── "Not permitted" is not "zero connections" ───────────────────────────────


def _net_plugin_with(conns_side_effect):
    with (
        patch("psutil.net_io_counters", return_value={}),
        patch("psutil.net_if_addrs", return_value={}),
        patch("psutil.net_connections", side_effect=conns_side_effect),
    ):
        return NetworkPlugin().collect()


def test_denied_connection_count_is_unknown_not_zero():
    """macOS refuses net_connections() to non-root users; that was shown as
    "Connections: 0", a plausible-looking and wrong reading."""
    assert _net_plugin_with(psutil.AccessDenied()).connections_count is None


def test_connection_count_is_still_reported_when_permitted():
    assert _net_plugin_with(lambda kind: [1, 2]).connections_count == 2


def test_unknown_connection_count_renders_as_a_dash():
    from sentinella.export.text_export import TextExporter

    snap = SystemSnapshot(network=NetworkMetrics(connections_count=None))
    assert "Connections: —" in TextExporter().format(snap, ["network"])


def test_unknown_connection_count_survives_the_remote_round_trip():
    snap = RemoteCollector._parse({"network": {"interfaces": [], "connections_count": None}})
    assert snap.network.connections_count is None
    # An older agent that omits the field still reads as a count of 0.
    assert RemoteCollector._parse({"network": {"interfaces": []}}).network.connections_count == 0


def test_network_widget_renders_unknown_connections_as_a_dash():
    from sentinella.tui.widgets.network_widget import NetworkWidget

    widget = NetworkWidget()
    captured = {}
    with patch.object(NetworkWidget, "update", lambda self, t: captured.setdefault("t", t)):
        widget.update_data(SystemSnapshot(network=NetworkMetrics(connections_count=None)))
    assert "Connections: —" in captured["t"].plain


# ── An unreachable agent is not asked on every property access ──────────────


def test_failed_module_probe_is_cached_for_a_while():
    """active_modules is a property; while the agent was down every read made
    a blocking HTTP request with a 10 s timeout."""
    remote = RemoteCollector("127.0.0.1:9")
    client = MagicMock()
    client.get.side_effect = OSError("unreachable")
    remote._client = client
    clock = [1000.0]
    with patch("sentinella.remote.client.time.monotonic", side_effect=lambda: clock[0]):
        # Unreachable = assume everything is served, as before.
        assert remote.active_modules == MODULE_NAMES
        assert remote.active_modules == MODULE_NAMES
        assert client.get.call_count == 1
        clock[0] += 31
        assert remote.active_modules == MODULE_NAMES
        assert client.get.call_count == 2
    remote._client = None
    remote.close()


# ── Per-call waste ──────────────────────────────────────────────────────────


def test_cpu_core_counts_are_read_once():
    from sentinella.plugins.cpu import CpuPlugin

    with patch("psutil.cpu_count", return_value=8) as count:
        plugin = CpuPlugin()
        calls_at_start = count.call_count
        plugin.collect()
        plugin.collect()
    assert count.call_count == calls_at_start


# ── Exiting past a plugin call that never returns ───────────────────────────


def _collector_with_a_stuck_call(make_local_collector, mock_config):
    collector = make_local_collector(mock_config)
    release = threading.Event()
    collector._track(collector._executor.submit(release.wait))
    return collector, release


def test_close_records_calls_it_had_to_abandon(make_local_collector, mock_config):
    from sentinella.core.collector import abandoned_calls

    collector, release = _collector_with_a_stuck_call(make_local_collector, mock_config)
    try:
        assert abandoned_calls() == []  # still owned by a live collector
        collector.close()
        assert len(abandoned_calls()) == 1
    finally:
        release.set()
    time.sleep(0.05)
    assert abandoned_calls() == []  # finished calls drop out


def test_exit_does_not_wait_for_a_stuck_plugin_call(make_local_collector, mock_config, capsys):
    """Interpreter shutdown joins every ThreadPoolExecutor worker, so even
    after close() stopped waiting, a stuck plugin call kept the process alive
    after the TUI had quit."""
    from sentinella.cli import _exit_past_stuck_calls

    collector, release = _collector_with_a_stuck_call(make_local_collector, mock_config)
    collector.close()
    try:
        with patch("sentinella.cli.os._exit") as hard_exit:
            _exit_past_stuck_calls(grace=0.2)
        hard_exit.assert_called_once_with(0)
        assert "still stuck" in capsys.readouterr().err
    finally:
        release.set()


def test_exit_is_normal_when_abandoned_calls_finish_in_time(make_local_collector, mock_config):
    from sentinella.cli import _exit_past_stuck_calls

    collector, release = _collector_with_a_stuck_call(make_local_collector, mock_config)
    collector.close()
    threading.Timer(0.05, release.set).start()
    with patch("sentinella.cli.os._exit") as hard_exit:
        _exit_past_stuck_calls(grace=2.0)
    hard_exit.assert_not_called()


def test_idle_workers_of_a_live_collector_are_not_stuck(make_local_collector, mock_config):
    """Only calls abandoned by close() count — an open collector's idle
    threads are alive too, and must never trigger a hard exit."""
    from sentinella.cli import _exit_past_stuck_calls

    collector = make_local_collector(mock_config)
    collector.collect()
    with patch("sentinella.cli.os._exit") as hard_exit:
        _exit_past_stuck_calls(grace=0.2)
    hard_exit.assert_not_called()


def test_main_checks_for_stuck_calls_after_a_command():
    from sentinella.cli import main

    with (
        patch("sentinella.cli._cmd_print"),
        patch("sentinella.cli._exit_past_stuck_calls") as check,
    ):
        main(["print"])
    check.assert_called_once()
