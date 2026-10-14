"""Remote monitoring agent — runs on a monitored host.

Exposes the same REST API endpoints so that a remote client (or the
TUI / web dashboard) can connect and pull metrics.

Usage::

    sentinella serve --host 0.0.0.0 --port 9090
"""

from __future__ import annotations

from fastapi import FastAPI

from sentinella.config import SentinellaConfig, load_config
from sentinella.core.collector import Collector
from sentinella.core.protocols import MetricCollector
from sentinella.server.api import create_base_app, warn_open_bind


def create_remote_app(
    config: SentinellaConfig | None = None,
    collector: MetricCollector | None = None,
) -> FastAPI:
    """Create the remote agent FastAPI application."""
    cfg = config or load_config()
    # An injected collector is shared with another consumer (e.g. the TUI), so
    # this app must not shut it down — see ``owns_collector``.
    owns_collector = collector is None
    collector = collector or Collector(cfg)
    api_key = cfg.remote.api_key

    warn_open_bind(cfg.remote.host, api_key, "Remote agent")

    if cfg.remote.expose_cmdline:
        # Collecting them costs a read per PID, so only when they'll be served.
        collector.set_collect_cmdline(True)

    return create_base_app(
        config=cfg,
        collector=collector,
        title="Sentinella Remote Agent",
        description="Remote system monitoring agent",
        api_key=api_key,
        owns_collector=owns_collector,
        bind_host=cfg.remote.host,
        allowed_hosts=cfg.remote.allowed_hosts,
        expose_cmdline=cfg.remote.expose_cmdline,
    )
