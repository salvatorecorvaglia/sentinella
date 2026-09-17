from collections import namedtuple
from unittest.mock import MagicMock, patch

from sentinella.plugins.cpu import CpuPlugin
from sentinella.plugins.disk import DiskPlugin
from sentinella.plugins.memory import MemoryPlugin
from sentinella.plugins.network import NetworkPlugin
from sentinella.plugins.processes import ProcessesPlugin
from sentinella.plugins.sensors import SensorsPlugin
from sentinella.plugins.system_info import SystemInfoPlugin
from sentinella.plugins.users import UsersPlugin

# The plugin diffs two cpu_times samples of its own rather than calling
# psutil.cpu_percent, whose "time of last call" state is keyed by thread id —
# see CpuPlugin's docstring. These fakes stand in for consecutive samples.
CpuTimes = namedtuple("CpuTimes", "user nice system idle")

# all=100, busy=15  ->  all=200, busy=30: 15 busy ticks out of 100 = 15%.
_BASELINE = CpuTimes(user=10.0, nice=0.0, system=5.0, idle=85.0)
_AFTER = CpuTimes(user=20.0, nice=0.0, system=10.0, idle=170.0)


@patch("psutil.cpu_times")
@patch("psutil.cpu_stats")
@patch("psutil.cpu_count")
@patch("psutil.cpu_freq", create=True)
@patch("psutil.getloadavg", create=True)
def test_cpu_plugin(mock_loadavg, mock_freq, mock_count, mock_stats, mock_times):
    mock_count.return_value = 4
    mock_freq.return_value = MagicMock(current=2500.0, max=3000.0)
    mock_loadavg.return_value = (1.0, 1.0, 1.0)
    mock_stats.return_value = MagicMock(ctx_switches=1, interrupts=2)

    # First call is the constructor's baseline, second is the collect().
    mock_times.side_effect = [[_BASELINE] * 4, [_AFTER] * 4]

    plugin = CpuPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert metrics.percent_overall == 15.0
    assert metrics.percent_per_core == [15.0, 15.0, 15.0, 15.0]
    assert metrics.core_count_logical == 4


@patch("psutil.cpu_times")
@patch("psutil.cpu_stats")
@patch("psutil.cpu_count")
@patch("psutil.cpu_freq", create=True)
@patch("psutil.getloadavg", create=True)
def test_cpu_percent_is_independent_of_the_calling_thread(
    mock_loadavg, mock_freq, mock_count, mock_stats, mock_times
):
    """psutil.cpu_percent keys its baseline by thread id, so running the plugin
    on the collector's rotating pool reported 0.0% every time it landed on a
    worker it had not run on before — and a wrong window when it had."""
    from concurrent.futures import ThreadPoolExecutor

    mock_count.return_value = 4
    mock_freq.return_value = MagicMock(current=2500.0, max=3000.0)
    mock_loadavg.return_value = (1.0, 1.0, 1.0)
    mock_stats.return_value = MagicMock(ctx_switches=1, interrupts=2)

    # Baseline, then three evenly-spaced samples of identical shape.
    samples = [[_BASELINE] * 4]
    step = CpuTimes(user=10.0, nice=0.0, system=5.0, idle=85.0)
    running = _BASELINE
    for _ in range(3):
        running = CpuTimes(*(a + b for a, b in zip(running, step, strict=True)))
        samples.append([running] * 4)
    mock_times.side_effect = samples

    plugin = CpuPlugin()
    # A fresh thread each time, as the collector pool effectively provides.
    results = []
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=1) as pool:
            results.append(pool.submit(plugin.collect).result().percent_overall)

    assert results == [15.0, 15.0, 15.0], f"thread rotation changed the reading: {results}"


@patch("psutil.virtual_memory")
@patch("psutil.swap_memory")
def test_memory_plugin(mock_swap, mock_virtual):
    mock_virtual.return_value = MagicMock(total=8000, available=4000, used=4000, percent=50.0)
    mock_swap.return_value = MagicMock(total=2000, used=1000, percent=50.0)

    plugin = MemoryPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert metrics.total == 8000
    assert metrics.percent == 50.0
    assert metrics.swap_total == 2000


@patch("psutil.disk_partitions")
@patch("psutil.disk_usage")
@patch("psutil.disk_io_counters")
def test_disk_plugin(mock_io, mock_usage, mock_partitions):
    mock_partitions.return_value = [
        MagicMock(device="/dev/sda1", mountpoint="/", fstype="ext4", opts="rw")
    ]
    mock_usage.return_value = MagicMock(total=1000, used=500, free=500, percent=50.0)
    mock_io.return_value = MagicMock(
        read_count=10,
        write_count=20,
        read_bytes=100,
        write_bytes=200,
        read_time=1,
        write_time=2,
    )

    plugin = DiskPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert len(metrics.partitions) == 1
    assert metrics.partitions[0].mountpoint == "/"
    assert metrics.io.read_bytes == 100


@patch("psutil.net_io_counters")
@patch("psutil.net_if_addrs")
@patch("psutil.net_connections")
def test_network_plugin(mock_conns, mock_addrs, mock_io):
    mock_io.return_value = {
        "eth0": MagicMock(
            bytes_sent=1000,
            bytes_recv=2000,
            packets_sent=10,
            packets_recv=20,
            errin=0,
            errout=0,
            dropin=0,
            dropout=0,
        )
    }
    mock_addrs.return_value = {}
    mock_conns.return_value = [1, 2, 3]

    plugin = NetworkPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert len(metrics.interfaces) == 1
    assert metrics.interfaces[0].name == "eth0"
    assert metrics.connections_count == 3


@patch("psutil.process_iter")
def test_processes_plugin(mock_process_iter):
    proc1 = MagicMock()
    proc1.info = {
        "pid": 1,
        "name": "proc1",
        "username": "user1",
        "cpu_percent": 10.0,
        "memory_percent": 5.0,
        "status": "running",
        "cmdline": ["proc1", "arg"],
        "num_threads": 2,
        "memory_info": MagicMock(rss=5000),
    }

    mock_process_iter.return_value = [proc1]

    plugin = ProcessesPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert len(metrics) == 1
    assert metrics[0].pid == 1
    assert metrics[0].name == "proc1"
    assert metrics[0].cpu_percent == 10.0


@patch("platform.node")
@patch("platform.system")
@patch("platform.release")
@patch("platform.machine")
@patch("psutil.boot_time")
def test_system_info_plugin(mock_boot, mock_mach, mock_rel, mock_sys, mock_node):
    mock_node.return_value = "host1"
    mock_sys.return_value = "Linux"
    mock_rel.return_value = "5.0"
    mock_mach.return_value = "x86_64"
    mock_boot.return_value = 1000.0

    plugin = SystemInfoPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert metrics.hostname == "host1"
    assert metrics.os_name == "Linux"


@patch("psutil.users")
def test_users_plugin(mock_users):
    user = MagicMock()
    user.name = "user1"
    user.terminal = "pts/1"
    user.host = "localhost"
    user.started = 100.0
    user.pid = 123
    mock_users.return_value = [user]

    plugin = UsersPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert len(metrics) == 1
    assert metrics[0].name == "user1"


@patch("psutil.sensors_temperatures", create=True)
@patch("psutil.sensors_fans", create=True)
@patch("psutil.sensors_battery", create=True)
def test_sensors_plugin(mock_bat, mock_fans, mock_temps):
    mock_temps.return_value = {
        "cpu_thermal": [MagicMock(label="Core 0", current=45.0, high=80.0, critical=90.0)]
    }
    mock_fans.return_value = {"fan1": [MagicMock(label="Fan 1", current=2000)]}
    mock_bat.return_value = MagicMock(percent=95.0, secsleft=-1, power_plugged=True)

    plugin = SensorsPlugin()
    assert plugin.is_available() is True
    metrics = plugin.collect()
    assert len(metrics.temperatures) == 1
    assert metrics.temperatures[0].label == "Core 0"
    assert metrics.battery.percent == 95.0
    assert metrics.battery.secs_left is None


@patch("psutil.process_iter")
def test_processes_plugin_sorting(mock_process_iter):
    proc1 = MagicMock()
    proc1.info = {
        "pid": 1,
        "name": "low_cpu",
        "username": "user",
        "cpu_percent": 5.0,
        "memory_percent": 2.0,
        "status": "running",
        "cmdline": ["run"],
        "num_threads": 1,
        "memory_info": MagicMock(rss=1000),
    }
    proc2 = MagicMock()
    proc2.info = {
        "pid": 2,
        "name": "high_cpu",
        "username": "user",
        "cpu_percent": 50.0,
        "memory_percent": 10.0,
        "status": "running",
        "cmdline": ["run"],
        "num_threads": 4,
        "memory_info": MagicMock(rss=8000),
    }

    mock_process_iter.return_value = [proc1, proc2]

    plugin = ProcessesPlugin()
    metrics = plugin.collect()
    assert len(metrics) == 2
    assert metrics[0].pid == 2  # high CPU sorted first
    assert metrics[1].pid == 1


@patch("psutil.process_iter")
def test_processes_plugin_sort_override_re_truncates(mock_process_iter):
    """A per-call sort_by must change *truncation*, not just the order of an
    already-truncated slice — otherwise cycling sort in the TUI can never
    surface a process that the default sort's truncation already cut."""
    from sentinella.config import ProcessesConfig, SentinellaConfig

    def make_info(pid, cpu, mem):
        info = MagicMock()
        info.info = {
            "pid": pid,
            "name": f"p{pid}",
            "username": "user",
            "cpu_percent": cpu,
            "memory_percent": mem,
            "status": "running",
            "cmdline": ["run"],
            "num_threads": 1,
            "memory_info": MagicMock(rss=1000),
        }
        return info

    # 100 high-CPU/low-memory processes fill the default 100-item floor...
    bulk = [make_info(i, cpu=100.0 - i, mem=0.1) for i in range(100)]
    # ...so a low-CPU/very-high-memory process ranks outside the top 100 by
    # CPU and would be cut before any consumer ever saw it.
    standout = make_info(999, cpu=0.0, mem=99.0)
    mock_process_iter.return_value = [*bulk, standout]

    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=25, sort_by="cpu"))
    plugin = ProcessesPlugin(config=cfg)

    default_sorted = plugin.collect()
    assert not any(p.pid == 999 for p in default_sorted)

    memory_sorted = plugin.collect(sort_by="memory")
    assert any(p.pid == 999 for p in memory_sorted)


def test_processes_plugin_config_cache():
    from sentinella.config import ProcessesConfig, SentinellaConfig

    custom_cfg = SentinellaConfig(processes=ProcessesConfig(max_display=5))
    plugin = ProcessesPlugin(config=custom_cfg)
    assert plugin._config is custom_cfg

    with patch("sentinella.config.load_config") as mock_load:
        mock_load.return_value = custom_cfg
        plugin_auto = ProcessesPlugin()
        assert plugin_auto._config is custom_cfg
        mock_load.assert_called_once()

        # Call collect and make sure load_config is NOT called again
        with patch("psutil.process_iter", return_value=[]):
            plugin_auto.collect()
            mock_load.assert_called_once()
