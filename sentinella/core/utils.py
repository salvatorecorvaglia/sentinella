"""Shared utility helpers for Sentinella.

Centralised here to avoid duplication across plugins, widgets, and exporters.
"""

from __future__ import annotations

from typing import Any

import rich.text


def human_bytes(n: int | float) -> str:
    """Convert bytes to a human-readable string (e.g. ``1.5 GB``)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_bytes_compact(n: int | float) -> str:
    """Compact variant without decimal for small values (used in process tables)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if n >= 1 else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"


# Severity levels, ordered least to most severe. These are the vocabulary the
# whole app shares: ``level_for_*`` decides *which* level a reading is at, and
# each surface maps the level to its own colours — fixed hexes for console
# output (``color_for_*`` below), live theme colours for the TUI
# (``sentinella.tui.theme``), CSS classes for the web dashboard (``.metric-*``).
LEVEL_GOOD = "good"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"

# The thresholds themselves, named so they can be shipped to the web dashboard
# over /health rather than hand-copied into app.js and kept in sync by comment.
PERCENT_THRESHOLDS: tuple[float, float] = (50.0, 80.0)
# Fallback trip points for a sensor that reports none of its own.
TEMP_THRESHOLDS: tuple[float, float] = (70.0, 85.0)

# Level -> hex, for console output (``fetch`` and ``print``), which has no
# theme to consult and so must pick one palette and keep it.
_LEVEL_HEX: dict[str, str] = {
    LEVEL_GOOD: "#00d2ff",
    LEVEL_WARN: "#f59e0b",
    LEVEL_CRIT: "#ef4444",
}


def level_for_percent(
    pct: float | None, thresholds: tuple[float, float] = PERCENT_THRESHOLDS
) -> str:
    """Return the severity level of a percentage value.

    Parameters
    ----------
    pct:
        The percentage (0–100) or None.
    thresholds:
        ``(warn, crit)`` — below *warn* is good, below *crit* is warn,
        above is crit.
    """
    if pct is None:
        pct = 0.0
    warn, crit = thresholds
    if pct < warn:
        return LEVEL_GOOD
    elif pct < crit:
        return LEVEL_WARN
    return LEVEL_CRIT


def level_for_temp(
    celsius: float,
    high: float | None = None,
    critical: float | None = None,
) -> str:
    """Return the severity level of a temperature reading.

    Temperatures are °C, not percentages, so the sensor's own trip points are
    used when psutil reports them; the 70/85 °C fallback only applies when it
    does not.  Mirrors ``levelForTemp`` in the web dashboard.
    """
    if critical and celsius >= critical:
        return LEVEL_CRIT
    if high and celsius >= high:
        return LEVEL_WARN
    if not high and not critical:
        warn, crit = TEMP_THRESHOLDS
        if celsius >= crit:
            return LEVEL_CRIT
        if celsius >= warn:
            return LEVEL_WARN
    return LEVEL_GOOD


def color_for_percent(
    pct: float | None, thresholds: tuple[float, float] = PERCENT_THRESHOLDS
) -> str:
    """Return a fixed hex colour for a percentage value.

    For console output. The TUI wants colours that follow the active theme —
    see ``sentinella.tui.theme.palette_for``.
    """
    return _LEVEL_HEX[level_for_percent(pct, thresholds)]


def color_for_temp(
    celsius: float,
    high: float | None = None,
    critical: float | None = None,
) -> str:
    """Return a fixed hex colour for a temperature reading.

    For console output; see ``color_for_percent`` on why the TUI differs.
    """
    return _LEVEL_HEX[level_for_temp(celsius, high, critical)]


def text_sparkline(history) -> str:
    """Render a text-based sparkline from a sequence of floats.

    Returns a string of Unicode block characters.
    """
    if len(history) < 2:
        return ""
    spark_chars = " ▂▃▄▅▆▇█"
    max_val = max(history) or 1
    spark = ""
    for v in history:
        val = v if v is not None else 0.0
        idx = int(val / max_val * (len(spark_chars) - 1))
        idx = max(0, min(idx, len(spark_chars) - 1))
        spark += spark_chars[idx]
    return spark


def render_bar(
    pct: float | None,
    width: int = 20,
    style_color: str | None = None,
    bracketed: bool = False,
    filled_char: str = "━",
    unfilled_char: str = "─",
    empty_color: str | None = None,
) -> rich.text.Text:
    """Render a sleek progress bar using Rich Text.

    Parameters
    ----------
    pct:
        The percentage (0-100) or None.
    width:
        The character width of the filled/unfilled portion of the bar.
    style_color:
        Explicit colour name, or None to determine based on percentage thresholds.
    bracketed:
        If True, wraps the bar with dim brackets '[ ]'.
    filled_char:
        Character used for filled portion (default '━').
    unfilled_char:
        Character used for empty portion (default '─').
    empty_color:
        Colour for the unfilled portion and brackets, or None for Rich's
        ``dim``. The TUI passes a theme colour here because ``dim`` is
        near-invisible on a light surface.
    """
    safe_pct = 0.0 if pct is None else pct
    filled = int(width * safe_pct / 100)
    filled = max(0, min(filled, width))
    color = style_color or color_for_percent(safe_pct)
    empty = empty_color or "dim"
    t = rich.text.Text()
    if bracketed:
        t.append("[", style=empty)
    t.append(filled_char * filled, style=color)
    t.append(unfilled_char * (width - filled), style=empty)
    if bracketed:
        t.append("]", style=empty)
    t.append(f" {safe_pct:5.1f}%", style=f"bold {color}")
    return t


def truncate_path(path: str, width: int) -> str:
    """Shorten *path* to *width* cells, keeping the end.

    Mount paths share their prefix, so trimming the tail is what destroys the
    distinguishing part: six APFS volumes all render as "/System/Volume".
    Keeping the end and marking the cut with a leading "…" preserves the part
    that actually identifies the mount.
    """
    if width <= 0:
        return ""
    if len(path) <= width:
        return path
    if width == 1:
        return "…"
    return "…" + path[-(width - 1) :]


def serialize_model(obj: Any) -> Any:
    """Recursively convert Sentinella models to dict/list structures.

    Bypasses slow dataclasses.asdict.
    """
    if hasattr(obj, "__dataclass_fields__"):
        return {f: serialize_model(getattr(obj, f)) for f in obj.__dataclass_fields__}
    elif isinstance(obj, list):
        return [serialize_model(x) for x in obj]
    elif isinstance(obj, dict):
        return {k: serialize_model(v) for k, v in obj.items()}
    else:
        return obj
