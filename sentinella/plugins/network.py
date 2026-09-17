"""Network monitoring plugin."""

from __future__ import annotations

import socket
import threading

import psutil

from sentinella.core.models import NetworkInterface, NetworkMetrics
from sentinella.plugins.base import MonitorPlugin


class NetworkPlugin(MonitorPlugin):
    name = "network"
    category = "network"

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._ticks = 0
        self._conn_count = 0
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return True

    def collect(self) -> NetworkMetrics:
        counters = psutil.net_io_counters(pernic=True)
        addrs = psutil.net_if_addrs()

        interfaces: list[NetworkInterface] = []
        for iface_name, stats in counters.items():
            # Collect addresses for this interface
            iface_addrs: list[str] = []
            if iface_name in addrs:
                for addr in addrs[iface_name]:
                    if addr.family in (socket.AF_INET, socket.AF_INET6):
                        iface_addrs.append(addr.address)

            interfaces.append(
                NetworkInterface(
                    name=iface_name,
                    bytes_sent=stats.bytes_sent,
                    bytes_recv=stats.bytes_recv,
                    packets_sent=stats.packets_sent,
                    packets_recv=stats.packets_recv,
                    addrs=iface_addrs,
                )
            )

        with self._lock:
            # Connection count — throttle to once every 10 collect cycles (ticks)
            self._ticks += 1
            if self._ticks % 10 == 1:
                try:
                    self._conn_count = len(psutil.net_connections(kind="inet"))
                except Exception:
                    self._conn_count = 0
            conn_count = self._conn_count

        return NetworkMetrics(
            interfaces=interfaces,
            connections_count=conn_count,
        )
