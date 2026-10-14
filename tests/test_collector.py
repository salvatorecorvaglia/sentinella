import threading
import weakref
from unittest.mock import patch

import pytest

from sentinella.config import (
    ModulesConfig,
    SentinellaConfig,
)
from sentinella.core.collector import Collector
from sentinella.core.models import SystemSnapshot
from sentinella.remote.client import RemoteCollector


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


def test_collector_active_modules_excludes_disabled(mock_config):
    cfg = SentinellaConfig(modules=ModulesConfig(cpu=False, memory=True))
    collector = Collector(cfg)
    try:
        assert "cpu" not in collector.active_modules
        assert "memory" in collector.active_modules
        assert collector.collect_module("cpu") is None
    finally:
        collector.close()


# ── P2-6: a hung plugin must not be resubmitted every cycle ──────────────────


def test_collector_does_not_resubmit_a_still_running_plugin(mock_config):
    """Resubmitting a plugin whose previous call hasn't returned is what
    exhausts the thread pool when a plugin hangs — repeated cycles would each
    permanently claim another worker on top of the ones already stuck. The
    same in-flight future must be reused across cycles instead."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    class SlowPlugin:
        name = "cpu"
        category = "cpu"

        def collect(self):
            calls.append(1)
            started.set()
            release.wait(timeout=5)
            from sentinella.core.models import CpuMetrics

            return CpuMetrics()

        def close(self):
            pass

    collector = Collector(mock_config)
    collector.plugins = [SlowPlugin()]
    try:
        with patch("sentinella.core.collector.as_completed", side_effect=TimeoutError):
            collector.collect()
        assert started.wait(timeout=2), "plugin.collect() was never invoked"
        assert "cpu" in collector._inflight
        assert not collector._inflight["cpu"].done()

        collector._last_collected_at = 0.0  # force past the TTL cache
        with patch("sentinella.core.collector.as_completed", side_effect=TimeoutError):
            collector.collect()

        assert len(calls) == 1, "hung plugin was resubmitted instead of reused"
    finally:
        release.set()
        collector.close()


# ── P2-5: TUI sort cycling must re-truncate, not just re-sort ────────────────


def test_collector_collect_processes_delegates_to_processes_plugin(mock_config):
    collector = Collector(mock_config)
    try:
        result = collector.collect_processes("pid")
        assert isinstance(result, list)
    finally:
        collector.close()


@pytest.mark.asyncio
async def test_collector_collect_processes_async(mock_config):
    collector = Collector(mock_config)
    try:
        result = await collector.collect_processes_async("memory")
        assert isinstance(result, list)
    finally:
        collector.close()


def test_collector_collect_processes_without_processes_plugin_returns_empty(mock_config):
    collector = Collector(mock_config)
    collector.plugins = []
    try:
        assert collector.collect_processes("cpu") == []
    finally:
        collector.close()


# ── Phase 1 (C1): the collector must sample every cycle, not every other ─────


def test_collector_samples_every_cycle(mock_config):
    """A completed future must not be re-harvested on the next cycle.

    ``_inflight`` was only cleared on the cycle *after* a plugin finished, so
    cycle N+1 read cycle N's result instead of submitting a fresh call: every
    surface refreshed at half its configured rate and showed each reading
    twice.  Line coverage never caught it — the bug lives on covered lines and
    ``_assemble`` mints a new snapshot object either way.
    """
    from sentinella.core.models import CpuMetrics

    class CountingPlugin:
        name = "cpu"
        category = "cpu"

        def __init__(self) -> None:
            self.calls = 0

        def collect(self):
            self.calls += 1
            return CpuMetrics(percent_overall=float(self.calls))

        def close(self):
            pass

    plugin = CountingPlugin()
    collector = Collector(mock_config)
    collector.plugins = [plugin]
    try:
        seen = []
        for _ in range(4):
            collector._last_collected_at = 0.0  # force past the TTL cache
            seen.append(collector.collect().cpu.percent_overall)

        assert seen == [1.0, 2.0, 3.0, 4.0], f"stale readings replayed: {seen}"
        assert plugin.calls == 4
        # A finished future must not be pinned between cycles.
        assert not collector._inflight
    finally:
        collector.close()


# ── Phase 3 (R2): one atexit handler, not one per collector ────────────────


def test_collectors_do_not_accumulate_atexit_handlers(mock_config):
    """Each Collector used to register its own bound atexit handler, so a
    process that built collectors over time pinned every thread pool it had
    ever made."""
    from sentinella.core import collector as collector_mod

    before = len(collector_mod._LIVE_COLLECTORS)
    made = [Collector(mock_config) for _ in range(3)]
    assert len(collector_mod._LIVE_COLLECTORS) == before + 3

    for c in made:
        c.close()
    assert len(collector_mod._LIVE_COLLECTORS) == before


def test_a_dropped_collector_is_garbage_collectable(mock_config):
    import gc

    from sentinella.core import collector as collector_mod

    collector = Collector(mock_config)
    ref = weakref.ref(collector)
    collector.close()
    del collector
    gc.collect()
    assert ref() is None, "a closed collector is still pinned by the exit hook"
    assert len(collector_mod._LIVE_COLLECTORS) >= 0


# ── Phase 3 (R4): the process total must describe the list it came with ────


def test_process_total_travels_with_the_list(mock_config, make_local_collector):
    """total_count was written from a worker thread and read from the collector
    thread, so it could describe a different cycle than the list beside it."""
    from sentinella.plugins.processes import ProcessesPlugin

    listing = ProcessesPlugin(mock_config).collect()
    assert listing.total >= len(listing)
    assert isinstance(listing, list)  # every existing consumer still works

    collector = make_local_collector(mock_config)
    snap = collector.collect()
    assert snap.process_count >= len(snap.processes)


def test_process_count_falls_back_for_a_plain_list():
    """A plugin returning a bare list (a stub, an old third-party one) must
    still produce a sane count rather than raising."""
    snap = Collector._assemble(
        {"processes": [1, 2, 3]}, Collector._process_count({"processes": [1, 2, 3]})
    )
    assert snap.process_count == 3


def test_collector_can_turn_cmdline_on_and_invalidates_its_cache(mock_config, make_local_collector):
    collector = make_local_collector(mock_config)
    first = collector.collect()
    assert all(not p.cmdline for p in first.processes)

    collector.set_collect_cmdline(True)
    # The cached snapshot must be dropped, or the change wouldn't take effect
    # until the TTL happened to expire.
    assert collector._last_snapshot is None
    assert any(p.cmdline for p in collector.collect().processes)


# ── Phase 5 (A1): the protocol must describe what the API actually needs ────


def test_both_collectors_satisfy_the_protocol(mock_config, make_local_collector):
    """MetricCollector declared four methods while core.api needed six members
    plus a private attribute, so a RemoteCollector satisfied the protocol and
    still blew up on /api/v1/{module}."""
    from sentinella.core.protocols import MetricCollector

    local = make_local_collector(mock_config)
    remote = RemoteCollector("127.0.0.1:9090")
    try:
        for collector in (local, remote):
            assert isinstance(collector, MetricCollector)
            for member in (
                "collect",
                "collect_async",
                "collect_module_async",
                "close",
                "close_async",
                "active_modules",
                "last_collected_at",
                "set_collect_cmdline",
                "collect_processes_async",
            ):
                assert hasattr(collector, member), f"{collector!r} lacks {member}"
    finally:
        remote.close()
