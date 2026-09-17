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

    This used to declare only the four snapshot/lifecycle methods, while
    ``core.api.create_base_app`` also required ``collect_module_async`` and
    ``active_modules`` and read the private ``_last_collected_at``.  A
    ``RemoteCollector`` therefore satisfied the protocol and would still have
    raised ``AttributeError`` on ``/api/v1/{module}``.  The protocol now
    describes what a collector actually has to provide to serve the API.
    """

    #: Modules this collector reports on.  Lets a caller distinguish
    #: "not monitored" from a genuine all-zero reading.
    active_modules: frozenset[str]

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

    def close(self) -> None:
        """Clean up synchronous collector resources."""
        ...

    async def close_async(self) -> None:
        """Clean up asynchronous collector resources."""
        ...
