"""Memory monitoring plugin."""

from __future__ import annotations

import psutil

from sentinella.core.models import MemoryMetrics
from sentinella.plugins.base import MonitorPlugin


class MemoryPlugin(MonitorPlugin):
    name = "memory"
    category = "memory"

    def is_available(self) -> bool:
        return True

    def collect(self) -> MemoryMetrics:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return MemoryMetrics(
            total=vm.total,
            available=vm.available,
            used=vm.used,
            percent=vm.percent,
            swap_total=sw.total,
            swap_used=sw.used,
            swap_free=sw.free,
            swap_percent=sw.percent,
        )
