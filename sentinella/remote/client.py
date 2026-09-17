"""Remote client — fetches metrics from a remote Sentinella agent.

Implements the same ``collect()`` / ``collect_async()`` interface as the
local ``Collector``, so TUI and exporters can seamlessly switch between
local and remote data sources.

Usage::

    sentinella --remote 10.0.0.5:9090
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from sentinella.core.models import (
    BatteryInfo,
    ContainerInfo,
    ContainerMetrics,
    CpuMetrics,
    DiskIO,
    DiskMetrics,
    DiskPartition,
    FanReading,
    MemoryMetrics,
    NetworkInterface,
    NetworkMetrics,
    ProcessInfo,
    SensorMetrics,
    SystemInfoMetrics,
    SystemSnapshot,
    TemperatureReading,
    UserInfo,
)

log = logging.getLogger(__name__)

# The agent trims its snapshot for browser dashboards (no cmdline, process list
# capped at max_display). Another Sentinella is not a browser: `sentinella print --remote`
# must be able to export the same fields it would export locally, so ask for
# the untrimmed form. Agents older than this flag ignore the parameter and send
# the full payload anyway, which is exactly the right fallback.
_FULL_SNAPSHOT = {"full": "true"}

# Assumed when the agent hasn't been asked (or can't be reached): better to
# offer a module and fail than to hide one that is actually there.
_ALL_MODULES = frozenset(
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


class RemoteCollector:
    """Collects system metrics from a remote Sentinella agent via HTTP."""

    def __init__(self, address: str, api_key: str = "") -> None:
        # Normalise address
        if not address.startswith("http"):
            address = f"http://{address}"
        self.base_url = address.rstrip("/")
        self.api_key = api_key
        self._client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None
        # Keeps a strong reference to fire-and-forget cleanup tasks scheduled
        # by close() so the event loop doesn't garbage-collect them mid-close.
        self._pending_close_tasks: set[asyncio.Task] = set()
        self._last_collected_at: float = 0.0
        # Populated from the agent's /health on first contact. Until then,
        # assume the agent serves everything — the same assumption core.api
        # made for collectors that didn't advertise the set.
        self._active_modules: frozenset[str] | None = None

    def _headers(self) -> dict[str, str]:
        hdrs: dict[str, str] = {}
        if self.api_key:
            hdrs["X-API-Key"] = self.api_key
        return hdrs

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=10)
        return self._client

    async def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=10)
        return self._async_client

    def close(self) -> None:
        """Close both HTTP clients.

        Callers that only ever used ``collect_async`` still hold an open
        ``AsyncClient``; closing just the sync client would leak its
        connection pool.  Prefer ``close_async()`` from async code — this
        drives ``aclose()`` on a throwaway loop, which is only possible when
        no loop is already running on this thread.  If a loop *is* already
        running, the close is scheduled on it instead of being dropped, so the
        connection pool still gets released even though this method can't
        await it.
        """
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._async_client is not None:
            client, self._async_client = self._async_client, None
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(client.aclose())
            else:
                log.warning(
                    "RemoteCollector.close() called from a running event loop; "
                    "scheduling async client cleanup — prefer close_async() to "
                    "await it directly"
                )
                task = loop.create_task(client.aclose())
                self._pending_close_tasks.add(task)
                task.add_done_callback(self._on_close_task_done)

    def _on_close_task_done(self, task: asyncio.Task) -> None:
        """Surface a cleanup task that failed instead of dropping it silently."""
        self._pending_close_tasks.discard(task)
        if task.cancelled():
            log.warning(
                "RemoteCollector async client cleanup was cancelled before it "
                "completed; its connection pool may not have been released"
            )
            return
        if task.exception() is not None:
            log.warning("RemoteCollector async client cleanup failed: %s", task.exception())

    async def close_async(self) -> None:
        """Close both HTTP clients, awaiting a clean async shutdown."""
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def last_collected_at(self) -> float:
        """Unix time of the last successful fetch (0.0 if none yet)."""
        return self._last_collected_at

    @property
    def active_modules(self) -> frozenset[str]:
        """Modules the upstream agent reports, from its ``/health``.

        Falls back to the full set until the agent has been asked, so a module
        is never wrongly reported as unmonitored.
        """
        if self._active_modules is not None:
            return self._active_modules
        try:
            resp = self._get_client().get(f"{self.base_url}/health", headers=self._headers())
            resp.raise_for_status()
            modules = resp.json().get("active_modules")
        except Exception:
            log.debug("Could not read active_modules from the agent", exc_info=True)
            return _ALL_MODULES
        if not isinstance(modules, list):
            return _ALL_MODULES
        self._active_modules = frozenset(str(m) for m in modules)
        return self._active_modules

    async def collect_module_async(self, name: str) -> Any:
        """Fetch one module from the agent.

        Returns ``None`` for a module the agent does not serve, matching
        ``Collector.collect_module`` so callers can tell "not monitored" from a
        genuine all-zero reading.
        """
        client = await self._get_async_client()
        resp = await client.get(
            f"{self.base_url}/api/v1/{name}",
            headers=self._headers(),
            params=_FULL_SNAPSHOT,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("data")

    def collect(self) -> SystemSnapshot:
        """Synchronous fetch of a remote snapshot using a persistent client."""
        client = self._get_client()
        resp = client.get(
            f"{self.base_url}/api/v1/snapshot",
            headers=self._headers(),
            params=_FULL_SNAPSHOT,
        )
        resp.raise_for_status()
        data = resp.json()
        self._last_collected_at = time.time()
        return self._parse(data)

    async def collect_async(self) -> SystemSnapshot:
        """Async fetch of a remote snapshot using a persistent client."""
        client = await self._get_async_client()
        resp = await client.get(
            f"{self.base_url}/api/v1/snapshot",
            headers=self._headers(),
            params=_FULL_SNAPSHOT,
        )
        resp.raise_for_status()
        data = resp.json()
        self._last_collected_at = time.time()
        return self._parse(data)

    # ── Parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _build(cls: type, data: dict | None):
        """Build a model from a dict, dropping keys the model doesn't declare.

        Unknown keys are ignored so a newer agent can add fields without
        breaking an older client.
        """
        if not data:
            return cls()
        known = cls.__dataclass_fields__  # type: ignore[attr-defined]  # always a dataclass
        return cls(**{k: v for k, v in data.items() if k in known})

    @staticmethod
    def _parse(data: dict) -> SystemSnapshot:
        """Convert a JSON dict back into a ``SystemSnapshot``."""
        cpu_data = data.get("cpu")
        mem_data = data.get("memory")
        disk_data = data.get("disk") or {}
        net_data = data.get("network") or {}
        si_data = data.get("system_info")
        sensor_data = data.get("sensors") or {}
        ct_data = data.get("containers") or {}

        # CPU
        cpu = RemoteCollector._build(CpuMetrics, cpu_data)

        # Memory
        mem = RemoteCollector._build(MemoryMetrics, mem_data)

        # Disk
        parts = [
            RemoteCollector._build(DiskPartition, p) for p in (disk_data.get("partitions") or [])
        ]
        disk_io = RemoteCollector._build(DiskIO, disk_data.get("io"))
        disk = DiskMetrics(partitions=parts, io=disk_io)

        # Network
        ifaces = [
            RemoteCollector._build(NetworkInterface, i) for i in (net_data.get("interfaces") or [])
        ]
        network = NetworkMetrics(
            interfaces=ifaces,
            connections_count=net_data.get("connections_count") or 0,
        )

        # Processes
        procs = [RemoteCollector._build(ProcessInfo, p) for p in (data.get("processes") or [])]

        # Users
        users = [RemoteCollector._build(UserInfo, u) for u in (data.get("users") or [])]

        # Sensors
        temps = [
            RemoteCollector._build(TemperatureReading, t)
            for t in (sensor_data.get("temperatures") or [])
        ]
        fans = [RemoteCollector._build(FanReading, f) for f in (sensor_data.get("fans") or [])]
        bat_raw = sensor_data.get("battery")
        battery = RemoteCollector._build(BatteryInfo, bat_raw) if bat_raw else None
        sensors = SensorMetrics(temperatures=temps, fans=fans, battery=battery)

        # Containers
        containers_list = [
            RemoteCollector._build(ContainerInfo, c) for c in (ct_data.get("containers") or [])
        ]
        containers = ContainerMetrics(
            containers=containers_list,
            docker_available=bool(ct_data.get("docker_available")),
            lxc_available=bool(ct_data.get("lxc_available")),
        )

        # System info
        sys_info = RemoteCollector._build(SystemInfoMetrics, si_data)

        return SystemSnapshot(
            timestamp=data.get("timestamp", 0.0),
            cpu=cpu,
            memory=mem,
            disk=disk,
            network=network,
            processes=procs,
            process_count=data.get("process_count") or len(procs),
            users=users,
            sensors=sensors,
            containers=containers,
            system_info=sys_info,
        )
