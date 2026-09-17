"""Process listing plugin."""

from __future__ import annotations

import logging

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

    def __init__(self, config: SentinellaConfig | None = None) -> None:
        super().__init__(config)
        from sentinella.config import load_config

        self._config = config or load_config()
        # Total processes seen on the last collect, before display truncation.
        # Kept for backwards compatibility; the authoritative value now travels
        # with the returned list (see ProcessListing) because this attribute is
        # written from a worker thread and read from the collector thread, so it
        # could report a different cycle's count than the list beside it.
        self.total_count = 0
        # Off by default; the exporters flip it on because their output
        # includes the field. See collect().
        self.collect_cmdline = False

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
        limit = max(100, self._config.processes.max_display * 2)

        # Reading cmdline means an extra per-process read (/proc/<pid>/cmdline
        # on Linux, a syscall elsewhere) for every PID on the host — it was
        # ~77% of the whole collect. Nothing renders it: not the TUI, not the
        # web dashboard. Only the JSON and CSV exporters do, so they ask for it
        # and every other caller skips the cost.
        want_cmdline = self.collect_cmdline if with_cmdline is None else with_cmdline

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

        self.total_count = len(raw_procs)

        # Sort by the *configured* key before truncating.  Sorting by CPU here
        # unconditionally (as this once did) meant that with sort_by="memory" a
        # RAM-heavy but idle process could be cut before any consumer saw it.
        sort_attr, descending = PROCESS_SORT_KEYS.get(
            sort_by or self._config.processes.sort_by, PROCESS_SORT_KEYS["cpu"]
        )
        if sort_attr == "name":
            raw_procs.sort(key=lambda item: (item.get("name") or "").lower())
        else:
            raw_procs.sort(
                key=lambda item: (
                    item.get(sort_attr) or 0,
                    item.get("memory_percent") or 0.0,
                ),
                reverse=descending,
            )

        procs: ProcessListing = ProcessListing(total=len(raw_procs))
        for info in raw_procs[:limit]:
            cmdline = info.get("cmdline") or []
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
