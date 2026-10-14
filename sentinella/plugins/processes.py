"""Process listing plugin."""

from __future__ import annotations

import logging
import threading
import time

import psutil

from sentinella.config import SentinellaConfig
from sentinella.core.models import ProcessInfo
from sentinella.core.sort import PROCESS_SORT_KEYS
from sentinella.plugins.base import MonitorPlugin

log = logging.getLogger(__name__)


class ProcessListing(list[ProcessInfo]):
    """The truncated process list, carrying the host's untruncated total.

    A plain ``list`` subclass so every existing consumer keeps working; the
    count rides along with the list it describes instead of being read off the
    plugin object from another thread.
    """

    def __init__(self, iterable=(), total: int = 0) -> None:
        super().__init__(iterable)
        self.total = total


class ProcessesPlugin(MonitorPlugin):
    name = "processes"
    category = "processes"

    config: SentinellaConfig

    def __init__(self, config: SentinellaConfig | None = None) -> None:
        # Built-in defaults when constructed bare (tests, library use) — never
        # a load_config(), which would read whatever sentinella.toml happens
        # to be in the working directory.
        super().__init__(config or SentinellaConfig())
        # Off by default; the exporters flip it on because their output
        # includes the field. See collect().
        self.collect_cmdline = False
        # The most recent full scan: (monotonic time, raw rows, had cmdline).
        # Reused by a call that lands shortly after it — see _scan().
        self._scan_lock = threading.Lock()
        self._last_scan: tuple[float, list[dict], bool] | None = None

    def is_available(self) -> bool:
        return True

    def collect(
        self, sort_by: str | None = None, *, with_cmdline: bool | None = None
    ) -> ProcessListing:
        """Collect and truncate the process list.

        ``sort_by`` overrides ``config.processes.sort_by`` for this call only
        — used by ``Collector.collect_processes`` so the TUI can request a
        truncation consistent with whichever sort key is currently active,
        rather than always truncating by the key fixed at construction time.
        """
        limit = max(100, self.config.processes.max_display * 2)

        # Reading cmdline means an extra per-process read (/proc/<pid>/cmdline
        # on Linux, a syscall elsewhere) for every PID on the host — it was
        # ~77% of the whole collect. Nothing renders it: not the TUI, not the
        # web dashboard. Only the JSON and CSV exporters do, so they ask for it
        # and every other caller skips the cost.
        want_cmdline = self.collect_cmdline if with_cmdline is None else with_cmdline
        raw_procs = self._scan(want_cmdline)

        # Sort by the *configured* key before truncating.  Sorting by CPU here
        # unconditionally (as this once did) meant that with sort_by="memory" a
        # RAM-heavy but idle process could be cut before any consumer saw it.
        # sorted(), not .sort(): the rows may be the shared cached scan.
        sort_attr, descending = PROCESS_SORT_KEYS.get(
            sort_by or self.config.processes.sort_by, PROCESS_SORT_KEYS["cpu"]
        )
        if sort_attr == "name":
            raw_procs = sorted(raw_procs, key=lambda item: (item.get("name") or "").lower())
        else:
            raw_procs = sorted(
                raw_procs,
                key=lambda item: (
                    item.get(sort_attr) or 0,
                    item.get("memory_percent") or 0.0,
                ),
                reverse=descending,
            )

        procs: ProcessListing = ProcessListing(total=len(raw_procs))
        for info in raw_procs[:limit]:
            # Gated on this call's request: a reused scan may carry cmdline
            # that an earlier caller asked for.
            cmdline = (info.get("cmdline") or []) if want_cmdline else []
            mem_info = info.get("memory_info")
            cmd_str = " ".join(cmdline) if isinstance(cmdline, (list, tuple)) else str(cmdline)
            procs.append(
                ProcessInfo(
                    pid=info.get("pid", 0) or 0,
                    name=info.get("name") or "",
                    username=info.get("username") or "",
                    cpu_percent=round(info.get("cpu_percent", 0.0) or 0.0, 1),
                    memory_percent=round(info.get("memory_percent", 0.0) or 0.0, 1),
                    status=info.get("status") or "",
                    cmdline=cmd_str,
                    num_threads=info.get("num_threads", 0) or 0,
                    memory_rss=getattr(mem_info, "rss", 0) if mem_info else 0,
                )
            )
        return procs

    def _scan(self, want_cmdline: bool) -> list[dict]:
        """Return raw per-process rows, reusing a scan younger than the cache TTL.

        psutil's ``cpu_percent`` is the usage *since the previous call on the
        same cached Process*, so scanning again moments after a scan measures a
        window of a few milliseconds and reports noise. That is what happened
        when the TUI re-collected with a different sort key right after the
        regular tick. Within the collector's own cache TTL a fresh scan cannot
        say anything new anyway, so the previous rows are reused — unless they
        lack cmdline and this call wants it.
        """
        ttl = max(0.5, self.config.general.refresh_interval / 2.0)
        with self._scan_lock:
            now = time.monotonic()
            if self._last_scan is not None:
                scanned_at, rows, had_cmdline = self._last_scan
                if now - scanned_at < ttl and (had_cmdline or not want_cmdline):
                    return rows
            rows = self._iter_processes(want_cmdline)
            self._last_scan = (now, rows, want_cmdline)
            return rows

    @staticmethod
    def _iter_processes(want_cmdline: bool) -> list[dict]:
        attrs = [
            "pid",
            "name",
            "username",
            "cpu_percent",
            "memory_percent",
            "status",
            "num_threads",
            "memory_info",
        ]
        if want_cmdline:
            attrs.append("cmdline")

        raw_procs: list[dict] = []
        try:
            for proc in psutil.process_iter(attrs=attrs):
                try:
                    info = proc.info
                    if info and info.get("pid") is not None:
                        raw_procs.append(info)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            log.debug("Process iteration encountered error", exc_info=True)
        return raw_procs
