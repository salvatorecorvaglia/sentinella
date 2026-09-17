"""Memory widget — RAM and swap usage bars with sparkline history."""

from __future__ import annotations

from collections import deque

from rich.text import Text
from textual.widgets import Static

from sentinella.core.models import SystemSnapshot
from sentinella.core.utils import human_bytes, text_sparkline
from sentinella.tui.theme import palette_for
from sentinella.tui.widgets._common import section_header, themed_bar


class MemoryWidget(Static):
    """Memory panel with RAM/swap bars and sparkline."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: deque[float] = deque(maxlen=60)

    def update_data(self, snap: SystemSnapshot) -> None:
        # See CpuWidget: history is widget state, not App state.
        mem = snap.memory
        self._history.append(mem.percent)

        palette = palette_for(self)
        text = Text()
        section_header(text, "Memory", palette)

        # RAM bar
        text.append("  RAM   ")
        text.append_text(themed_bar(mem.percent, 20, palette))
        text.append(f"  {human_bytes(mem.used)} / {human_bytes(mem.total)}\n", style=palette.muted)

        # Swap bar
        if mem.swap_total > 0:
            text.append("  Swap  ")
            text.append_text(themed_bar(mem.swap_percent, 20, palette))
            text.append(
                f"  {human_bytes(mem.swap_used)} / {human_bytes(mem.swap_total)}\n",
                style=palette.muted,
            )

        # History sparkline
        spark = text_sparkline(self._history)
        if spark:
            text.append("  Trend ")
            text.append(spark, style=palette.for_percent(mem.percent))
            text.append("\n")

        # Available
        text.append(f"  Available: {human_bytes(mem.available)}\n", style=palette.muted)

        self.update(text)
