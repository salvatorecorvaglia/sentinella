"""Disk monitoring plugin."""

from __future__ import annotations

import psutil

from sentinella.core.models import DiskIO, DiskMetrics, DiskPartition
from sentinella.plugins.base import MonitorPlugin


class DiskPlugin(MonitorPlugin):
    name = "disk"
    category = "disk"

    def is_available(self) -> bool:
        return True

    def collect(self) -> DiskMetrics:
        partitions: list[DiskPartition] = []
        seen_devices: set[str] = set()

        # Blocklist of slow network filesystems and system pseudo-filesystems
        SKIPPED_FSTYPES = {
            "nfs",
            "nfs4",
            "cifs",
            "smbfs",
            "vboxsf",
            "sshfs",
            "afpfs",
            "davfs",
            "gfs",
            "gfs2",
            "gpfs",
            "lustre",
            "tmpfs",
            "devtmpfs",
            "sysfs",
            "proc",
            "devpts",
            "configfs",
            "debugfs",
            "securityfs",
            "fusectl",
            "pstore",
            "bpf",
            "cgroup",
            "cgroup2",
            "autofs",
            "binfmt_misc",
            "mqueue",
            "hugetlbfs",
        }

        for part in psutil.disk_partitions(all=False):
            # Skip duplicates and pseudo/network filesystems
            if part.device in seen_devices:
                continue
            if (part.fstype or "").lower() in SKIPPED_FSTYPES:
                continue
            seen_devices.add(part.device)

            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (PermissionError, OSError):
                continue

            partitions.append(
                DiskPartition(
                    device=part.device,
                    mountpoint=part.mountpoint,
                    fstype=part.fstype,
                    total=usage.total,
                    used=usage.used,
                    free=usage.free,
                    percent=usage.percent,
                )
            )

        # I/O counters
        try:
            io = psutil.disk_io_counters()
            disk_io = DiskIO(
                read_bytes=io.read_bytes if io else 0,
                write_bytes=io.write_bytes if io else 0,
                read_count=io.read_count if io else 0,
                write_count=io.write_count if io else 0,
            )
        except Exception:
            disk_io = DiskIO()

        return DiskMetrics(partitions=partitions, io=disk_io)
