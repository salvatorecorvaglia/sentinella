import pytest

from sentinella.core.models import SystemSnapshot


def test_collector_initialization(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)
    assert len(collector.plugins) > 0
    assert collector.config == mock_config


def test_collector_collect(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)
    # Perform a real collection run on the local machine
    snapshot = collector.collect()
    assert isinstance(snapshot, SystemSnapshot)
    assert snapshot.timestamp > 0
    assert snapshot.system_info is not None


@pytest.mark.asyncio
async def test_collector_collect_async(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)
    snapshot = await collector.collect_async()
    assert isinstance(snapshot, SystemSnapshot)
    assert snapshot.timestamp > 0


def test_collector_ttl_caching(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)

    snap1 = collector.collect()
    snap2 = collector.collect()
    assert snap1 is snap2

    collector._last_collected_at = 0.0
    snap3 = collector.collect()
    assert snap1 is not snap3


def test_collector_collect_module(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)
    cpu_metrics = collector.collect_module("cpu")
    assert cpu_metrics is not None
    assert hasattr(cpu_metrics, "percent_overall")


def test_collector_failure_tolerance(mock_config, make_local_collector):
    from sentinella.core.models import CpuMetrics

    class FailingPlugin:
        name = "cpu"
        category = "cpu"
        enabled = True

        def collect(self):
            raise RuntimeError("Dummy collect failure")

    collector = make_local_collector(mock_config)
    collector.plugins = [FailingPlugin()]

    snapshot = collector.collect()
    assert snapshot is not None
    assert snapshot.cpu == CpuMetrics()
