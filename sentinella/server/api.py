"""Shared API factory for web and remote servers.

Eliminates code duplication between ``web/server.py`` and
``remote/server.py`` by providing a common FastAPI app builder
with API key middleware, snapshot endpoints, and WebSocket streaming.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import sys
import time
import urllib.parse
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sentinella.config import SentinellaConfig
from sentinella.core.constants import API_VERSION, MODULE_NAMES, PROCESS_SORT_ORDER
from sentinella.core.limits import DASHBOARD_LIMITS
from sentinella.core.protocols import MetricCollector
from sentinella.core.sort import PROCESS_SORT_KEYS
from sentinella.core.utils import PERCENT_THRESHOLDS, TEMP_THRESHOLDS, serialize_model
from sentinella.server.broadcast import MAX_WEBSOCKET_CLIENTS, BroadcastHub

log = logging.getLogger(__name__)

# Module names valid for individual endpoints
VALID_MODULES = MODULE_NAMES


# How long a WebSocket may sit connected before sending its API key. Pending
# sockets count toward the client limit, so this bounds how long an
# unauthenticated peer can hold a slot.
WS_AUTH_TIMEOUT = 5.0

# Host header values a loopback-bound server always answers to.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_public_bind(host: str) -> bool:
    """True if *host* exposes the service beyond this machine.

    Matching the literal string ``"0.0.0.0"`` missed ``::`` (all IPv6
    interfaces), an empty host, and any explicit LAN address — all of which
    put metrics on the network just as effectively.
    """
    bind = (host or "").strip()
    if bind in ("", "*"):
        return True
    # Strip brackets from an IPv6 literal like "[::]".
    bind = bind.strip("[]")
    try:
        addr = ipaddress.ip_address(bind)
    except ValueError:
        # A hostname. "localhost" is the only one we can call safe without
        # resolving it, and resolution here would be a surprising side effect.
        return bind.lower() not in ("localhost", "localhost.localdomain")
    return not addr.is_loopback


# Fields dropped from the streamed/REST snapshot. `cmdline` is ~70% of a
# typical payload (61 KB of 87 KB on a 605-process host) and no dashboard —
# TUI or web — renders it. `sentinella print` builds its output from the Collector
# directly, so exports keep the full field; a remote client that wants it asks
# for ?full=true.
_TRIMMED_PROCESS_FIELDS = ("cmdline",)


def _shape_processes(
    procs: list[Any], *, max_processes: int | None, keep_cmdline: bool
) -> list[Any]:
    """A serialised process list as a client receives it.

    With ``max_processes`` (a dashboard): capped, and the fields no dashboard
    renders dropped. With ``None`` (a remote Sentinella asking for the full
    list): every process, with command lines blanked unless *keep_cmdline* —
    they often carry secrets, so the agent shares them only when configured to.
    """
    if max_processes is not None:
        return [
            {k: v for k, v in p.items() if k not in _TRIMMED_PROCESS_FIELDS}
            if isinstance(p, dict)
            else p
            for p in procs[:max_processes]
        ]
    if keep_cmdline:
        return procs
    return [{**p, "cmdline": ""} if isinstance(p, dict) and "cmdline" in p else p for p in procs]


def serialize_snapshot(
    snap: Any, *, max_processes: int | None, keep_cmdline: bool = True
) -> dict[str, Any]:
    """Serialise a snapshot for the wire.

    ``max_processes`` caps the process list for a dashboard; ``None`` sends
    every process — what a remote Sentinella asks for so its exports match a
    local run (see ``_shape_processes`` for what each shape carries).
    ``process_count`` is never altered, so a capped payload still reports the
    host's real total.
    """
    data = serialize_model(snap)
    procs = data.get("processes")
    if isinstance(procs, list):
        data["processes"] = _shape_processes(
            procs, max_processes=max_processes, keep_cmdline=keep_cmdline
        )
    return data


def split_host_port(value: str) -> tuple[str, int | None]:
    """Split a Host header value into (lower-cased host, port).

    Handles bracketed IPv6 (``[::1]:8080``), which a naive ``split(":")``
    turns into ``"["``. Returns ``("", None)`` for something unparseable.
    """
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return "", None
        host, rest = value[1:end], value[end + 1 :]
        port_text = rest[1:] if rest.startswith(":") else ""
    elif value.count(":") == 1:
        host, port_text = value.split(":")
    else:
        # A bare name, or a bare IPv6 address (no port to strip).
        host, port_text = value, ""
    if not port_text:
        return host, None
    return (host, int(port_text)) if port_text.isdigit() else ("", None)


def allowed_host_patterns(bind_host: str, configured: Iterable[str]) -> frozenset[str] | None:
    """The Host header values a server should answer to; ``None`` means any.

    A loopback-bound server only answers to loopback names, which is what
    stops DNS rebinding: a hostile page that rebinds its own name to
    127.0.0.1 still sends its own name as the Host. A server bound to the
    network answers to any name unless ``allowed_hosts`` narrows it — the
    names a LAN uses for a machine can't be guessed.
    """
    patterns = {h.strip().lower().strip("[]") for h in configured if h.strip()}
    if not patterns and _is_public_bind(bind_host):
        return None
    return frozenset(patterns | LOOPBACK_HOSTS)


def _host_is_allowed(host: str, patterns: frozenset[str]) -> bool:
    if host in patterns:
        return True
    # "*.example.com" matches any subdomain, the way Starlette's does.
    return any(p.startswith("*.") and host.endswith(p[1:]) for p in patterns)


class HostAllowlistMiddleware:
    """Reject requests and WebSockets whose Host header isn't allowed.

    Starlette's ``TrustedHostMiddleware`` splits the header on the first
    ``:``, which reduces ``[::1]:8080`` to ``[`` and would lock out every
    IPv6-loopback visitor, hence this small replacement.
    """

    def __init__(self, app: ASGIApp, patterns: frozenset[str]) -> None:
        self.app = app
        self.patterns = patterns

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host, _ = split_host_port(Headers(scope=scope).get("host", ""))
            if not _host_is_allowed(host, self.patterns):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = PlainTextResponse("Invalid host header", status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def origin_matches_host(origin: str, host_header: str) -> bool:
    """True if a browser's ``Origin`` names the server it is connecting to.

    WebSockets are exempt from CORS, so this is the only thing stopping any
    web page from opening the live stream: the dashboard's own origin always
    matches the Host it connects to, a foreign page's never does.
    """
    parsed = urllib.parse.urlsplit(origin.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False  # includes "null" (sandboxed frames, file://)
    try:
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    host, port = split_host_port(host_header)
    if parsed.hostname.lower() != host:
        return False
    if port is None:
        # No port in Host: a default-port origin, whichever scheme. A proxy
        # terminating TLS forwards `Host: name` over plain ws while the page
        # is on https/443, so the backend's own scheme can't decide it — but
        # an origin on any other port (another local app) is still refused.
        return origin_port in (80, 443)
    return origin_port == port


def warn_open_bind(host: str, api_key: str, service_name: str) -> None:
    """Log a security warning if binding beyond loopback with no auth."""
    if _is_public_bind(host) and not api_key:
        log.warning(
            "⚠️  %s is binding to %s with no API key. "
            "System metrics will be exposed to the network. "
            "Set an api_key or bind to 127.0.0.1.",
            service_name,
            host or "all interfaces",
        )
        print(
            f"⚠️  WARNING: {service_name} is binding to {host or 'all interfaces'} "
            "with no API key. System metrics are exposed to the network.",
            file=sys.stderr,
        )


def keys_match(candidate: str, expected: str) -> bool:
    """Constant-time API key comparison that tolerates non-ASCII keys.

    ``hmac.compare_digest`` raises ``TypeError`` when given ``str`` arguments
    containing non-ASCII characters, so both sides are encoded to UTF-8 first.
    Pad both byte strings to the same length to avoid a length mismatch in
    ``compare_digest``. Check the original lengths too, so trailing NUL bytes
    cannot make different keys match. Encoding and padding depend on length;
    the byte comparison does not short-circuit based on key contents.
    """
    candidate_bytes = candidate.encode("utf-8")
    expected_bytes = expected.encode("utf-8")
    comparison_length = max(len(candidate_bytes), len(expected_bytes))
    contents_match = hmac.compare_digest(
        candidate_bytes.ljust(comparison_length, b"\x00"),
        expected_bytes.ljust(comparison_length, b"\x00"),
    )
    return contents_match & (len(candidate_bytes) == len(expected_bytes))


def create_base_app(
    *,
    config: SentinellaConfig,
    collector: MetricCollector,
    title: str,
    description: str,
    api_key: str = "",
    skip_auth_paths: frozenset[str] | None = None,
    owns_collector: bool = True,
    bind_host: str = "127.0.0.1",
    allowed_hosts: Iterable[str] = (),
    expose_cmdline: bool = False,
) -> FastAPI:
    """Create a FastAPI app with shared monitoring endpoints.

    Parameters
    ----------
    config:
        Sentinella configuration.
    collector:
        The metric collector instance.
    title:
        FastAPI app title.
    description:
        FastAPI app description.
    api_key:
        API key for authentication (empty = no auth).
    skip_auth_paths:
        Exact URL paths, or path prefixes whose children (``prefix + "/"``)
        are also exempt, to skip auth for (e.g. static files).
    owns_collector:
        Whether this app is responsible for the collector's lifecycle.  Must
        be ``False`` when the collector is shared with another consumer (e.g.
        the TUI running a background web server), otherwise app shutdown
        would tear down a collector still in use.
    bind_host, allowed_hosts:
        Where the server listens and the extra Host names it answers to —
        see ``allowed_host_patterns``.
    expose_cmdline:
        Whether ``?full=true`` responses include process command lines. When
        ``False`` they are blanked: they often carry secrets.
    """
    from sentinella import __version__

    def _wire_snapshot(snap: Any) -> dict[str, Any]:
        """Trim a snapshot to what a dashboard actually renders."""
        return serialize_snapshot(snap, max_processes=config.processes.max_display)

    hub = BroadcastHub(
        snapshot_source=collector.collect_async,
        serializer=_wire_snapshot,
        interval=config.general.refresh_interval,
        # Read here, at app-construction time, so this stays the app's own
        # capacity knob rather than a value baked into a default argument.
        max_clients=MAX_WEBSOCKET_CLIENTS,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.start()
        yield
        await hub.stop()
        # Only tear down a collector this app created — a shared collector
        # (e.g. owned by the TUI) must outlive the server.
        if owns_collector:
            await collector.close_async()

    app = FastAPI(
        title=title,
        version=__version__,
        description=description,
        lifespan=lifespan,
        # Swagger UI and ReDoc load from a CDN the CSP forbids, so they only
        # ever rendered blank — while advertising the full API surface.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ── CORS ─────────────────────────────────────────────────────────
    # The web dashboard is served from the same origin so no CORS is
    # needed.  Keep the middleware for explicit opt-in later, but
    # default to NO allowed origins to prevent cross-origin data leaks.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["X-API-Key"],
    )

    # ── API key middleware (timing-safe) ─────────────────────────────
    _skip = (skip_auth_paths or frozenset()) | frozenset({"/health"})

    if api_key:
        from fastapi.responses import JSONResponse

        @app.middleware("http")
        async def _check_api_key(request: Request, call_next):
            path = request.url.path
            is_skipped = path in _skip or any(
                path.startswith(prefix + "/") for prefix in _skip if prefix != "/"
            )
            if is_skipped:
                return await call_next(request)

            key = request.headers.get("X-API-Key")
            if not key:
                return JSONResponse(status_code=401, content={"detail": "Missing API key"})
            if not keys_match(key, api_key):
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
            return await call_next(request)

    # ── Host allowlist (DNS rebinding) ──────────────────────────────
    # Added after auth so it runs before it: a rebinding page gets a 400 from
    # the Host check, never a 401 that confirms the server is there.
    host_patterns = allowed_host_patterns(bind_host, allowed_hosts)
    if host_patterns is not None:
        app.add_middleware(HostAllowlistMiddleware, patterns=host_patterns)

    # ── Security headers ────────────────────────────────────────────
    # The dashboard ships all of its own assets (Chart.js is vendored, fonts
    # are system stacks), so the CSP can forbid every external origin.
    # connect-src keeps ws:/wss: for the live stream.
    #
    # Registered *after* the auth middleware on purpose: Starlette runs the
    # last-registered HTTP middleware first, so this must be outermost or a
    # 401 short-circuits before the headers are ever attached — which is
    # exactly what used to happen to every auth failure.
    @app.middleware("http")
    async def _add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'none'",
        )
        return response

    # ── REST: full snapshot ──────────────────────────────────────────
    @app.get("/api/v1/snapshot")
    async def snapshot(full: bool = False):
        """The dashboard payload by default; ``?full=true`` for the whole thing.

        Another Sentinella instance (``sentinella --remote``) asks for the full form so
        that `sentinella print` over a remote agent still exports every process and
        every field.
        """
        snap = await collector.collect_async()
        if full:
            return serialize_snapshot(snap, max_processes=None, keep_cmdline=expose_cmdline)
        return _wire_snapshot(snap)

    # ── REST: individual modules ─────────────────────────────────────
    @app.get("/api/v1/{module}")
    async def module_data(module: str, full: bool = False):
        if module not in VALID_MODULES:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown module '{module}'. Valid: {sorted(VALID_MODULES)}",
            )
        if module not in collector.active_modules:
            raise HTTPException(
                status_code=404,
                detail=f"Module '{module}' is not enabled on this agent",
            )
        result = await collector.collect_module_async(module)
        if result is None:
            raise HTTPException(status_code=404, detail=f"No data for module '{module}'")
        serialized = serialize_model(result)
        if module == "processes" and isinstance(serialized, list):
            serialized = _shape_processes(
                serialized,
                max_processes=None if full else config.processes.max_display,
                keep_cmdline=expose_cmdline,
            )
        timestamp = collector.last_collected_at or time.time()
        return {"module": module, "timestamp": timestamp, "data": serialized}

    # ── WebSocket: live stream ───────────────────────────────────────
    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin is not None and not origin_matches_host(
            origin, websocket.headers.get("host", "")
        ):
            # Refused before the handshake completes. Non-browser clients
            # send no Origin and are unaffected.
            log.warning("Rejecting WebSocket from foreign origin %r", origin)
            await websocket.close(code=1008)
            return

        await websocket.accept()
        # The slot is taken *before* authentication: sockets still waiting to
        # send their key used to be uncounted, so any number could be held.
        if not hub.reserve():
            log.warning(
                "Rejecting WebSocket client — %d client limit reached",
                MAX_WEBSOCKET_CLIENTS,
            )
            await websocket.close(code=1013, reason="Too many clients")
            return
        joined = False
        try:
            # Authenticate via first message if an API key is configured.
            # This avoids leaking the key in URL query params (logs, history).
            if api_key:
                try:
                    auth_msg = await asyncio.wait_for(
                        websocket.receive_text(), timeout=WS_AUTH_TIMEOUT
                    )
                except TimeoutError:
                    await websocket.close(code=4001, reason="Auth timeout")
                    return
                if not keys_match(auth_msg, api_key):
                    await websocket.close(code=4001, reason="Invalid API key")
                    return

            hub.join(websocket)
            joined = True
            initial_snap = await collector.collect_async()
            # Same trim as the broadcast loop — this is the first frame every
            # browser sees, and it was going out untrimmed.
            await websocket.send_text(hub.encode(initial_snap))

            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass
        finally:
            if joined:
                hub.discard(websocket)
            else:
                hub.release()

    # ── Health check ─────────────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "agent": "sentinella",
            "version": __version__,
            # The payload contract, so a mismatched dashboard or --remote
            # client can say so instead of misreading fields.
            "api_version": API_VERSION,
            # Lets clients render "not monitored" instead of a misleading 0.
            "active_modules": sorted(collector.active_modules),
            # Server-side default; the browser's saved preference wins.
            "theme": config.general.theme,
            # So the dashboard applies the same process limit as the TUI and
            # `sentinella print` rather than a hardcoded one.
            "max_display": config.processes.max_display,
            # How often snapshots should advance, so the dashboard can tell a
            # stalled stream (same snapshot, over and over) from a live one.
            "refresh_interval": config.general.refresh_interval,
            # The keys the agent can rank the whole host by, with the field
            # and direction each one means, so the dashboard sorts exactly as
            # the agent does without keeping a copy of PROCESS_SORT_KEYS. Any
            # other column it sorts only reorders the rows it was sent.
            "sort_keys": {
                key: {"field": PROCESS_SORT_KEYS[key][0], "descending": PROCESS_SORT_KEYS[key][1]}
                for key in PROCESS_SORT_ORDER
            },
            # The key the agent ranked the process list by before truncating.
            # The dashboard can only re-sort what it received, so it needs this
            # to label a local re-sort as such instead of implying a true top-N.
            "sort_by": config.processes.sort_by,
            # Same reason, for the variable-length lists: without these the web
            # dashboard rendered every partition and interface while the TUI
            # showed 5, so one host described itself two different ways.
            "display_limits": dict(DASHBOARD_LIMITS),
            # The severity thresholds, so the dashboard colours a reading the
            # same way the TUI and console output do instead of keeping its
            # own copy in step by comment.
            "thresholds": {
                "percent": list(PERCENT_THRESHOLDS),
                "temp": list(TEMP_THRESHOLDS),
            },
        }

    return app
