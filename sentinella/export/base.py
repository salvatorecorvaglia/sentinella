"""Abstract base class for export formatters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from sentinella.config import SentinellaConfig
from sentinella.core.models import SystemSnapshot
from sentinella.core.sort import sort_processes


class BaseExporter(ABC):
    """Format a ``SystemSnapshot`` as a string in a given format."""

    name: str = "base"

    def __init__(self, config: SentinellaConfig | None = None) -> None:
        # Exporters honour processes.max_display / processes.sort_by, so the
        # same limits apply to `sentinella print` as to the TUI.
        self.config = config or SentinellaConfig()

    def sorted_processes(self, snapshot: SystemSnapshot) -> list:
        """Processes sorted by ``processes.sort_by`` and capped at ``max_display``."""
        procs = sort_processes(snapshot.processes, self.config.processes.sort_by)
        return procs[: self.config.processes.max_display]

    @abstractmethod
    def format(self, snapshot: SystemSnapshot, modules: list[str] | None = None) -> str:
        """Return the snapshot formatted as a string.

        Parameters
        ----------
        snapshot:
            The collected metrics.
        modules:
            Optional list of module names to include.  ``None`` = all.
        """
