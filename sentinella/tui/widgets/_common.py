"""Small helpers shared across dashboard widgets."""

from __future__ import annotations

from rich.text import Text

from sentinella.core.utils import render_bar
from sentinella.tui.theme import Palette


def section_header(text: Text, title: str, palette: Palette) -> None:
    """Append a widget's bold section header line (e.g. "  CPU\\n").

    The colour comes from the palette rather than a literal, so headings follow
    the active theme instead of staying dark-theme cyan on a light surface.
    """
    text.append(f"  {title}\n", style=f"bold {palette.title}")


def themed_bar(pct: float | None, width: int, palette: Palette) -> Text:
    """``render_bar`` with both halves coloured from the active theme.

    ``render_bar``'s defaults are the fixed console palette; the dashboard needs
    the theme-resolved one for the fill and something better than ``dim`` for
    the empty half.
    """
    return render_bar(
        pct,
        width=width,
        style_color=palette.for_percent(pct),
        empty_color=palette.muted,
    )


def more_row(text: Text, hidden: int, noun: str, palette: Palette) -> None:
    """Append a "+N more" line so a truncated list reads as truncated.

    Widgets cap their lists to fit the panel; without this the extra rows are
    silently dropped and the panel looks complete.
    """
    if hidden > 0:
        text.append(f"  +{hidden} more {noun}\n", style=palette.muted)
