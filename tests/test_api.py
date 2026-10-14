import json
import time

import pytest
from loopback_client import TestClient

from sentinella.config import (
    GeneralConfig,
    ModulesConfig,
    RemoteConfig,
    SentinellaConfig,
    WebConfig,
)
from sentinella.core.collector import Collector
from sentinella.core.models import SystemSnapshot
from sentinella.remote.client import RemoteCollector
from sentinella.remote.server import create_remote_app
from sentinella.web.server import create_app


@pytest.fixture
def api_client(mock_config):
    # Set up config with a mock API key
    cfg = SentinellaConfig(web=WebConfig(api_key="secret-key", host="127.0.0.1", port=8080))
    app = create_app(cfg)
    return TestClient(app)


def test_api_health_check(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_api_static_skip_auth(api_client):
    response = api_client.get("/")
    assert response.status_code == 200
    assert "Sentinella" in response.text


def test_api_auth_required_without_key(api_client):
    response = api_client.get("/api/v1/snapshot")
    assert response.status_code == 401
    assert "Missing API key" in response.json()["detail"]


def test_api_auth_required_invalid_key(api_client):
    response = api_client.get("/api/v1/snapshot", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert "Invalid API key" in response.json()["detail"]


def test_api_auth_success(api_client):
    response = api_client.get("/api/v1/snapshot", headers={"X-API-Key": "secret-key"})
    assert response.status_code == 200
    data = response.json()
    assert "timestamp" in data
    assert "cpu" in data


def test_api_module_endpoint(api_client):
    # Valid module
    response = api_client.get("/api/v1/cpu", headers={"X-API-Key": "secret-key"})
    assert response.status_code == 200
    res = response.json()
    assert res["module"] == "cpu"
    assert "percent_overall" in res["data"]

    # Invalid module
    response = api_client.get("/api/v1/invalid_module", headers={"X-API-Key": "secret-key"})
    assert response.status_code == 404
    assert "Unknown module" in response.json()["detail"]


def test_api_static_files(api_client):
    response = api_client.get("/static/style.css")
    assert response.status_code == 200
    assert "body" in response.text


def test_websocket_auth_required_without_key(api_client):
    from fastapi.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with api_client.websocket_connect("/ws/live") as websocket:
            websocket.send_text("wrong-key")
            websocket.receive_json()
    assert exc_info.value.code == 4001


def test_websocket_auth_success(api_client):
    with api_client.websocket_connect("/ws/live") as websocket:
        websocket.send_text("secret-key")
        data = websocket.receive_json()
        assert "timestamp" in data
        assert "cpu" in data


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


# ── S5: WebSocket client cap ─────────────────────────────────────────────────


def test_websocket_client_limit_is_enforced(monkeypatch):
    import sentinella.server.api as api_module

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


# ── Phase 1 (C2): one bad cycle must not kill the broadcast loop ─────────────


def test_broadcast_loop_survives_a_failing_cycle():
    """``while True`` used to sit inside the ``try``, so a single exception
    from ``collect_async()`` ended the broadcast loop for the lifetime of the
    app — every browser kept an open socket, a "Live" badge, and no further
    data.  A failed cycle must be survivable."""
    from loopback_client import TestClient

    from sentinella.config import SentinellaConfig
    from sentinella.server.api import create_base_app

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
    last_collected_at = 1.0

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


def _app_for(snap, max_display=25, expose_cmdline=False):
    from sentinella.config import ProcessesConfig
    from sentinella.server.api import create_base_app

    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=max_display))
    return create_base_app(
        config=cfg,
        collector=_FixedCollector(snap),
        title="t",
        description="d",
        owns_collector=False,
        expose_cmdline=expose_cmdline,
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
    """`sentinella print --remote` must export exactly what a local run would —
    command lines included, once the agent opts in with expose_cmdline (they
    are withheld by default; see test_security.py)."""
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap, expose_cmdline=True)) as client:
        body = client.get("/api/v1/snapshot?full=true").json()

    assert len(body["processes"]) == 60
    assert all(p["cmdline"] for p in body["processes"])
    assert body["process_count"] == 999


def test_trimming_actually_shrinks_the_payload():
    snap = _snapshot_with_processes()
    with TestClient(_app_for(snap, expose_cmdline=True)) as client:
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
    with TestClient(_app_for(snap, expose_cmdline=True)) as client:
        trimmed = client.get("/api/v1/processes").json()["data"]
        full = client.get("/api/v1/processes?full=true").json()["data"]
    assert len(trimmed) == 25 and all("cmdline" not in p for p in trimmed)
    assert len(full) == 60 and all(p["cmdline"] for p in full)


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

    from sentinella.server.api import create_base_app

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


def test_module_responses_are_timestamped_by_the_collector():
    """The timestamp comes from the collector's public last_collected_at."""
    snap = SystemSnapshot(timestamp=5.0, processes=[])
    with TestClient(_app_for(snap)) as client:
        body = client.get("/api/v1/cpu").json()
    assert body["timestamp"] == _FixedCollector.last_collected_at
