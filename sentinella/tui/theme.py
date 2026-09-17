"""Resolved theme colours for dashboard widgets.

Widgets render with Rich ``Text``, and a Rich style cannot name a Textual
design token: ``Static.update()`` funnels Rich text through
``Content.from_rich_text``, which parses styles with Rich's colour parser, so
``style="$success"`` raises ``MissingStyle``. The ``.tcss`` token classes
therefore cannot reach inline spans either.

So the tokens are resolved here instead, off the app's active theme, and handed
to widgets as plain hex. That keeps the palette in one place and — unlike the
hardcoded dark-theme literals this replaces — makes it follow the theme:
``#00d2ff`` bars sat at 1.26:1 against ``textual-light``'s ``#D8D8D8`` surface.
Every role below clears 3:1 in both built-in themes.
"""

from __future__ import annotations

import weakref
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from textual.color import Color

from sentinella.core.utils import (
    LEVEL_CRIT,
    LEVEL_WARN,
    PERCENT_THRESHOLDS,
    level_for_percent,
    level_for_temp,
)

# Role -> Textual theme variable. The ``$text-*`` variants are the
# auto-contrast ones: Textual tunes them per theme against the surface, where
# bare ``$success``/``$warning``/``$error`` are single values picked for dark
# backgrounds. ``$text-primary`` (blue) rather than ``$text-accent`` for titles
# because accent and warning resolve to the *same* colour in both built-in
# themes, which would make a panel heading indistinguishable from a warning.
_ROLE_VARS: dict[str, str] = {
    "title": "text-primary",
    "value": "foreground",
    "good": "text-success",
    "warn": "text-warning",
    "crit": "text-error",
}

# How far to blend the foreground into the surface for secondary text. Replaces
# Rich's ``dim``, which on a light surface renders as near-invisible grey.
_MUTED_BLEND = 0.45

_FALLBACK = {
    "title": "#57A5E2",
    "value": "#E0E0E0",
    "good": "#8AD4A1",
    "warn": "#FFC473",
    "crit": "#D17E92",
    "muted": "#888888",
}


@dataclass(frozen=True)
class Palette:
    """Theme-resolved hex colours for one semantic role each."""

    title: str
    value: str
    muted: str
    good: str
    warn: str
    crit: str

    def for_level(self, level: str) -> str:
        """Colour for a ``sentinella.core.utils`` severity level."""
        if level == LEVEL_CRIT:
            return self.crit
        if level == LEVEL_WARN:
            return self.warn
        return self.good

    def for_percent(
        self, pct: float | None, thresholds: tuple[float, float] = PERCENT_THRESHOLDS
    ) -> str:
        return self.for_level(level_for_percent(pct, thresholds))

    def for_temp(
        self, celsius: float, high: float | None = None, critical: float | None = None
    ) -> str:
        return self.for_level(level_for_temp(celsius, high, critical))


# Resolving means a dict build plus colour parsing, and widgets repaint on every
# refresh tick, so cache per (app, theme name): a theme switch misses the cache,
# and the entry dies with the app rather than leaking across app instances (or
# pinning stale colours for a theme that was redefined under the same name).
_cache: MutableMapping[Any, dict[str, Palette]] = weakref.WeakKeyDictionary()


def _resolve(variables: dict[str, str]) -> Palette:
    def var(role: str) -> str:
        value = variables.get(_ROLE_VARS[role], "")
        # "auto 87%"-style values are Textual pseudo-colours Rich cannot parse.
        if not value.startswith("#"):
            return _FALLBACK[role]
        return value

    value_hex = var("value")
    surface = variables.get("surface", "")
    try:
        muted = Color.parse(value_hex).blend(Color.parse(surface), _MUTED_BLEND).hex
    except Exception:
        muted = _FALLBACK["muted"]

    return Palette(
        title=var("title"),
        value=value_hex,
        muted=muted,
        good=var("good"),
        warn=var("warn"),
        crit=var("crit"),
    )


def palette_for(widget) -> Palette:
    """Return the palette for *widget*'s app, falling back to dark defaults.

    Widgets call this during ``update_data``; a widget that is not mounted (or
    an app with no theme yet) gets the fallback rather than raising.
    """
    try:
        app = widget.app
        theme_name = app.theme
    except Exception:
        return Palette(**_FALLBACK)

    try:
        per_app = _cache.setdefault(app, {})
    except TypeError:  # pragma: no cover - a non-weakrefable stand-in app
        per_app = {}

    cached = per_app.get(theme_name)
    if cached is not None:
        return cached

    try:
        palette = _resolve(app.get_css_variables())
    except Exception:
        palette = Palette(**_FALLBACK)
    per_app[theme_name] = palette
    return palette
