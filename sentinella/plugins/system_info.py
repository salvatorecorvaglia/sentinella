"""System information plugin (hostname, OS, kernel, uptime, etc.)."""

from __future__ import annotations

import getpass
import platform
import time

import psutil

from sentinella.core.models import SystemInfoMetrics
from sentinella.plugins.base import MonitorPlugin


class SystemInfoPlugin(MonitorPlugin):
    name = "system_info"
    category = "system_info"

    def __init__(self, config=None) -> None:
        super().__init__(config)
        self._boot_time = psutil.boot_time()
        self._hostname = platform.node()
        self._os_name = platform.system()

        # OS version string
        if self._os_name == "Darwin":
            self._os_version = platform.mac_ver()[0] or platform.version()
        else:
            self._os_version = platform.version()

        self._os_release = platform.platform()
        self._kernel = platform.release()
        self._architecture = platform.machine()
        self._python_version = platform.python_version()

        try:
            self._username = getpass.getuser()
        except Exception:
            self._username = ""

    def is_available(self) -> bool:
        return True

    def collect(self) -> SystemInfoMetrics:
        uptime = time.time() - self._boot_time

        return SystemInfoMetrics(
            hostname=self._hostname,
            os_name=self._os_name,
            os_version=self._os_version,
            os_release=self._os_release,
            kernel=self._kernel,
            architecture=self._architecture,
            uptime_seconds=uptime,
            boot_time=self._boot_time,
            python_version=self._python_version,
            username=self._username,
        )
