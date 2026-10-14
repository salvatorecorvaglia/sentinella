"""FastAPI web dashboard and REST API server.

Endpoints:
    GET  /                     Web dashboard
    GET  /api/v1/snapshot      Full JSON snapshot
    GET  /api/v1/{module}      Individual module data
    WS   /ws/live              WebSocket live streaming
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from sentinella.config import SentinellaConfig, load_config
from sentinella.core.collector import Collector
from sentinella.core.protocols import MetricCollector
from sentinella.server.api import create_base_app, warn_open_bind

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: SentinellaConfig | None = None, collector: MetricCollector | None = None
) -> FastAPI:
    """Create and configure the FastAPI web dashboard application."""
    cfg = config or load_config()
    # An injected collector is shared with another consumer (e.g. the TUI), so
    # this app must not shut it down — see ``owns_collector``.
    owns_collector = collector is None
    collector = collector or Collector(cfg)
    api_key = cfg.web.api_key

    warn_open_bind(cfg.web.host, api_key, "Web dashboard")

    app = create_base_app(
        config=cfg,
        collector=collector,
        title="Sentinella System Monitor",
        description="Cross-platform system monitoring REST API",
        api_key=api_key,
        skip_auth_paths=frozenset({"/", "/static"}),
        owns_collector=owns_collector,
        bind_host=cfg.web.host,
        allowed_hosts=cfg.web.allowed_hosts,
        # A browser dashboard never needs command lines, whatever the
        # remote agent is configured to share.
        expose_cmdline=False,
    )

    # ── Static files ─────────────────────────────────────────────────
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Pre-load HTML dashboard template in memory on startup
    index_html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # ── HTML dashboard ───────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return index_html

    return app
