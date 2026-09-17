"""CPU monitoring plugin."""

from __future__ import annotations

import os
import threading

import psutil

from sentinella.core.models import CpuMetrics
from sentinella.plugins.base import MonitorPlugin


def _busy_percent(previous, current) -> float:
    """Percentage of the interval between two ``cpu_times`` samples spent busy.

    The same calculation ``psutil.cpu_percent`` performs internally; done here
    so the figure depends on this plugin's own previous sample rather than on
    per-thread state inside psutil (see ``CpuPlugin``).
    """
    prev_all, curr_all = sum(previous), sum(current)
    prev_busy = prev_all - previous.idle
    curr_busy = curr_all - current.idle

    all_delta = curr_all - prev_all
    busy_delta = curr_busy - prev_busy
    if all_delta <= 0 or busy_delta <= 0:
        return 0.0
    return round(min(max(busy_delta / all_delta * 100.0, 0.0), 100.0), 1)


class CpuPlugin(MonitorPlugin):
    """CPU metrics, sampled from this plugin's own previous reading.

    ``psutil.cpu_percent(interval=0)`` keys its "time of last call" state by
    **thread id**.  The collector runs plugins on a rotating thread pool, so
    every time this plugin landed on a worker it had not run on before, psutil
    had no baseline for that thread and returned ``0.0`` — and when it did have
    one, the measurement window was "since this thread last ran", which is not
    the refresh interval.  Holding the previous ``cpu_times`` sample on the
    instance makes the reading correct no matter which thread collects it.
    """

    name = "cpu"
    category = "cpu"

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._lock = threading.Lock()
        # Baseline taken at construction so the first collect() has something
        # to diff against instead of reporting a flat zero.
        self._prev_per_cpu = psutil.cpu_times(percpu=True)

    def is_available(self) -> bool:
        return True

    def _percentages(self) -> tuple[float, list[float]]:
        """Return ``(overall, per_core)`` since the previous collect."""
        current = psutil.cpu_times(percpu=True)
        with self._lock:
            previous = self._prev_per_cpu
            self._prev_per_cpu = current

        if not current:
            return 0.0, []
        if previous is None or len(previous) != len(current):
            return 0.0, [0.0] * len(current)

        per_core = [_busy_percent(p, c) for p, c in zip(previous, current, strict=False)]
        overall = round(sum(per_core) / len(per_core), 1) if per_core else 0.0
        return overall, per_core

    @staticmethod
    def _normalise_mhz(value: float | None) -> float | None:
        """Return a frequency in MHz.

        psutil reports GHz rather than MHz on some platforms (notably macOS on
        Apple Silicon, where ``cpu_freq()`` returns ``current=4``), which would
        otherwise be rendered as "4 MHz". No real CPU runs below 10 MHz, so a
        value that small is unambiguously GHz.
        """
        if value is None or value <= 0:
            return None
        return value * 1000 if value < 10 else value

    def collect(self) -> CpuMetrics:
        freq = None
        if hasattr(psutil, "cpu_freq"):
            try:
                freq = psutil.cpu_freq()
            except Exception:
                pass
        # Load average is not available on Windows
        load1: float | None
        load5: float | None
        load15: float | None
        try:
            load1, load5, load15 = os.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = None

        cpu_stats = psutil.cpu_stats()

        percent_overall, percent_per_core = self._percentages()

        return CpuMetrics(
            percent_overall=percent_overall,
            percent_per_core=percent_per_core,
            core_count_logical=psutil.cpu_count(logical=True) or 0,
            core_count_physical=psutil.cpu_count(logical=False),
            frequency_current_mhz=self._normalise_mhz(freq.current) if freq else None,
            frequency_max_mhz=self._normalise_mhz(freq.max) if freq else None,
            load_avg_1=load1,
            load_avg_5=load5,
            load_avg_15=load15,
            ctx_switches=cpu_stats.ctx_switches,
            interrupts=cpu_stats.interrupts,
        )
