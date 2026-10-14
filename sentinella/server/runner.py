"""Runner helper to spawn background servers for Web and Remote agents.

Decouples CLI/TUI routing from uvicorn thread spawning.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import TYPE_CHECKING

import uvicorn

from sentinella.config import RemoteConfig, SentinellaConfig, WebConfig

if TYPE_CHECKING:
    from sentinella.core.protocols import MetricCollector

log = logging.getLogger(__name__)


def start_background_servers(
    config: SentinellaConfig, collector: MetricCollector | None = None
) -> list[str]:
    """Start web and/or remote servers in background daemon threads if configured.

    Returns one message per server that could not start, for the caller to
    show. uvicorn exits *inside its thread* when it can't bind, so without a
    check up front a taken port left the TUI running with no server and no
    sign of it.
    """
    failures: list[str] = []
    if config.web.enabled:
        from sentinella.web.server import create_app

        problem = _bind_problem(config.web.host, config.web.port)
        if problem:
            failures.append(_failure("Web dashboard", config.web, problem))
        else:
            _serve_in_background(
                create_app(config, collector=collector), config.web, "Web dashboard"
            )

    if config.remote.enabled:
        from sentinella.remote.server import create_remote_app

        problem = _bind_problem(config.remote.host, config.remote.port)
        if problem:
            failures.append(_failure("Remote agent", config.remote, problem))
        else:
            _serve_in_background(
                create_remote_app(config, collector=collector), config.remote, "Remote agent"
            )

    for message in failures:
        log.error("%s", message)
    return failures


def _failure(label: str, server: WebConfig | RemoteConfig, problem: str) -> str:
    return f"{label} could not start on {server.host}:{server.port}: {problem}"


def _bind_problem(host: str, port: int) -> str | None:
    """Why *host*:*port* can't be bound right now, or ``None`` if it can.

    Binds and immediately releases a socket the way uvicorn will. There is a
    window between this check and uvicorn's own bind; ``_run_server`` logs a
    failure in that window rather than letting it vanish.
    """
    try:
        infos = socket.getaddrinfo(
            host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except socket.gaierror as exc:
        return f"cannot resolve host ({exc.strerror or exc})"
    family, sock_type, proto, _, address = infos[0]
    with socket.socket(family, sock_type, proto) as sock:
        if os.name != "nt":
            # As uvicorn does: lets a port in TIME_WAIT through, while a live
            # listener still fails. (On Windows it would let us steal a port.)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(address)
        except OSError as exc:
            return exc.strerror or str(exc)
    return None


def _run_server(app, kwargs: dict, label: str) -> None:
    """``uvicorn.run`` for a background thread, logging how it ended badly.

    uvicorn raises ``SystemExit`` when startup fails; in a thread that ends the
    thread silently, so it is caught and logged here instead.
    """
    try:
        uvicorn.run(app, **kwargs)
    except (SystemExit, Exception):
        log.error("%s stopped unexpectedly", label, exc_info=True)


def _serve_in_background(app, server: WebConfig | RemoteConfig, label: str) -> None:
    kwargs = {
        "host": server.host,
        "port": server.port,
        "log_level": "warning",
        "install_signal_handlers": False,
        # Propagate to root instead of binding handlers to the terminal,
        # which the TUI is drawing on (see cli._tui_logging).
        "log_config": None,
        "ssl_certfile": server.ssl_certfile or None,
        "ssl_keyfile": server.ssl_keyfile or None,
    }
    threading.Thread(target=_run_server, args=(app, kwargs, label), daemon=True).start()
    log.info(
        "Started background %s thread on %s://%s:%d",
        label,
        "https" if server.ssl_certfile else "http",
        server.host,
        server.port,
    )
