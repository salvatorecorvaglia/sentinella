"""Abstract base class for Sentinella monitoring plugins.

Every plugin must subclass ``MonitorPlugin`` and implement ``collect()``
and ``is_available()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sentinella.config import SentinellaConfig


class MonitorPlugin(ABC):
    """Base class for all monitoring plugins."""

    name: str = "unnamed"
    category: str = "general"

    def __init__(self, config: SentinellaConfig | None = None) -> None:
        """Every plugin accepts the config, whether or not it uses it.

        A uniform signature lets ``get_enabled_plugins`` construct plugins
        without inspecting each constructor to guess what it accepts.
        """
        self.config = config

    @abstractmethod
    def collect(self) -> Any:
        """Collect and return a metric dataclass."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this plugin can operate on the current platform."""

    def close(self) -> None:  # noqa: B027 - intentional no-op default hook
        """Release any resources this plugin holds. Default: nothing to do.

        Overridden by plugins that own long-lived resources (e.g. a reused
        thread pool), so ``Collector`` can clean them up generically without
        needing to know which plugins allocate what.
        """

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
