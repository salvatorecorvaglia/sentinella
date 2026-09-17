"""Regression tests for the bugs found in the v1.0.0 audit.

Each test is named for the audit finding it pins down, so a future change that
reintroduces one of these fails loudly rather than silently.
"""

import asyncio
import json
import threading
import time
import weakref
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from sentinella.cli import main
from sentinella.config import (
    GeneralConfig,
    ModulesConfig,
    RemoteConfig,
    SentinellaConfig,
    WebConfig,
    _dict_to_config,
)
from sentinella.core.collector import Collector
from sentinella.core.models import SystemSnapshot
from sentinella.remote.client import RemoteCollector
from sentinella.remote.server import create_remote_app
from sentinella.web.server import create_app

# ── C1: non-ASCII API keys must 401, not 500 ─────────────────────────────────


def test_non_ascii_header_value_is_rejected_not_crashed():
    """Starlette decodes headers as latin-1, so a non-ASCII key can reach the
    middleware over the wire. hmac.compare_digest would raise TypeError on it."""
    cfg = SentinellaConfig(web=WebConfig(api_key="secret-key"))
    with TestClient(create_app(cfg)) as client:
        # Sent as raw bytes: httpx refuses to ASCII-encode a non-ASCII str.
        resp = client.get("/api/v1/snapshot", headers={"X-API-Key": "wrong-clé".encode("latin-1")})
    assert resp.status_code == 401


def test_non_ascii_configured_key_does_not_crash_every_request():
    """A server configured with an emoji key must still answer 401, not 500 —
    the configured side of compare_digest is what raised TypeError."""
    cfg = SentinellaConfig(web=WebConfig(api_key="chiave-segreta-🔑"))
    with TestClient(create_app(cfg)) as client:
        resp = client.get("/api/v1/snapshot", headers={"X-API-Key": "guess"})
    assert resp.status_code == 401


def test_non_ascii_key_over_websocket():
    key = "chiave-🔑"
    cfg = SentinellaConfig(web=WebConfig(api_key=key))
    with TestClient(create_app(cfg)) as client, client.websocket_connect("/ws/live") as ws:
        ws.send_text(key)
        assert "cpu" in ws.receive_json()


# ── C2: a shared collector must survive server shutdown ──────────────────────


def test_injected_collector_survives_app_shutdown(mock_config):
    """The TUI hands its collector to background servers; shutting the server
    down must not tear down the collector the TUI is still using."""
    collector = Collector(mock_config)
    try:
        with TestClient(create_app(mock_config, collector=collector)):
            pass  # enter + exit runs the full lifespan
        # Still usable after the app has shut down.
        assert collector.collect() is not None
    finally:
        collector.close()


def test_owned_collector_is_closed_on_shutdown(mock_config):
    """Conversely, a collector the app created is its own responsibility."""
    collector = Collector(mock_config)
    with TestClient(create_app(mock_config, collector=collector)):
        pass
    assert collector.collect() is not None  # shared: still alive
    collector.close()

    # An app that built its own collector shuts that collector's pool down.
    app = create_app(mock_config)
    owned = _collector_of(app)
    with TestClient(app):
        pass
    with pytest.raises(RuntimeError):
        owned._executor.submit(lambda: None)


def _collector_of(app):
    """Dig the collector out of the app's snapshot route closure."""
    for route in app.routes:
        closure = getattr(getattr(route, "endpoint", None), "__closure__", None) or ()
        for cell in closure:
            if isinstance(cell.cell_contents, Collector):
                return cell.cell_contents
    raise AssertionError("no Collector found on app")


# ── C3: --host must reach the app, not just uvicorn ──────────────────────────


def test_cli_host_override_reaches_open_bind_warning():
    """`sentinella web --host 0.0.0.0` with no API key must warn."""
    from sentinella.cli import main

    cfg = SentinellaConfig(web=WebConfig(host="127.0.0.1", api_key=""))
    with (
        patch("sentinella.cli.load_config", return_value=cfg),
        patch("sentinella.cli.uvicorn.run"),
        patch("sentinella.core.api.warn_open_bind") as mock_warn,
        patch("sentinella.web.server.warn_open_bind") as mock_warn_web,
    ):
        main(["web", "--host", "0.0.0.0"])

    called = mock_warn_web.call_args or mock_warn.call_args
    assert called is not None, "warn_open_bind was never called"
    assert called.args[0] == "0.0.0.0", "warning saw the config host, not the CLI override"


def test_cli_host_override_is_what_uvicorn_binds():
    from sentinella.cli import main

    cfg = SentinellaConfig(web=WebConfig(host="127.0.0.1", port=8080, api_key=""))
    with (
        patch("sentinella.cli.load_config", return_value=cfg),
        patch("sentinella.cli.uvicorn.run") as mock_run,
    ):
        main(["web", "--host", "0.0.0.0", "--port", "9999"])
    assert mock_run.call_args.kwargs["host"] == "0.0.0.0"
    assert mock_run.call_args.kwargs["port"] == 9999


# ── C4: disabled modules must not report zeros ───────────────────────────────


def test_disabled_module_is_404_not_fake_zero():
    """A disabled CPU module must not be indistinguishable from an idle CPU."""
    cfg = SentinellaConfig(modules=ModulesConfig(cpu=False))
    with TestClient(create_app(cfg)) as client:
        resp = client.get("/api/v1/cpu")
    assert resp.status_code == 404
    assert "not enabled" in resp.json()["detail"]


def test_enabled_module_still_serves_data():
    cfg = SentinellaConfig(modules=ModulesConfig(cpu=True))
    with TestClient(create_app(cfg)) as client:
        resp = client.get("/api/v1/cpu")
    assert resp.status_code == 200
    assert "percent_overall" in resp.json()["data"]


def test_health_advertises_active_modules():
    cfg = SentinellaConfig(modules=ModulesConfig(cpu=False))
    with TestClient(create_app(cfg)) as client:
        body = client.get("/health").json()
    assert "cpu" not in body["active_modules"]
    assert "memory" in body["active_modules"]


def test_collector_active_modules_excludes_disabled(mock_config):
    cfg = SentinellaConfig(modules=ModulesConfig(cpu=False, memory=True))
    collector = Collector(cfg)
    try:
        assert "cpu" not in collector.active_modules
        assert "memory" in collector.active_modules
        assert collector.collect_module("cpu") is None
    finally:
        collector.close()


# ── C5: RemoteCollector must not leak its async client ───────────────────────


@pytest.mark.asyncio
async def test_remote_collector_close_async_releases_async_client():
    client = RemoteCollector("http://localhost:9090")
    await client._get_async_client()
    assert client._async_client is not None
    await client.close_async()
    assert client._async_client is None


def test_remote_collector_sync_close_also_releases_async_client():
    """The TUI drives collect_async but tears down via close()."""
    import asyncio

    client = RemoteCollector("http://localhost:9090")
    asyncio.run(client._get_async_client())
    assert client._async_client is not None
    client.close()
    assert client._async_client is None


def test_remote_collector_close_is_idempotent():
    client = RemoteCollector("http://localhost:9090")
    client.close()
    client.close()


@pytest.mark.asyncio
async def test_remote_collector_close_from_running_loop_still_releases_client():
    """close() called synchronously from inside a running event loop used to
    drop the AsyncClient reference without ever calling aclose() on it — a
    real connection-pool leak. It must now schedule the cleanup instead."""
    client = RemoteCollector("http://localhost:9090")
    await client._get_async_client()
    async_client = client._async_client
    assert async_client is not None

    client.close()  # called from within this running loop
    assert client._async_client is None
    assert client._pending_close_tasks  # cleanup was scheduled, not dropped

    await asyncio.gather(*client._pending_close_tasks)
    assert async_client.is_closed


# ── C6: container plugin must stay loaded when Docker is down ────────────────


def test_container_plugin_stays_loaded_without_runtime():
    from sentinella.core.plugin_manager import get_enabled_plugins

    with patch("shutil.which", return_value=None), patch.dict("sys.modules", {"docker": None}):
        plugins = get_enabled_plugins(SentinellaConfig(modules=ModulesConfig(containers=True)))
    assert "containers" in {p.name for p in plugins}


def test_container_plugin_respects_config_disable():
    from sentinella.core.plugin_manager import get_enabled_plugins

    plugins = get_enabled_plugins(SentinellaConfig(modules=ModulesConfig(containers=False)))
    assert "containers" not in {p.name for p in plugins}


# ── M6: config errors are user errors, not tracebacks ────────────────────────


def test_missing_config_file_exits_cleanly(capsys):
    from sentinella.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["-c", "/nonexistent/sentinella.toml", "fetch"])
    assert exc.value.code == 2
    assert "Config file not found" in capsys.readouterr().err


def test_invalid_config_value_exits_cleanly(tmp_path, capsys):
    from sentinella.cli import main

    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[general]\nrefresh_interval = 0\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["-c", str(cfg_file), "fetch"])
    assert exc.value.code == 2
    assert "refresh_interval" in capsys.readouterr().err


def test_malformed_toml_exits_cleanly(tmp_path, capsys):
    from sentinella.cli import main

    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[general\nthis is not toml", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["-c", str(cfg_file), "fetch"])
    assert exc.value.code == 2


def test_wrong_type_names_the_offending_key():
    """A string port must be reported by name, not as a bare TypeError."""
    with pytest.raises(ValueError, match="web.port must be an integer"):
        _dict_to_config({"web": {"port": "8080"}})


def test_wrong_type_for_bool_module_flag():
    with pytest.raises(ValueError, match="modules.cpu must be a boolean"):
        _dict_to_config({"modules": {"cpu": "yes"}})


def test_float_refresh_interval_is_accepted():
    cfg = _dict_to_config({"general": {"refresh_interval": 2.5}})
    assert cfg.general.refresh_interval == 2.5


# ── M7: global flags work on either side of the subcommand ───────────────────


def test_global_flags_accepted_after_subcommand(tmp_path):
    from sentinella.cli import _build_parser

    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[general]\nrefresh_interval = 3\n", encoding="utf-8")
    parser = _build_parser()

    after = parser.parse_args(["print", "-c", str(cfg_file)])
    before = parser.parse_args(["-c", str(cfg_file), "print"])
    assert after.config == before.config == str(cfg_file)


def test_subcommand_does_not_clobber_root_global_flag(tmp_path):
    """argparse parents= would reset --config to None without SUPPRESS."""
    from sentinella.cli import _build_parser

    args = _build_parser().parse_args(["-c", "root.toml", "print"])
    assert args.config == "root.toml"


# ── S5: WebSocket client cap ─────────────────────────────────────────────────


def test_websocket_client_limit_is_enforced(monkeypatch):
    import sentinella.core.api as api_module

    monkeypatch.setattr(api_module, "MAX_WEBSOCKET_CLIENTS", 1)
    cfg = SentinellaConfig(web=WebConfig(api_key=""))
    with TestClient(create_app(cfg)) as client:
        with client.websocket_connect("/ws/live") as first:
            first.send_text("")
            first.receive_json()
            with pytest.raises(Exception):  # noqa: B017 - close code varies by transport
                with client.websocket_connect("/ws/live") as second:
                    second.receive_json()


# ── Remote agent parity ──────────────────────────────────────────────────────


def test_remote_agent_health_and_auth():
    cfg = SentinellaConfig(remote=RemoteConfig(api_key="agent-key"))
    with TestClient(create_remote_app(cfg)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/snapshot").status_code == 401
        assert client.get("/api/v1/snapshot", headers={"X-API-Key": "agent-key"}).status_code == 200


def test_general_config_defaults_are_valid():
    cfg = GeneralConfig()
    assert cfg.refresh_interval >= 1
    assert cfg.theme in ("dark", "light")


# ── P2-6: a hung plugin must not be resubmitted every cycle ──────────────────


def test_collector_does_not_resubmit_a_still_running_plugin(mock_config):
    """Resubmitting a plugin whose previous call hasn't returned is what
    exhausts the thread pool when a plugin hangs — repeated cycles would each
    permanently claim another worker on top of the ones already stuck. The
    same in-flight future must be reused across cycles instead."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    class SlowPlugin:
        name = "cpu"
        category = "cpu"

        def collect(self):
            calls.append(1)
            started.set()
            release.wait(timeout=5)
            from sentinella.core.models import CpuMetrics

            return CpuMetrics()

        def close(self):
            pass

    collector = Collector(mock_config)
    collector.plugins = [SlowPlugin()]
    try:
        with patch("sentinella.core.collector.as_completed", side_effect=TimeoutError):
            collector.collect()
        assert started.wait(timeout=2), "plugin.collect() was never invoked"
        assert "cpu" in collector._inflight
        assert not collector._inflight["cpu"].done()

        collector._last_collected_at = 0.0  # force past the TTL cache
        with patch("sentinella.core.collector.as_completed", side_effect=TimeoutError):
            collector.collect()

        assert len(calls) == 1, "hung plugin was resubmitted instead of reused"
    finally:
        release.set()
        collector.close()


# ── P2-5: TUI sort cycling must re-truncate, not just re-sort ────────────────


def test_collector_collect_processes_delegates_to_processes_plugin(mock_config):
    collector = Collector(mock_config)
    try:
        result = collector.collect_processes("pid")
        assert isinstance(result, list)
    finally:
        collector.close()


@pytest.mark.asyncio
async def test_collector_collect_processes_async(mock_config):
    collector = Collector(mock_config)
    try:
        result = await collector.collect_processes_async("memory")
        assert isinstance(result, list)
    finally:
        collector.close()


def test_collector_collect_processes_without_processes_plugin_returns_empty(mock_config):
    collector = Collector(mock_config)
    collector.plugins = []
    try:
        assert collector.collect_processes("cpu") == []
    finally:
        collector.close()


# ── P2-8: container stats pool is reused, not rebuilt every cycle ────────────


def test_containers_plugin_reuses_stats_executor_across_cycles():
    from unittest.mock import MagicMock

    from sentinella.plugins.containers import ContainersPlugin

    mock_docker = MagicMock()
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.name = "c1"
    mock_container.short_id = "abc123"
    mock_container.status = "running"
    mock_container.attrs = {"Config": {"Image": "nginx"}}
    mock_container.stats.return_value = {
        "cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 2000},
        "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
        "memory_stats": {"usage": 1000, "stats": {"cache": 0}},
    }
    mock_client.containers.list.return_value = [mock_container]
    mock_docker.from_env.return_value = mock_client

    class ModulesStub:
        container_stats = True

    class ConfigStub:
        modules = ModulesStub()

    with (
        patch("shutil.which", return_value=None),
        patch.dict("sys.modules", {"docker": mock_docker}),
    ):
        plugin = ContainersPlugin(config=ConfigStub())
        plugin.collect()
        first_pool = plugin._stats_executor
        assert first_pool is not None

        plugin._last_check = 0.0  # force a fresh availability + stats round
        plugin.collect()
        assert plugin._stats_executor is first_pool, "a new pool was built for the same cycle type"

        plugin.close()
        assert plugin._stats_executor is None


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


# ── Phase 1 (C1): the collector must sample every cycle, not every other ─────


def test_collector_samples_every_cycle(mock_config):
    """A completed future must not be re-harvested on the next cycle.

    ``_inflight`` was only cleared on the cycle *after* a plugin finished, so
    cycle N+1 read cycle N's result instead of submitting a fresh call: every
    surface refreshed at half its configured rate and showed each reading
    twice.  Line coverage never caught it — the bug lives on covered lines and
    ``_assemble`` mints a new snapshot object either way.
    """
    from sentinella.core.models import CpuMetrics

    class CountingPlugin:
        name = "cpu"
        category = "cpu"

        def __init__(self) -> None:
            self.calls = 0

        def collect(self):
            self.calls += 1
            return CpuMetrics(percent_overall=float(self.calls))

        def close(self):
            pass

    plugin = CountingPlugin()
    collector = Collector(mock_config)
    collector.plugins = [plugin]
    try:
        seen = []
        for _ in range(4):
            collector._last_collected_at = 0.0  # force past the TTL cache
            seen.append(collector.collect().cpu.percent_overall)

        assert seen == [1.0, 2.0, 3.0, 4.0], f"stale readings replayed: {seen}"
        assert plugin.calls == 4
        # A finished future must not be pinned between cycles.
        assert not collector._inflight
    finally:
        collector.close()


# ── Phase 1 (C2): one bad cycle must not kill the broadcast loop ─────────────


def test_broadcast_loop_survives_a_failing_cycle():
    """``while True`` used to sit inside the ``try``, so a single exception
    from ``collect_async()`` ended the broadcast loop for the lifetime of the
    app — every browser kept an open socket, a "Live" badge, and no further
    data.  A failed cycle must be survivable."""
    from fastapi.testclient import TestClient

    from sentinella.config import GeneralConfig, SentinellaConfig
    from sentinella.core.api import create_base_app

    calls = {"n": 0}

    class FlakyCollector:
        active_modules = frozenset({"cpu"})
        _last_collected_at = 0.0

        def collect(self):  # pragma: no cover - async path only
            raise AssertionError("unused")

        async def collect_async(self):
            calls["n"] += 1
            # 1 = the endpoint's own initial snapshot; 2 = first loop cycle.
            if calls["n"] == 2:
                raise RuntimeError("transient collection failure")
            return SystemSnapshot(timestamp=float(calls["n"]))

        def close(self):
            pass

        async def close_async(self):
            pass

    cfg = SentinellaConfig(general=GeneralConfig(refresh_interval=1.0))
    cfg.general.refresh_interval = 0.05  # tick fast; validate_config isn't rerun
    app = create_base_app(
        config=cfg,
        collector=FlakyCollector(),
        title="t",
        description="d",
        owns_collector=False,
    )

    with TestClient(app) as client, client.websocket_connect("/ws/live") as ws:
        first = json.loads(ws.receive_text())  # endpoint's initial snapshot
        assert first["timestamp"] == 1.0
        # Cycle 2 raises. Poll rather than block on receive_text(): if the loop
        # dies the socket simply goes quiet, and a blocking read would hang the
        # suite instead of failing it.
        deadline = time.monotonic() + 5.0
        while calls["n"] < 3 and time.monotonic() < deadline:
            time.sleep(0.05)

    assert calls["n"] >= 3, "broadcast loop stopped after the failing cycle"


# ── Phase 1 (C3/C4): the CLI must not silently do the wrong thing ────────────


def test_fetch_honours_remote_flag(monkeypatch):
    """``--remote`` is advertised on every subparser, but ``fetch`` always
    built a local Collector — so ``sentinella --remote host fetch`` summarised the
    *local* machine with no indication anything was ignored."""
    seen = {}

    class FakeRemote:
        def __init__(self, address, api_key=""):
            seen["address"] = address
            seen["api_key"] = api_key

        def collect(self):
            return SystemSnapshot()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr("sentinella.remote.client.RemoteCollector", FakeRemote)
    monkeypatch.setattr("sentinella.fetch.Console", lambda *a, **k: _SilentConsole())

    main(["--remote", "10.0.0.5:9090", "fetch"])

    assert seen["address"] == "10.0.0.5:9090"
    assert seen.get("closed"), "remote collector was not released"


class _SilentConsole:
    def print(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("command", ["web", "serve"])
def test_remote_with_a_server_command_is_rejected(command, capsys):
    """Silently ignoring --remote here meant `sentinella --remote host serve`
    started an agent reporting the *local* host."""
    with pytest.raises(SystemExit):
        main(["--remote", "10.0.0.5:9090", command])
    assert "--remote cannot be used" in capsys.readouterr().err


def test_print_rejects_an_unknown_module(capsys):
    """A typo used to exit 0 with empty output, reading as 'this host reports
    nothing' rather than 'you misspelled it'."""
    with pytest.raises(SystemExit):
        main(["print", "cpu", "bogusmodule"])
    err = capsys.readouterr().err
    assert "bogusmodule" in err
    assert "Valid modules" in err


def test_print_still_accepts_valid_modules(capsys):
    main(["print", "cpu", "--format", "json"])
    out = capsys.readouterr().out
    assert '"cpu"' in out


@pytest.mark.parametrize("command", ["fetch", "print"])
def test_unreachable_remote_is_a_user_error_not_a_traceback(command, capsys):
    """An agent that is down is a predictable user error. It used to surface as
    a raw httpx traceback; config errors have always been reported cleanly and
    this should match."""
    with pytest.raises(SystemExit):
        main(["--remote", "127.0.0.1:9099", command])
    err = capsys.readouterr().err
    assert "could not reach remote agent at 127.0.0.1:9099" in err
    assert "Traceback" not in err


def test_remote_auth_failure_names_the_api_key(monkeypatch, capsys):
    import httpx

    class Unauthorized:
        def __init__(self, address, api_key=""):
            pass

        def collect(self):
            request = httpx.Request("GET", "http://x/api/v1/snapshot")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401", request=request, response=response)

        def close(self):
            pass

    monkeypatch.setattr("sentinella.remote.client.RemoteCollector", Unauthorized)
    with pytest.raises(SystemExit):
        main(["--remote", "10.0.0.5:9090", "print"])
    assert "rejected the API key" in capsys.readouterr().err


# ── Phase 2 (S1): security headers must survive an auth failure ─────────────


def test_security_headers_are_present_on_a_401():
    """Starlette runs the last-registered middleware first, so the auth check
    short-circuited before the header middleware ever ran — every 401 shipped
    with no CSP, nosniff, X-Frame-Options or Referrer-Policy."""
    cfg = SentinellaConfig(web=WebConfig(api_key="secret"))
    with TestClient(create_app(cfg)) as client:
        resp = client.get("/api/v1/snapshot")
        assert resp.status_code == 401
        for header in (
            "Content-Security-Policy",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Referrer-Policy",
        ):
            assert header in resp.headers, f"{header} missing from a 401"


def test_security_headers_still_present_when_authorised():
    cfg = SentinellaConfig(web=WebConfig(api_key="secret"))
    with TestClient(create_app(cfg)) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert "Content-Security-Policy" in resp.headers


# ── Phase 2 (S2): every non-loopback bind must warn, not just "0.0.0.0" ─────


@pytest.mark.parametrize(
    "host,public",
    [
        ("0.0.0.0", True),
        ("::", True),
        ("[::]", True),
        ("", True),
        ("192.168.1.10", True),
        ("myhost.lan", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("localhost", False),
    ],
)
def test_open_bind_detection_covers_more_than_the_ipv4_wildcard(host, public):
    from sentinella.core.api import _is_public_bind

    assert _is_public_bind(host) is public


def test_open_bind_on_ipv6_wildcard_warns(capsys):
    from sentinella.core.api import warn_open_bind

    warn_open_bind("::", "", "Remote agent")
    assert "no API key" in capsys.readouterr().err


def test_open_bind_with_a_key_stays_quiet(capsys):
    from sentinella.core.api import warn_open_bind

    warn_open_bind("0.0.0.0", "a-key", "Remote agent")
    assert capsys.readouterr().err == ""


# ── Phase 2 (S4): plugin discovery must not pick up an imported class ───────


def test_plugin_discovery_ignores_imported_plugin_classes():
    """``dir(mod)`` is alphabetical, so a module importing a sibling plugin
    would silently load that one instead of its own."""
    import types

    from sentinella.core.plugin_manager import _load_plugin_module
    from sentinella.plugins.base import MonitorPlugin
    from sentinella.plugins.cpu import CpuPlugin

    module = types.ModuleType("sentinella.plugins.zzz_fake")

    class ActualPlugin(MonitorPlugin):
        name = "zzz_fake"

        def collect(self):
            return None

        def is_available(self):
            return True

    ActualPlugin.__module__ = "sentinella.plugins.zzz_fake"
    # "AaaImported" sorts before "ActualPlugin" — the old scan would win with it.
    module.AaaImported = CpuPlugin
    module.ActualPlugin = ActualPlugin

    with patch.dict("sys.modules", {"sentinella.plugins.zzz_fake": module}):
        assert _load_plugin_module("zzz_fake") is ActualPlugin


# ── Phase 3 (R1): the Docker client's connection pool must be released ──────


def test_container_plugin_close_releases_the_docker_client():
    """close() dropped the reference without closing it, leaking docker-py's
    HTTP connection pool for the life of the process."""
    from unittest.mock import MagicMock

    from sentinella.plugins.containers import ContainersPlugin

    mock_docker = MagicMock()
    client = MagicMock()
    mock_docker.from_env.return_value = client

    with (
        patch("shutil.which", return_value=None),
        patch.dict("sys.modules", {"docker": mock_docker}),
    ):
        plugin = ContainersPlugin()
        plugin.collect()
        assert plugin._docker_client is client
        plugin.close()

    client.close.assert_called_once()
    assert plugin._docker_client is None


def test_container_plugin_close_survives_a_failing_docker_close():
    from unittest.mock import MagicMock

    from sentinella.plugins.containers import ContainersPlugin

    plugin = ContainersPlugin()
    failing = MagicMock()
    failing.close.side_effect = RuntimeError("already gone")
    plugin._docker_client = failing

    plugin.close()  # must not propagate
    assert plugin._docker_client is None


# ── Phase 3 (R2): one atexit handler, not one per collector ────────────────


def test_collectors_do_not_accumulate_atexit_handlers(mock_config):
    """Each Collector used to register its own bound atexit handler, so a
    process that built collectors over time pinned every thread pool it had
    ever made."""
    from sentinella.core import collector as collector_mod

    before = len(collector_mod._LIVE_COLLECTORS)
    made = [Collector(mock_config) for _ in range(3)]
    assert len(collector_mod._LIVE_COLLECTORS) == before + 3

    for c in made:
        c.close()
    assert len(collector_mod._LIVE_COLLECTORS) == before


def test_a_dropped_collector_is_garbage_collectable(mock_config):
    import gc

    from sentinella.core import collector as collector_mod

    collector = Collector(mock_config)
    ref = weakref.ref(collector)
    collector.close()
    del collector
    gc.collect()
    assert ref() is None, "a closed collector is still pinned by the exit hook"
    assert len(collector_mod._LIVE_COLLECTORS) >= 0


# ── Phase 3 (R4): the process total must describe the list it came with ────


def test_process_total_travels_with_the_list(mock_config, make_local_collector):
    """total_count was written from a worker thread and read from the collector
    thread, so it could describe a different cycle than the list beside it."""
    from sentinella.plugins.processes import ProcessesPlugin

    listing = ProcessesPlugin(mock_config).collect()
    assert listing.total >= len(listing)
    assert isinstance(listing, list)  # every existing consumer still works

    collector = make_local_collector(mock_config)
    snap = collector.collect()
    assert snap.process_count >= len(snap.processes)


def test_process_count_falls_back_for_a_plain_list():
    """A plugin returning a bare list (a stub, an old third-party one) must
    still produce a sane count rather than raising."""
    snap = Collector._assemble(
        {"processes": [1, 2, 3]}, Collector._process_count({"processes": [1, 2, 3]})
    )
    assert snap.process_count == 3


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


# ── Phase 4 (P1): the streamed snapshot must not carry what nothing renders ──


def _snapshot_with_processes(n=60):
    from sentinella.core.models import ProcessInfo

    return SystemSnapshot(
        processes=[
            ProcessInfo(pid=i, name=f"p{i}", cmdline=f"/usr/bin/p{i} --flag=value{i}" * 4)
            for i in range(n)
        ],
        process_count=999,
    )


class _FixedCollector:
    active_modules = frozenset({"processes", "cpu"})
    _last_collected_at = 1.0

    def __init__(self, snap):
        self.snap = snap

    def collect(self):
        return self.snap

    async def collect_async(self):
        return self.snap

    async def collect_module_async(self, name):
        return getattr(self.snap, name, None)

    def close(self):
        pass

    async def close_async(self):
        pass


def _app_for(snap, max_display=25):
    from sentinella.config import ProcessesConfig
    from sentinella.core.api import create_base_app

    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=max_display))
    return create_base_app(
        config=cfg,
        collector=_FixedCollector(snap),
        title="t",
        description="d",
        owns_collector=False,
    )


def test_dashboard_snapshot_drops_cmdline_and_caps_the_list():
    """cmdline was ~70% of an 87 KB frame and no dashboard renders it; the list
    held 100 processes for a 25-row table."""
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap)) as client:
        body = client.get("/api/v1/snapshot").json()

    assert len(body["processes"]) == 25
    assert all("cmdline" not in p for p in body["processes"])
    # The host total must survive the trim, or the dashboard misreports it.
    assert body["process_count"] == 999


def test_full_snapshot_is_untouched_for_remote_clients():
    """`sentinella print --remote` must export exactly what a local run would."""
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap)) as client:
        body = client.get("/api/v1/snapshot?full=true").json()

    assert len(body["processes"]) == 60
    assert all(p["cmdline"] for p in body["processes"])
    assert body["process_count"] == 999


def test_trimming_actually_shrinks_the_payload():
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap)) as client:
        trimmed = len(client.get("/api/v1/snapshot").content)
        full = len(client.get("/api/v1/snapshot?full=true").content)
    assert trimmed < full / 3, f"expected a large reduction, got {trimmed} vs {full}"


def test_websocket_frames_are_trimmed_too():
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap)) as client, client.websocket_connect("/ws/live") as ws:
        frame = json.loads(ws.receive_text())
    assert len(frame["processes"]) == 25
    assert all("cmdline" not in p for p in frame["processes"])


def test_processes_module_endpoint_is_trimmed_but_full_is_available():
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap)) as client:
        trimmed = client.get("/api/v1/processes").json()["data"]
        full = client.get("/api/v1/processes?full=true").json()["data"]
    assert len(trimmed) == 25 and all("cmdline" not in p for p in trimmed)
    assert len(full) == 60 and all(p["cmdline"] for p in full)


def test_remote_collector_asks_for_the_full_snapshot():
    """Without ?full=true a remote `sentinella print` would silently export a
    truncated, cmdline-less list while a local run exported everything."""
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"timestamp": 1.0, "processes": []})

    collector = RemoteCollector("127.0.0.1:9090")
    collector._client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        collector.collect()
    finally:
        collector.close()
    assert "full=true" in seen["url"]


# ── Phase 4 (P2): cmdline is only gathered when something will show it ──────


def test_cmdline_is_skipped_unless_requested(mock_config):
    from sentinella.plugins.processes import ProcessesPlugin

    plugin = ProcessesPlugin(mock_config)
    assert all(not p.cmdline for p in plugin.collect())

    plugin.collect_cmdline = True
    assert any(p.cmdline for p in plugin.collect())


def test_collector_can_turn_cmdline_on_and_invalidates_its_cache(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)
    first = collector.collect()
    assert all(not p.cmdline for p in first.processes)

    collector.set_collect_cmdline(True)
    # The cached snapshot must be dropped, or the change wouldn't take effect
    # until the TTL happened to expire.
    assert collector._last_snapshot is None
    assert any(p.cmdline for p in collector.collect().processes)


# ── Phase 5 (A1): the protocol must describe what the API actually needs ────


def test_both_collectors_satisfy_the_protocol(mock_config, make_local_collector):
    """MetricCollector declared four methods while core.api needed six members
    plus a private attribute, so a RemoteCollector satisfied the protocol and
    still blew up on /api/v1/{module}."""
    from sentinella.core.protocols import MetricCollector

    local = make_local_collector(mock_config)
    remote = RemoteCollector("127.0.0.1:9090")
    try:
        for collector in (local, remote):
            assert isinstance(collector, MetricCollector)
            for member in (
                "collect",
                "collect_async",
                "collect_module_async",
                "close",
                "close_async",
                "active_modules",
                "last_collected_at",
            ):
                assert hasattr(collector, member), f"{collector!r} lacks {member}"
    finally:
        remote.close()


def test_a_remote_backed_server_can_serve_module_endpoints():
    """This is the combination that used to AttributeError."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"active_modules": ["cpu", "memory"]})
        if request.url.path == "/api/v1/cpu":
            return httpx.Response(
                200, json={"module": "cpu", "timestamp": 1.0, "data": {"percent_overall": 42.0}}
            )
        if request.url.path == "/api/v1/snapshot":
            return httpx.Response(200, json={"timestamp": 1.0, "processes": []})
        return httpx.Response(404, json={"detail": "nope"})

    transport = httpx.MockTransport(handler)
    remote = RemoteCollector("127.0.0.1:9090")
    remote._client = httpx.Client(transport=transport)
    remote._async_client = httpx.AsyncClient(transport=transport)

    from sentinella.core.api import create_base_app

    app = create_base_app(
        config=SentinellaConfig(),
        collector=remote,
        title="proxy",
        description="d",
        owns_collector=False,
    )
    try:
        with TestClient(app) as client:
            assert client.get("/health").json()["active_modules"] == ["cpu", "memory"]
            body = client.get("/api/v1/cpu").json()
            assert body["data"]["percent_overall"] == 42.0
            # A module the upstream agent doesn't serve is a 404, not a crash.
            assert client.get("/api/v1/disk").status_code == 404
    finally:
        remote.close()


def test_remote_collector_reports_unknown_modules_as_none():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not enabled"})

    remote = RemoteCollector("127.0.0.1:9090")
    remote._async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = asyncio.run(remote.collect_module_async("sensors"))
    finally:
        remote.close()
    assert result is None


def test_api_no_longer_reads_a_private_collector_attribute():
    source = (Path(__file__).parent.parent / "sentinella/core/api.py").read_text(encoding="utf-8")
    assert "_last_collected_at" not in source


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


def test_container_running_status_has_one_definition():
    from sentinella.core.models import ContainerInfo

    assert ContainerInfo(status="running").is_running
    assert ContainerInfo(status="up").is_running
    # LXC reports "Running"; the plugin lower-cases, but the model shouldn't
    # depend on that having happened.
    assert ContainerInfo(status="Running").is_running
    assert not ContainerInfo(status="exited").is_running
    assert not ContainerInfo(status="").is_running


def test_no_surface_spells_out_the_running_check_inline():
    root = Path(__file__).parent.parent
    for rel in (
        "sentinella/fetch.py",
        "sentinella/tui/widgets/container_widget.py",
    ):
        source = (root / rel).read_text(encoding="utf-8")
        assert '"running", "up"' not in source, f"{rel} still inlines the status test"
