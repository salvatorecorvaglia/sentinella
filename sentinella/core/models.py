"""Data models for all Sentinella metric categories.

Every model is a frozen dataclass that serialises cleanly to JSON via
``dataclasses.asdict``.  The top-level ``SystemSnapshot`` aggregates all
categories with a UTC timestamp.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# ── CPU ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CpuMetrics:
    """Aggregate CPU statistics."""

    percent_overall: float = 0.0
    percent_per_core: list[float] = field(default_factory=list)
    core_count_logical: int = 0
    core_count_physical: int | None = None
    frequency_current_mhz: float | None = None
    frequency_max_mhz: float | None = None
    load_avg_1: float | None = None
    load_avg_5: float | None = None
    load_avg_15: float | None = None
    ctx_switches: int = 0
    interrupts: int = 0


# ── Memory ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MemoryMetrics:
    """Physical + swap memory."""

    total: int = 0
    available: int = 0
    used: int = 0
    percent: float = 0.0
    swap_total: int = 0
    swap_used: int = 0
    swap_free: int = 0
    swap_percent: float = 0.0


# ── Disk ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DiskPartition:
    device: str = ""
    mountpoint: str = ""
    fstype: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    percent: float = 0.0


@dataclass(frozen=True, slots=True)
class DiskIO:
    read_bytes: int = 0
    write_bytes: int = 0
    read_count: int = 0
    write_count: int = 0


@dataclass(frozen=True, slots=True)
class DiskMetrics:
    partitions: list[DiskPartition] = field(default_factory=list)
    io: DiskIO = field(default_factory=DiskIO)


# ── Network ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NetworkInterface:
    name: str = ""
    bytes_sent: int = 0
    bytes_recv: int = 0
    packets_sent: int = 0
    packets_recv: int = 0
    addrs: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NetworkMetrics:
    interfaces: list[NetworkInterface] = field(default_factory=list)
    connections_count: int = 0


# ── Processes ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int = 0
    name: str = ""
    username: str = ""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    status: str = ""
    cmdline: str = ""
    num_threads: int = 0
    memory_rss: int = 0


# ── Users ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UserInfo:
    name: str = ""
    terminal: str | None = None
    host: str = ""
    started: float = 0.0


# ── Sensors ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TemperatureReading:
    label: str = ""
    current: float = 0.0
    high: float | None = None
    critical: float | None = None


@dataclass(frozen=True, slots=True)
class FanReading:
    label: str = ""
    current: int = 0


@dataclass(frozen=True, slots=True)
class BatteryInfo:
    percent: float | None = None
    power_plugged: bool | None = None
    secs_left: int | None = None


@dataclass(frozen=True, slots=True)
class SensorMetrics:
    temperatures: list[TemperatureReading] = field(default_factory=list)
    fans: list[FanReading] = field(default_factory=list)
    battery: BatteryInfo | None = None


# ── Containers ───────────────────────────────────────────────────────────────


# Docker reports "running"; LXC reports "Running", lower-cased on ingest to
# "running"; some runtimes say "up". Four surfaces each spelled this test out
# inline, so a fifth status would have had to be added in four places.
RUNNING_STATUSES = frozenset({"running", "up"})


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    name: str = ""
    container_id: str = ""
    image: str = ""
    status: str = ""
    runtime: str = ""  # "docker" or "lxc"
    cpu_percent: float | None = None
    memory_usage: int | None = None
    memory_limit: int | None = None

    @property
    def is_running(self) -> bool:
        """Whether this container is currently up."""
        return self.status.lower() in RUNNING_STATUSES


@dataclass(frozen=True, slots=True)
class ContainerMetrics:
    containers: list[ContainerInfo] = field(default_factory=list)
    docker_available: bool = False
    lxc_available: bool = False


# ── System Info ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SystemInfoMetrics:
    hostname: str = ""
    os_name: str = ""
    os_version: str = ""
    os_release: str = ""
    kernel: str = ""
    architecture: str = ""
    uptime_seconds: float = 0.0
    boot_time: float = 0.0
    python_version: str = ""
    username: str = ""


# ── Aggregate Snapshot ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    """Complete point-in-time system state."""

    timestamp: float = field(default_factory=time.time)
    cpu: CpuMetrics = field(default_factory=CpuMetrics)
    memory: MemoryMetrics = field(default_factory=MemoryMetrics)
    disk: DiskMetrics = field(default_factory=DiskMetrics)
    network: NetworkMetrics = field(default_factory=NetworkMetrics)
    processes: list[ProcessInfo] = field(default_factory=list)
    # Total processes seen on the host.  ``processes`` is truncated for
    # display, so its length must not be reported as the process count.
    process_count: int = 0
    users: list[UserInfo] = field(default_factory=list)
    sensors: SensorMetrics = field(default_factory=SensorMetrics)
    containers: ContainerMetrics = field(default_factory=ContainerMetrics)
    system_info: SystemInfoMetrics = field(default_factory=SystemInfoMetrics)
