"""Protocol defining the collector interface.

Both the local ``Collector`` and ``RemoteCollector`` implement this
protocol so that TUI, web, and exporters can work with either seamlessly.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sentinella.core.models import SystemSnapshot


@runtime_checkable
class MetricCollector(Protocol):
    """Interface that all metric collectors must satisfy.

    Everything the TUI, the exporters and ``server.api.create_base_app`` call
    on a collector, so anything that satisfies it can back any of them.
    Capabilities a collector can't offer are still implemented, and say so
    through their return value (see ``collect_processes_async``) rather than
    by being absent.
    """

    @property
    def active_modules(self) -> frozenset[str]:
        """Modules this collector reports on.

        Lets a caller distinguish "not monitored" from a genuine all-zero
        reading. Read-only: ``RemoteCollector`` derives it from the agent.
        """
        ...

    @property
    def last_collected_at(self) -> float:
        """Unix time of the most recent completed collection (0.0 if never)."""
        ...

    def collect(self) -> SystemSnapshot:
        """Run a synchronous collection and return a snapshot."""
        ...

    async def collect_async(self) -> SystemSnapshot:
        """Run an async collection and return a snapshot."""
        ...

    async def collect_module_async(self, name: str) -> Any:
        """Return one module's metrics, or ``None`` if it isn't monitored."""
        ...

    def set_collect_cmdline(self, enabled: bool) -> None:
        """Ask for process command lines in later snapshots (costly; off by default).

        A collector that can't honour this (a remote agent decides for itself,
        via ``[remote] expose_cmdline``) treats it as a no-op.
        """
        ...

    async def collect_processes_async(self, sort_by: str) -> list[Any] | None:
        """Re-collect the process list ranked by *sort_by* before truncation.

        ``None`` means this collector can't (a remote agent only ranks by its
        own configured key); the caller keeps the snapshot's list.
        """
        ...

    def close(self) -> None:
        """Clean up synchronous collector resources."""
        ...

    async def close_async(self) -> None:
        """Clean up asynchronous collector resources."""
        ...
