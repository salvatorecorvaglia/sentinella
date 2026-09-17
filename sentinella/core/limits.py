"""How many rows each surface shows for a variable-length list.

Previously every surface picked its own caps inline — the TUI showed 6
partitions, ``sentinella print`` 8, and the web dashboard all of them — so the same
host described itself differently depending on where you looked, and nothing
told you a list had been cut short.

Two profiles, because the constraint genuinely differs: a dashboard panel has a
fixed number of cells to draw in, while ``print`` writes to a scrollback buffer
and is often piped somewhere. The dashboards share one profile so the TUI and
the web agree, which is the case that was confusing.

Surfaces that truncate should say so — the TUI appends a "+N more" row via
``sentinella.tui.widgets._common.more_row``.
"""

from __future__ import annotations

# Shared by the TUI panels and the web dashboard cards. Sized so that a TUI
# panel's header, its rows, its "+N more" line and any footer all fit the cells
# dashboard.tcss gives it — a Static clips silently, so a limit one too high
# loses the bottom row with no indication.
DASHBOARD_LIMITS: dict[str, int] = {
    "partitions": 5,
    "interfaces": 5,
    "temperatures": 6,
    "fans": 4,
    "containers": 8,
    "users": 5,
}

# ``sentinella print`` — no fixed viewport, so it can afford more rows.
EXPORT_LIMITS: dict[str, int] = {
    "partitions": 8,
    "interfaces": 6,
    "temperatures": 8,
    "fans": 4,
    "containers": 10,
}
