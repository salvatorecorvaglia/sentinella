import pytest

from sentinella.config import (
    ExportConfig,
    GeneralConfig,
    ModulesConfig,
    ProcessesConfig,
    RemoteConfig,
    SentinellaConfig,
    WebConfig,
)
from sentinella.core.models import (
    BatteryInfo,
    ContainerMetrics,
    CpuMetrics,
    DiskIO,
    DiskMetrics,
    DiskPartition,
    MemoryMetrics,
    NetworkInterface,
    NetworkMetrics,
    SensorMetrics,
    SystemInfoMetrics,
    SystemSnapshot,
)


class MockMetricCollector:
    """Minimal MetricCollector that replays a fixed snapshot."""

    def __init__(self, snapshot):
        self.snapshot = snapshot

    def collect(self):
        return self.snapshot

    async def collect_async(self):
        return self.snapshot

    def close(self):
        pass

    async def close_async(self):
        pass


@pytest.fixture
def mock_collector(dummy_snapshot):
    return MockMetricCollector(dummy_snapshot)


@pytest.fixture
def make_collector():
    """Build a MockMetricCollector around an arbitrary snapshot."""
    return MockMetricCollector


@pytest.fixture
def make_local_collector():
    """Build real ``Collector`` instances that are always closed afterwards.

    Constructing one directly in a test leaves its thread pool (and, before the
    weak-set change, an atexit handler) alive for the rest of the session.
    """
    from sentinella.core.collector import Collector

    created = []

    def _make(config=None):
        collector = Collector(config)
        created.append(collector)
        return collector

    yield _make

    for collector in created:
        collector.close()


@pytest.fixture
def mock_config():
    return SentinellaConfig(
        general=GeneralConfig(refresh_interval=1.0),
        modules=ModulesConfig(),
        web=WebConfig(enabled=False, host="127.0.0.1", port=8080, api_key=""),
        remote=RemoteConfig(enabled=False, host="127.0.0.1", port=9090, api_key=""),
        export=ExportConfig(format="text"),
        processes=ProcessesConfig(max_display=25, sort_by="cpu"),
    )


@pytest.fixture
def dummy_snapshot():
    return SystemSnapshot(
        timestamp=123456789.0,
        system_info=SystemInfoMetrics(
            hostname="test-host",
            os_name="Linux",
            os_version="5.15.0",
            architecture="x86_64",
            uptime_seconds=3600.0,
        ),
        cpu=CpuMetrics(
            percent_overall=10.0,
            percent_per_core=[8.0, 12.0],
            core_count_physical=2,
            core_count_logical=2,
            frequency_current_mhz=2500.0,
            frequency_max_mhz=3000.0,
            load_avg_1=1.0,
            load_avg_5=1.0,
            load_avg_15=1.0,
        ),
        memory=MemoryMetrics(
            total=8589934592,
            available=4294967296,
            used=4294967296,
            percent=50.0,
            swap_total=2147483648,
            swap_used=1073741824,
            swap_percent=50.0,
        ),
        disk=DiskMetrics(
            partitions=[
                DiskPartition(
                    device="/dev/sda1",
                    mountpoint="/",
                    fstype="ext4",
                    total=1000000,
                    used=500000,
                    free=500000,
                    percent=50.0,
                )
            ],
            io=DiskIO(read_count=100, write_count=200, read_bytes=102400, write_bytes=204800),
        ),
        network=NetworkMetrics(
            interfaces=[
                NetworkInterface(
                    name="eth0",
                    bytes_sent=5000,
                    bytes_recv=10000,
                    packets_sent=50,
                    packets_recv=100,
                    addrs=[],
                )
            ],
            connections_count=5,
        ),
        sensors=SensorMetrics(
            temperatures=[],
            fans=[],
            battery=BatteryInfo(percent=95.0, secs_left=None, power_plugged=True),
        ),
        users=[],
        containers=ContainerMetrics(
            containers=[],
            docker_available=False,
            lxc_available=False,
        ),
        processes=[],
    )
