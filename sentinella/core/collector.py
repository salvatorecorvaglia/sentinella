"""Central metric collection orchestrator.

The ``Collector`` loads all enabled plugins once, then on each ``collect()``
call runs every plugin and assembles a ``SystemSnapshot``.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sentinella.config import SentinellaConfig, load_config
from sentinella.core.models import (
    ContainerMetrics,
    CpuMetrics,
    DiskMetrics,
    MemoryMetrics,
    NetworkMetrics,
    SensorMetrics,
    SystemInfoMetrics,
    SystemSnapshot,
)
from sentinella.core.plugin_manager import get_enabled_plugins

log = logging.getLogger(__name__)

# Every Collector used to register its own bound atexit handler, and only an
# explicit close() unregistered it — so a process that built collectors over
# time (or a test session) accumulated handlers, each pinning a live thread
# pool. One module-level handler over a weak set does the same job and lets a
# dropped collector be garbage collected.
_LIVE_COLLECTORS: weakref.WeakSet[Collector] = weakref.WeakSet()


def _shutdown_live_collectors() -> None:
    for collector in list(_LIVE_COLLECTORS):
        try:
            collector._shutdown()
        except Exception:  # pragma: no cover - best effort at interpreter exit
            log.debug("Collector shutdown failed at exit", exc_info=True)


atexit.register(_shutdown_live_collectors)


def _register_for_shutdown(collector: Collector) -> None:
    _LIVE_COLLECTORS.add(collector)


class Collector:
    """Collects system metrics from all enabled plugins."""

    def __init__(self, config: SentinellaConfig | None = None) -> None:
        self.config = config or load_config()
        self.plugins = get_enabled_plugins(self.config)
        # Snapshot fields backed by a live plugin.  Anything absent here is
        # reported as "not monitored" rather than as a default-zero metric,
        # so a disabled module cannot masquerade as an idle one.
        self.active_modules: frozenset[str] = frozenset(p.name for p in self.plugins)
        # Instance-level executor — properly cleaned up on shutdown
        self._executor = ThreadPoolExecutor(
            max_workers=min(len(self.plugins), 8) or 1,
            thread_name_prefix="sentinella-collector",
        )
        # Separate from _executor: that pool's workers *are* the plugin calls,
        # so dispatching the outer collect() onto it could deadlock when every
        # worker is busy. Two threads is plenty — collect() serialises on
        # _cache_lock anyway.
        self._async_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="sentinella-collect-async"
        )
        self._last_snapshot: SystemSnapshot | None = None
        self._last_collected_at: float = 0.0
        self._cache_lock = threading.Lock()
        # Tracks each plugin's most recently submitted future by name, across
        # collect() calls. A plugin whose call is still running when the next
        # cycle starts is *not* resubmitted — see collect()'s inflight check.
        self._inflight: dict[str, Any] = {}
        _register_for_shutdown(self)
        log.info(
            "Collector initialised with %d plugins: %s",
            len(self.plugins),
            [p.name for p in self.plugins],
        )

    def _shutdown(self) -> None:
        """Shut down the thread pool gracefully on exit."""
        for plugin in self.plugins:
            try:
                plugin.close()
            except Exception:
                log.debug("Plugin %s close() failed", plugin.name, exc_info=True)
        for executor in (self._async_executor, self._executor):
            try:
                executor.shutdown(wait=True, cancel_futures=True)
            except Exception:
                try:
                    executor.shutdown(wait=False)
                except Exception:
                    pass

    def close(self) -> None:
        """Explicitly shut down the executor and drop the atexit registration."""
        _LIVE_COLLECTORS.discard(self)
        self._shutdown()

    async def close_async(self) -> None:
        """Clean up asynchronous collector resources."""
        self.close()

    @property
    def last_collected_at(self) -> float:
        """Unix time of the most recent completed collection (0.0 if never).

        Public because the API layer needs it to timestamp module responses;
        it used to reach into ``_last_collected_at`` directly.
        """
        return self._last_collected_at

    def collect(self) -> SystemSnapshot:
        """Run all plugins in parallel and return a snapshot, using TTL cache if fresh."""
        now = time.time()
        ttl = max(0.5, self.config.general.refresh_interval / 2.0)

        # Fast path: check cache without lock
        if self._last_snapshot and (now - self._last_collected_at < ttl):
            return self._last_snapshot

        # Non-blocking lock acquire to prevent thread accumulation when a call hangs
        acquired = self._cache_lock.acquire(blocking=False)
        if not acquired:
            if self._last_snapshot:
                return self._last_snapshot
            # If no snapshot is cached yet, wait for the lock
            self._cache_lock.acquire()

        try:
            # Double check cache inside lock using the fresh current time
            if self._last_snapshot and (time.time() - self._last_collected_at < ttl):
                return self._last_snapshot

            results: dict[str, Any] = {}
            futures: dict[Any, Any] = {}
            for plugin in self.plugins:
                prior = self._inflight.get(plugin.name)
                if prior is not None and not prior.done():
                    # *Still running* from an earlier cycle (past its own
                    # deadline) — reuse it instead of submitting a second
                    # call on top of it. Resubmitting a hung plugin on every
                    # cycle is what exhausts the pool: each hang would
                    # permanently claim another worker on top of the ones
                    # already stuck.
                    #
                    # A future that has *finished* must not be reused: reading
                    # its result again would replay the previous cycle's
                    # reading, which halved the effective refresh rate and made
                    # every value appear twice.
                    futures[prior] = plugin
                    continue
                self._inflight.pop(plugin.name, None)
                fut = self._executor.submit(plugin.collect)
                self._inflight[plugin.name] = fut
                futures[fut] = plugin

            try:
                for future in as_completed(futures, timeout=10):
                    plugin = futures[future]
                    try:
                        results[plugin.name] = future.result()
                    except Exception:
                        log.exception("Plugin %s failed during collection", plugin.name)
            except TimeoutError:
                stuck = sorted(p.name for f, p in futures.items() if not f.done())
                log.warning(
                    "Plugins still running past the 10s collection deadline: %s "
                    "— using stale/default values for them this cycle",
                    stuck,
                )
            finally:
                # Drop every future that finished this cycle so ``_inflight``
                # only ever holds genuinely stuck calls — otherwise it pins a
                # completed future (and its result) until the next cycle.
                for fut, plugin in futures.items():
                    if fut.done() and self._inflight.get(plugin.name) is fut:
                        del self._inflight[plugin.name]

            self._last_snapshot = self._assemble(results, self._process_count(results))
            self._last_collected_at = time.time()  # set to the exact completion time
            return self._last_snapshot
        finally:
            self._cache_lock.release()

    def collect_module(self, name: str) -> Any:
        """Collect metrics for a single module, checking TTL cache or querying the plugin.

        Returns ``None`` for modules with no active plugin, so callers can tell
        "not monitored" apart from a genuine all-zero reading.
        """
        if name not in self.active_modules:
            return None

        now = time.time()
        ttl = max(0.5, self.config.general.refresh_interval / 2.0)

        # Fast path check
        if self._last_snapshot and (now - self._last_collected_at < ttl):
            return getattr(self._last_snapshot, name, None)

        # Expired: trigger a full thread-safe collection to refresh the snapshot cache
        self.collect()
        if self._last_snapshot:
            return getattr(self._last_snapshot, name, None)
        return None

    # These run on the collector's *own* pool rather than the event loop's
    # default executor. A collect that blocks (a wedged plugin, a slow Docker
    # daemon) would otherwise occupy a thread shared with everything else on
    # the loop; keeping it in-house means the damage is bounded to collection.
    async def collect_module_async(self, name: str) -> Any:
        """Async wrapper around collect_module."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._async_executor, self.collect_module, name)

    async def collect_async(self) -> SystemSnapshot:
        """Run collection in a thread executor (non-blocking for async apps)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._async_executor, self.collect)

    def set_collect_cmdline(self, enabled: bool) -> None:
        """Ask the processes plugin to include (or skip) command lines.

        Gathering cmdline costs a read per PID and nothing renders it, so it is
        off by default; ``sentinella print --format json/csv`` turns it on because
        its output carries the field.  Drops the cached snapshot so the next
        collect actually reflects the change.
        """
        for plugin in self.plugins:
            if plugin.name == "processes":
                if getattr(plugin, "collect_cmdline", None) != enabled:
                    plugin.collect_cmdline = enabled  # type: ignore[attr-defined]
                    with self._cache_lock:
                        self._last_snapshot = None
                        self._last_collected_at = 0.0
                return

    def collect_processes(self, sort_by: str) -> list[Any]:
        """Re-run the processes plugin with an explicit sort key.

        Bypasses the snapshot cache. The cached snapshot's process list was
        truncated using whichever sort key was active when it was collected;
        re-sorting that already-truncated slice by a different key can hide
        processes that would rank in range under the new key. Called
        on-demand (e.g. the TUI's sort-cycling action), not on every tick.
        """
        from sentinella.plugins.processes import ProcessesPlugin

        for plugin in self.plugins:
            if isinstance(plugin, ProcessesPlugin):
                return plugin.collect(sort_by=sort_by)
        return []

    async def collect_processes_async(self, sort_by: str) -> list[Any]:
        """Async wrapper around collect_processes."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._async_executor, self.collect_processes, sort_by)

    # ── private ──────────────────────────────────────────────────────────

    @staticmethod
    def _process_count(results: dict[str, Any]) -> int:
        """Total processes on the host, before display truncation.

        The processes plugin truncates its return value, so its length would
        understate the real count.  The total travels *with* the list (see
        ``ProcessListing``) rather than being read back off the plugin object,
        which is written from a worker thread and could describe a different
        cycle than the list it is paired with.
        """
        procs = results.get("processes") or []
        return getattr(procs, "total", len(procs))

    @staticmethod
    def _assemble(results: dict[str, Any], process_count: int = 0) -> SystemSnapshot:
        """Map plugin results to the ``SystemSnapshot`` fields.

        Processes arrive pre-sorted by ``config.processes.sort_by`` (the plugin
        must sort before truncating); consumers may re-sort the returned slice.
        """
        procs = results.get("processes", [])
        if not isinstance(procs, list):
            procs = []

        return SystemSnapshot(
            timestamp=time.time(),
            process_count=process_count or len(procs),
            cpu=results.get("cpu", CpuMetrics()),
            memory=results.get("memory", MemoryMetrics()),
            disk=results.get("disk", DiskMetrics()),
            network=results.get("network", NetworkMetrics()),
            processes=procs,
            users=results.get("users", []),
            sensors=results.get("sensors", SensorMetrics()),
            containers=results.get("containers", ContainerMetrics()),
            system_info=results.get("system_info", SystemInfoMetrics()),
        )
