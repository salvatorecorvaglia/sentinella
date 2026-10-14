import dataclasses

import pytest

from sentinella.core.models import CpuMetrics, SystemInfoMetrics


def test_models_frozen():
    info = SystemInfoMetrics(
        hostname="test",
        os_name="Linux",
        os_version="5.0",
        architecture="x86_64",
        uptime_seconds=100.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.hostname = "new-host"


def test_cpu_metrics():
    cpu = CpuMetrics(
        percent_overall=50.0,
        percent_per_core=[50.0],
        core_count_logical=1,
        core_count_physical=1,
        frequency_current_mhz=2000.0,
        frequency_max_mhz=3000.0,
        load_avg_1=1.0,
        load_avg_5=1.0,
        load_avg_15=1.0,
    )
    assert cpu.percent_overall == 50.0
    assert len(cpu.percent_per_core) == 1


def test_container_running_status_has_one_definition():
    from sentinella.core.models import ContainerInfo

    assert ContainerInfo(status="running").is_running
    assert ContainerInfo(status="up").is_running
    # LXC reports "Running"; the plugin lower-cases, but the model shouldn't
    # depend on that having happened.
    assert ContainerInfo(status="Running").is_running
    assert not ContainerInfo(status="exited").is_running
    assert not ContainerInfo(status="").is_running
