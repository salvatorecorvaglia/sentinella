"""Browser-facing and transport hardening for the web and remote servers."""

import json
import logging
import os
import stat
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient as RawTestClient
from loopback_client import TestClient
from starlette.websockets import WebSocketDisconnect

from sentinella.cli import main
from sentinella.config import (
    RemoteConfig,
    SentinellaConfig,
    WebConfig,
    load_config,
    validate_config,
)
from sentinella.core.models import ProcessInfo, SystemSnapshot
from sentinella.remote.server import create_remote_app
from sentinella.server.api import keys_match
from sentinella.web.server import create_app

SECRET_CMDLINE = "backup --password hunter2"


class _Collector:
    """Serves one fixed snapshot; records what the app asked of it."""

    active_modules = frozenset({"processes", "cpu"})
    last_collected_at = 0.0

    def __init__(self, snapshot=None):
        self.snapshot = snapshot or SystemSnapshot(timestamp=1.0)
        self.cmdline_requested = None

    def collect(self):
        return self.snapshot

    async def collect_async(self):
        return self.snapshot

    async def collect_module_async(self, name):
        return getattr(self.snapshot, name, None)

    def set_collect_cmdline(self, enabled):
        self.cmdline_requested = enabled

    def close(self):
        pass

    async def close_async(self):
        pass


def _snapshot_with_cmdline():
    return SystemSnapshot(
        timestamp=1.0,
        processes=[ProcessInfo(pid=7, name="backup", cmdline=SECRET_CMDLINE)],
        process_count=1,
    )


# ── Cross-site WebSocket hijacking ──────────────────────────────────────────


def test_websocket_from_a_foreign_origin_is_refused():
    """WebSockets are exempt from CORS, so without this any page the user
    visited could stream this host's metrics from ws://127.0.0.1:8080."""
    with TestClient(create_app(SentinellaConfig(), collector=_Collector())) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "/ws/live", headers={"Origin": "https://evil.example"}
            ) as ws:
                ws.receive_text()
    assert exc.value.code == 1008


@pytest.mark.parametrize("origin", [None, "http://localhost", "HTTP://LOCALHOST"])
def test_websocket_from_the_dashboard_or_a_non_browser_is_accepted(origin):
    headers = {"Origin": origin} if origin else {}
    with TestClient(create_app(SentinellaConfig(), collector=_Collector())) as client:
        with client.websocket_connect("/ws/live", headers=headers) as ws:
            assert json.loads(ws.receive_text())["timestamp"] == 1.0


def test_origin_must_match_the_port_too():
    with TestClient(create_app(SentinellaConfig(), collector=_Collector())) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/live", headers={"Origin": "http://localhost:3000"}
            ) as ws:
                ws.receive_text()


# ── DNS rebinding ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/health", "/api/v1/snapshot"])
def test_loopback_bind_rejects_a_foreign_host_header(path):
    app = create_app(SentinellaConfig(), collector=_Collector())
    with RawTestClient(app, base_url="http://attacker.example") as client:
        response = client.get(path)
    assert response.status_code == 400
    # Rejections still carry the security headers.
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize(
    "host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080", "[::1]", "LOCALHOST"]
)
def test_loopback_bind_accepts_loopback_names(host):
    """Including bracketed IPv6, which Starlette's own TrustedHostMiddleware
    splits into "[" and rejects."""
    app = create_app(SentinellaConfig(), collector=_Collector())
    with RawTestClient(app) as client:
        assert client.get("/health", headers={"host": host}).status_code == 200


def test_loopback_bind_rejects_a_foreign_host_on_the_websocket():
    app = create_app(SentinellaConfig(), collector=_Collector())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/live", headers={"host": "attacker.example"}) as ws:
                ws.receive_text()
    assert exc.value.code == 1008


def test_public_bind_without_allowed_hosts_stays_permissive():
    """A server bound to the network is reached by whatever name the LAN uses
    for it; guessing would lock people out, so this needs explicit config."""
    cfg = SentinellaConfig(web=WebConfig(host="0.0.0.0", api_key="k"))
    with RawTestClient(create_app(cfg, collector=_Collector()), base_url="http://nas.lan") as c:
        assert c.get("/health").status_code == 200


def test_allowed_hosts_restricts_a_public_bind():
    cfg = SentinellaConfig(web=WebConfig(host="0.0.0.0", api_key="k", allowed_hosts=["nas.lan"]))
    app = create_app(cfg, collector=_Collector())
    with RawTestClient(app, base_url="http://nas.lan:8080") as client:
        assert client.get("/health").status_code == 200
    with RawTestClient(app, base_url="http://other.lan") as client:
        assert client.get("/health").status_code == 400
    with RawTestClient(app, base_url="http://localhost") as client:
        assert client.get("/health").status_code == 200


def test_allowed_hosts_extends_a_loopback_bind():
    """A reverse proxy forwarding its own Host to a loopback-bound server."""
    cfg = SentinellaConfig(web=WebConfig(allowed_hosts=["monitor.example.com"]))
    app = create_app(cfg, collector=_Collector())
    with RawTestClient(app, base_url="https://monitor.example.com") as client:
        assert client.get("/health").status_code == 200


def test_remote_agent_uses_its_own_allowed_hosts():
    cfg = SentinellaConfig(remote=RemoteConfig(allowed_hosts=["agent.lan"]))
    app = create_remote_app(cfg, collector=_Collector())
    with RawTestClient(app, base_url="http://agent.lan:9090") as client:
        assert client.get("/health").status_code == 200
    with RawTestClient(app, base_url="http://attacker.example") as client:
        assert client.get("/health").status_code == 400


def test_allowed_hosts_must_be_a_list_of_strings(tmp_path):
    path = tmp_path / "sentinella.toml"
    path.write_text('[web]\nallowed_hosts = "nas.lan"\n')
    with pytest.raises(ValueError, match=r"web\.allowed_hosts"):
        load_config(str(path))


# ── Unauthenticated sockets are bounded ─────────────────────────────────────


def test_sockets_waiting_to_authenticate_count_toward_the_client_limit():
    """They were accepted and held for the auth timeout without counting, so
    an exposed agent could be made to hold any number of idle sockets."""
    cfg = SentinellaConfig(web=WebConfig(api_key="k"))
    with patch("sentinella.server.api.MAX_WEBSOCKET_CLIENTS", 1):
        app = create_app(cfg, collector=_Collector())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live"):  # never authenticates
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/ws/live") as second:
                    second.send_text("k")
                    second.receive_text()
            assert exc.value.code == 1013


def test_a_failed_authentication_frees_its_slot():
    cfg = SentinellaConfig(web=WebConfig(api_key="k"))
    with patch("sentinella.server.api.MAX_WEBSOCKET_CLIENTS", 1):
        app = create_app(cfg, collector=_Collector())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/live") as ws:
                ws.send_text("wrong")
                ws.receive_text()
        with client.websocket_connect("/ws/live") as ws:
            ws.send_text("k")
            assert json.loads(ws.receive_text())["timestamp"] == 1.0


class _LiveServer:
    """The app on a real uvicorn server in a thread, on a free port.

    Used where a broken server would leave Starlette's TestClient blocked
    forever: over a real socket the client can give up, so a regression fails
    the test instead of hanging the suite.
    """

    def __init__(self, app):
        import threading

        import uvicorn

        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        import time

        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=5)


def test_a_client_that_never_authenticates_is_closed_and_frees_its_slot():
    """A socket that connects and never sends its key must be closed at the
    auth timeout, and its slot freed — not held open indefinitely."""
    import asyncio

    import websockets

    cfg = SentinellaConfig(web=WebConfig(api_key="k"))
    with (
        patch("sentinella.server.api.MAX_WEBSOCKET_CLIENTS", 1),
        patch("sentinella.server.api.WS_AUTH_TIMEOUT", 0.3),
    ):
        app = create_app(cfg, collector=_Collector())

    async def scenario(port):
        url = f"ws://127.0.0.1:{port}/ws/live"
        async with websockets.connect(url) as silent:
            with pytest.raises(websockets.ConnectionClosed) as closed:
                # Says nothing. Well past the 0.3 s timeout, the server must
                # have closed it; a server that doesn't fails here, not hangs.
                await asyncio.wait_for(silent.recv(), timeout=3)
        assert closed.value.rcvd.code == 4001
        # The only slot is free again for a client that does authenticate.
        async with websockets.connect(url) as ws:
            await ws.send("k")
            first = await asyncio.wait_for(ws.recv(), timeout=5)
        assert json.loads(first)["timestamp"] == 1.0

    with (
        patch("sentinella.server.api.MAX_WEBSOCKET_CLIENTS", 1),
        patch("sentinella.server.api.WS_AUTH_TIMEOUT", 0.3),
        _LiveServer(app) as server,
    ):
        asyncio.run(scenario(server.port))


def test_auth_timeout_is_short():
    from sentinella.core import api

    assert api.WS_AUTH_TIMEOUT <= 5


# ── API documentation endpoints ─────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_interactive_docs_are_not_served(path):
    """Swagger UI loads from a CDN that the CSP forbids, so /docs rendered
    blank, while still advertising every endpoint to anyone who asked."""
    with TestClient(create_app(SentinellaConfig(), collector=_Collector())) as client:
        assert client.get(path).status_code == 404


# ── Keys from the environment ───────────────────────────────────────────────


def test_api_keys_can_come_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "sentinella.toml"
    path.write_text('[web]\napi_key = "from-file"\n')
    monkeypatch.setenv("SENTINELLA_WEB_API_KEY", "from-env")
    monkeypatch.setenv("SENTINELLA_REMOTE_API_KEY", "remote-env")
    cfg = load_config(str(path))
    assert cfg.web.api_key == "from-env"
    assert cfg.remote.api_key == "remote-env"


def test_an_empty_environment_variable_does_not_clear_the_file_key(tmp_path, monkeypatch):
    path = tmp_path / "sentinella.toml"
    path.write_text('[web]\napi_key = "from-file"\n')
    monkeypatch.setenv("SENTINELLA_WEB_API_KEY", "")
    assert load_config(str(path)).web.api_key == "from-file"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_env_key_does_not_trigger_the_file_permission_warning(tmp_path, monkeypatch, caplog):
    path = tmp_path / "sentinella.toml"
    path.write_text("[general]\ntheme = 'dark'\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    monkeypatch.setenv("SENTINELLA_WEB_API_KEY", "from-env")
    with caplog.at_level(logging.WARNING):
        load_config(str(path))
    assert "plaintext api_key" not in caplog.text


# ── TLS ─────────────────────────────────────────────────────────────────────


def test_tls_needs_both_certificate_and_key():
    with pytest.raises(ValueError, match="ssl_certfile and ssl_keyfile"):
        validate_config(SentinellaConfig(web=WebConfig(ssl_certfile="cert.pem")))


def test_tls_files_reach_uvicorn(tmp_path, capsys):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    cfg = SentinellaConfig(web=WebConfig(ssl_certfile=str(cert), ssl_keyfile=str(key)))
    with (
        patch("sentinella.cli.load_config", return_value=cfg),
        patch("uvicorn.run") as run,
        patch("sentinella.web.server.create_app"),
    ):
        main(["web"])
    assert run.call_args.kwargs["ssl_certfile"] == str(cert)
    assert run.call_args.kwargs["ssl_keyfile"] == str(key)
    assert "https://" in capsys.readouterr().out


def test_missing_tls_file_is_a_config_error(tmp_path):
    cfg = SentinellaConfig(
        remote=RemoteConfig(ssl_certfile=str(tmp_path / "no.pem"), ssl_keyfile=str(tmp_path / "k"))
    )
    with pytest.raises(ValueError, match="remote.ssl_certfile"):
        validate_config(cfg)


# ── Command lines are opt-in on the agent ───────────────────────────────────


def test_agent_withholds_command_lines_by_default():
    """Command lines routinely carry secrets (passwords, tokens passed as
    arguments), so the agent does not serve them unless told to."""
    collector = _Collector(_snapshot_with_cmdline())
    with TestClient(create_remote_app(SentinellaConfig(), collector=collector)) as client:
        full = client.get("/api/v1/snapshot", params={"full": "true"}).json()
        module = client.get("/api/v1/processes", params={"full": "true"}).json()
    assert collector.cmdline_requested is None
    assert full["processes"][0]["cmdline"] == ""
    assert module["data"][0]["cmdline"] == ""


def test_agent_serves_command_lines_when_exposed():
    """What `sentinella --remote … print -f json` needs to match a local run —
    which silently exported empty command lines before."""
    cfg = SentinellaConfig(remote=RemoteConfig(expose_cmdline=True))
    collector = _Collector(_snapshot_with_cmdline())
    with TestClient(create_remote_app(cfg, collector=collector)) as client:
        full = client.get("/api/v1/snapshot", params={"full": "true"}).json()
        trimmed = client.get("/api/v1/snapshot").json()
    assert collector.cmdline_requested is True
    assert full["processes"][0]["cmdline"] == SECRET_CMDLINE
    assert "cmdline" not in trimmed["processes"][0]  # dashboards never get it


def test_web_dashboard_never_serves_command_lines():
    cfg = SentinellaConfig(remote=RemoteConfig(expose_cmdline=True))
    with TestClient(create_app(cfg, collector=_Collector(_snapshot_with_cmdline()))) as client:
        full = client.get("/api/v1/snapshot", params={"full": "true"}).json()
    assert full["processes"][0]["cmdline"] == ""


def test_remote_export_says_when_the_agent_withholds_command_lines(capsys):
    snap = SystemSnapshot(processes=[ProcessInfo(pid=1, name="a")], process_count=1)
    with patch("sentinella.remote.client.RemoteCollector") as rc:
        rc.return_value.collect.return_value = snap
        main(["--remote", "agent:9090", "print", "-f", "json"])
    err = capsys.readouterr().err
    assert "expose_cmdline" in err


def test_remote_text_export_has_no_cmdline_note(capsys):
    snap = SystemSnapshot(processes=[ProcessInfo(pid=1, name="a")], process_count=1)
    with patch("sentinella.remote.client.RemoteCollector") as rc:
        rc.return_value.collect.return_value = snap
        main(["--remote", "agent:9090", "print", "-f", "text"])
    assert "expose_cmdline" not in capsys.readouterr().err


# ── Key comparison ──────────────────────────────────────────────────────────


def test_key_comparison_does_not_depend_on_length():
    with patch("sentinella.server.api.hmac.compare_digest", return_value=False) as cmp:
        keys_match("short", "a-much-longer-key")
    a, b = cmp.call_args.args
    assert len(a) == len(b)
    assert keys_match("ключ", "ключ")
    assert not keys_match("key", "key2")


@pytest.mark.parametrize(
    ("origin", "host", "ok"),
    [
        ("http://localhost:8080", "localhost:8080", True),
        ("http://[::1]:8080", "[::1]:8080", True),
        ("http://localhost:3000", "localhost:8080", False),  # another local app
        ("https://evil.example", "localhost:8080", False),
        ("null", "localhost:8080", False),  # sandboxed frame / file://
        # TLS-terminating proxy with `Host $host`: the browser is on https/443,
        # the proxy speaks plain ws to the backend and the Host has no port.
        ("https://monitor.example.com", "monitor.example.com", True),
        ("http://monitor.example.com:3000", "monitor.example.com", False),
    ],
)
def test_origin_matching(origin, host, ok):
    from sentinella.server.api import origin_matches_host

    assert origin_matches_host(origin, host) is ok


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
    from sentinella.server.api import _is_public_bind

    assert _is_public_bind(host) is public


def test_open_bind_on_ipv6_wildcard_warns(capsys):
    from sentinella.server.api import warn_open_bind

    warn_open_bind("::", "", "Remote agent")
    assert "no API key" in capsys.readouterr().err


def test_open_bind_with_a_key_stays_quiet(capsys):
    from sentinella.server.api import warn_open_bind

    warn_open_bind("0.0.0.0", "a-key", "Remote agent")
    assert capsys.readouterr().err == ""
