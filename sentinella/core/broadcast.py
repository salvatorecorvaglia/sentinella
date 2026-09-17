"""WebSocket fan-out for the monitoring API.

Split out of ``core.api``: the client set, the per-client send timeout, the
capacity limit and the collect-and-fan-out loop were closures inside a
300-line factory, so none of them could be exercised without booting a
``TestClient`` around a whole FastAPI app.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

# Upper bound on concurrent WebSocket clients per server.  The broadcast loop
# sends to every client serially, so an unbounded set lets one viewer degrade
# the stream for all of them.
MAX_WEBSOCKET_CLIENTS = 64

# Per-client send timeout, so a stalled socket cannot block the broadcast loop.
WS_SEND_TIMEOUT = 5.0

# Consecutive failed broadcast cycles tolerated before the clients are closed
# and handed to their own reconnect backoff.
MAX_BROADCAST_FAILURES = 3


class BroadcastHub:
    """Owns the connected clients and the loop that feeds them."""

    def __init__(
        self,
        *,
        snapshot_source: Callable[[], Awaitable[Any]],
        serializer: Callable[[Any], dict[str, Any]],
        interval: float,
        max_clients: int | None = None,
        send_timeout: float | None = None,
        max_failures: int | None = None,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._serializer = serializer
        self._interval = interval
        # Resolved here rather than as default arguments so the module-level
        # values stay a live knob instead of being frozen at import time.
        self._max_clients = MAX_WEBSOCKET_CLIENTS if max_clients is None else max_clients
        self._send_timeout = WS_SEND_TIMEOUT if send_timeout is None else send_timeout
        self._max_failures = MAX_BROADCAST_FAILURES if max_failures is None else max_failures
        self._clients: set[Any] = set()
        self._task: asyncio.Task | None = None

    # ── Membership ───────────────────────────────────────────────────

    @property
    def clients(self) -> set[Any]:
        """The live client set (read-only in spirit; used for assertions)."""
        return self._clients

    def is_full(self) -> bool:
        return len(self._clients) >= self._max_clients

    def add(self, websocket: Any) -> None:
        self._clients.add(websocket)

    def discard(self, websocket: Any) -> None:
        self._clients.discard(websocket)

    # ── Sending ──────────────────────────────────────────────────────

    def encode(self, snapshot: Any) -> str:
        """Serialise a snapshot to the exact text a client receives."""
        return json.dumps(self._serializer(snapshot), default=str)

    async def _drop(self, clients, code: int, reason: str) -> None:
        for ws in clients:
            self._clients.discard(ws)
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass

    async def broadcast_once(self) -> None:
        """Collect a snapshot and fan it out to every connected client."""
        payload = self.encode(await self._snapshot_source())
        disconnected = []
        for ws in list(self._clients):
            try:
                await asyncio.wait_for(ws.send_text(payload), self._send_timeout)
            except TimeoutError:
                log.warning("Dropping WebSocket client — send timed out")
                disconnected.append(ws)
            except Exception:
                disconnected.append(ws)
        await self._drop(disconnected, 1011, "Send timeout")

    # ── The loop ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Broadcast until cancelled, surviving individual failed cycles.

        The ``try`` wraps the *body*, not the loop: an exception escaping to an
        outer handler used to end the loop for the lifetime of the app, leaving
        every browser on an open socket that showed "Live" and never received
        another frame. A failed cycle is survivable; a dead loop is not.
        """
        log.info("Starting background WebSocket broadcast loop")
        failures = 0
        try:
            while True:
                if self._clients:
                    try:
                        await self.broadcast_once()
                        failures = 0
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failures += 1
                        log.exception("Broadcast cycle failed (%d consecutive)", failures)
                        if failures >= self._max_failures:
                            # Stop pretending the stream is alive: closing the
                            # sockets hands the clients to their own reconnect
                            # backoff instead of stranding them on stale data.
                            log.error(
                                "Dropping all WebSocket clients after %d "
                                "consecutive broadcast failures",
                                failures,
                            )
                            await self._drop(list(self._clients), 1011, "Broadcast failure")
                            failures = 0
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            log.info("Background WebSocket broadcast loop cancelled")

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
