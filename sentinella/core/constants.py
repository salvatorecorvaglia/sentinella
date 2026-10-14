"""Names shared by the CLI, the config, the API, the remote client and the TUI.

Free of web-framework imports on purpose: ``sentinella print`` validates module
names against this, and importing them from the API module pulled in FastAPI.
"""

from __future__ import annotations

from dataclasses import fields

from sentinella.core.models import SystemSnapshot
from sentinella.core.sort import PROCESS_SORT_KEYS

# Every SystemSnapshot field that carries one module's metrics — derived from
# the model, so adding a module there is enough for every surface to accept it.
MODULE_NAMES: frozenset[str] = frozenset(f.name for f in fields(SystemSnapshot)) - {
    "timestamp",
    "process_count",
}

# Valid ``processes.sort_by`` values, in the order the TUI's `p` key cycles.
PROCESS_SORT_ORDER: tuple[str, ...] = tuple(PROCESS_SORT_KEYS)

# Valid ``general.theme`` values.
THEMES: tuple[str, ...] = ("dark", "light")

# Valid ``export.format`` / ``sentinella print --format`` values.
EXPORT_FORMATS: tuple[str, ...] = ("text", "csv", "json")

# Version of the HTTP/WebSocket payload contract, reported by /health. Bump
# it when a change would break an older client (a renamed or removed field),
# so the web dashboard and ``--remote`` can say they're talking to a
# mismatched agent instead of rendering garbage.
API_VERSION = 1
