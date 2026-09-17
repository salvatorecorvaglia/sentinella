"""Shared process sort-key definitions.

Single source of truth for how a ``processes.sort_by`` config value maps to a
``ProcessInfo`` attribute and sort direction — previously duplicated across
``export/base.py``, ``plugins/processes.py``, and
``tui/widgets/process_table.py``.
"""

from __future__ import annotations

from sentinella.core.models import ProcessInfo

# sort_by config value -> (ProcessInfo attribute name, sort descending?)
PROCESS_SORT_KEYS: dict[str, tuple[str, bool]] = {
    "cpu": ("cpu_percent", True),
    "memory": ("memory_percent", True),
    "pid": ("pid", False),
    "name": ("name", False),
}

DEFAULT_SORT_BY = "cpu"


def sort_processes(processes: list[ProcessInfo], sort_by: str) -> list[ProcessInfo]:
    """Sort ``ProcessInfo`` objects by a ``processes.sort_by`` config value."""
    attr, descending = PROCESS_SORT_KEYS.get(sort_by, PROCESS_SORT_KEYS[DEFAULT_SORT_BY])
    if attr == "name":
        return sorted(processes, key=lambda p: (p.name or "").lower())
    return sorted(processes, key=lambda p: getattr(p, attr, 0) or 0, reverse=descending)
