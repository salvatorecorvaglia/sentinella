"""Shared API factory for web and remote servers.

Eliminates code duplication between ``web/server.py`` and
``remote/server.py`` by providing a common FastAPI app builder
with API key middleware, snapshot endpoints, and WebSocket streaming.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from sentinella.config import SentinellaConfig
from sentinella.core.broadcast import MAX_WEBSOCKET_CLIENTS, BroadcastHub
from sentinella.core.collector import Collector
from sentinella.core.limits import DASHBOARD_LIMITS
from sentinella.core.utils import PERCENT_THRESHOLDS, TEMP_THRESHOLDS
from sentinella.core.utils import serialize_model as asdict

log = logging.getLogger(__name__)

# Module names valid for individual endpoints
VALID_MODULES = frozenset(
    {
        "cpu",
        "memory",
        "disk",
        "network",
        "processes",
        "users",
        "sensors",
        "containers",
        "system_info",
    }
)


def _is_public_bind(host: str) -> bool:
    """True if *host* exposes the service beyond this machine.

    Matching the literal string ``"0.0.0.0"`` missed ``::`` (all IPv6
    interfaces), an empty host, and any explicit LAN address — all of which
    put metrics on the network just as effectively.
    """
    import ipaddress

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


def serialize_snapshot(snap: Any, *, max_processes: int | None) -> dict[str, Any]:
    """Serialise a snapshot for the wire.

    ``max_processes`` caps the process list and drops the fields no dashboard
    renders.  ``None`` means send everything, untouched — what a remote Sentinella
    client asks for so its exports match a local run.  ``process_count`` is
    never altered, so a trimmed payload still reports the host's real total.
    """
    data = asdict(snap)
    if max_processes is None:
        return data
    procs = data.get("processes")
    if not isinstance(procs, list):
        return data
    data["processes"] = [
        {k: v for k, v in proc.items() if k not in _TRIMMED_PROCESS_FIELDS}
        if isinstance(proc, dict)
        else proc
        for proc in procs[:max_processes]
    ]
    return data


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
    """
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def create_base_app(
    *,
    config: SentinellaConfig,
    collector: Collector,
    title: str,
    description: str,
    api_key: str = "",
    skip_auth_paths: frozenset[str] | None = None,
    owns_collector: bool = True,
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
    """
    from sentinella import __version__

    def _active_modules() -> frozenset[str]:
        """Modules this collector actually monitors.

        Collectors that don't advertise the set (e.g. ``RemoteCollector``,
        which proxies whatever the upstream agent reports) are assumed to
        serve every module.
        """
        return getattr(collector, "active_modules", VALID_MODULES)

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
            if hasattr(collector, "close_async"):
                await collector.close_async()
            collector.close()

    app = FastAPI(
        title=title,
        version=__version__,
        description=description,
        lifespan=lifespan,
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
            return serialize_snapshot(snap, max_processes=None)
        return _wire_snapshot(snap)

    # ── REST: individual modules ─────────────────────────────────────
    @app.get("/api/v1/{module}")
    async def module_data(module: str, full: bool = False):
        if module not in VALID_MODULES:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown module '{module}'. Valid: {sorted(VALID_MODULES)}",
            )
        if module not in _active_modules():
            raise HTTPException(
                status_code=404,
                detail=f"Module '{module}' is not enabled on this agent",
            )
        result = await collector.collect_module_async(module)
        if result is None:
            raise HTTPException(status_code=404, detail=f"No data for module '{module}'")
        if isinstance(result, list):
            if module == "processes" and not full:
                result = result[: config.processes.max_display]
            serialized = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in result]
            if module == "processes" and not full:
                serialized = [
                    {k: v for k, v in x.items() if k not in _TRIMMED_PROCESS_FIELDS}
                    if isinstance(x, dict)
                    else x
                    for x in serialized
                ]
        elif hasattr(result, "__dataclass_fields__"):
            serialized = asdict(result)
        else:
            serialized = result
        timestamp = getattr(collector, "last_collected_at", 0.0) or time.time()
        return {"module": module, "timestamp": timestamp, "data": serialized}

    # ── WebSocket: live stream ───────────────────────────────────────
    @app.websocket("/ws/live")
    async def ws_live(websocket: WebSocket):
        await websocket.accept()
        try:
            # Authenticate via first message if an API key is configured.
            # This avoids leaking the key in URL query params (logs, history).
            if api_key:
                try:
                    auth_msg = await asyncio.wait_for(websocket.receive_text(), timeout=10)
                except TimeoutError:
                    await websocket.close(code=4001, reason="Auth timeout")
                    return
                if not keys_match(auth_msg, api_key):
                    await websocket.close(code=4001, reason="Invalid API key")
                    return

            if hub.is_full():
                log.warning(
                    "Rejecting WebSocket client — %d client limit reached",
                    MAX_WEBSOCKET_CLIENTS,
                )
                await websocket.close(code=1013, reason="Too many clients")
                return

            hub.add(websocket)
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
            hub.discard(websocket)

    # ── Health check ─────────────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "agent": "sentinella",
            "version": __version__,
            # Lets clients render "not monitored" instead of a misleading 0.
            "active_modules": sorted(_active_modules()),
            # Server-side default; the browser's saved preference wins.
            "theme": config.general.theme,
            # So the dashboard applies the same process limit as the TUI and
            # `sentinella print` rather than a hardcoded one.
            "max_display": config.processes.max_display,
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
