"""CPU widget — overall usage, per-core bars, frequency, sparkline history."""

from __future__ import annotations

from collections import deque

from rich.text import Text
from textual.widgets import Static

from sentinella.core.models import SystemSnapshot
from sentinella.core.utils import text_sparkline
from sentinella.tui.theme import palette_for
from sentinella.tui.widgets._common import section_header, themed_bar


class CpuWidget(Static):
    """CPU panel with per-core bars and sparkline."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._history: deque[float] = deque(maxlen=60)

    def update_data(self, snap: SystemSnapshot) -> None:
        # History lives on the widget: it persists for the widget's lifetime,
        # which is the app's lifetime, without monkey-patching the App object.
        cpu = snap.cpu
        self._history.append(cpu.percent_overall)

        palette = palette_for(self)
        text = Text()
        section_header(text, "CPU", palette)

        # Overall bar
        text.append("  Overall  ")
        text.append_text(themed_bar(cpu.percent_overall, 15, palette))
        text.append("\n")

        # History sparkline
        spark = text_sparkline(self._history)
        if spark:
            text.append("  History  ")
            text.append(spark, style=palette.for_percent(cpu.percent_overall))
            text.append("\n")

        # Per-core bars (compact: 2 or 4 per line depending on core count)
        cores = cpu.percent_per_core
        cols = 4 if len(cores) > 16 else 2
        bar_width = 6 if cols == 4 else 10
        for i in range(0, len(cores), cols):
            line = Text("  ")
            for j in range(cols):
                idx = i + j
                if idx < len(cores):
                    line.append(f"C{idx:<2} ")
                    line.append_text(themed_bar(cores[idx], bar_width, palette))
                    line.append("  ")
            text.append_text(line)
            text.append("\n")

        # Frequency and load
        info_parts: list[str] = []
        if cpu.frequency_current_mhz:
            info_parts.append(f"Freq: {cpu.frequency_current_mhz:.0f} MHz")
        # See TextExporter: a partial load tuple must not crash the panel.
        if None not in (cpu.load_avg_1, cpu.load_avg_5, cpu.load_avg_15):
            info_parts.append(
                f"Load: {cpu.load_avg_1:.2f} {cpu.load_avg_5:.2f} {cpu.load_avg_15:.2f}"
            )
        if info_parts:
            text.append("  " + "  │  ".join(info_parts) + "\n", style=palette.muted)

        self.update(text)
