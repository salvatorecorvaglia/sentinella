"""What the dashboards show, and how they say it."""

from unittest.mock import patch

from loopback_client import TestClient

from sentinella.config import GeneralConfig, SentinellaConfig
from sentinella.core.constants import PROCESS_SORT_ORDER
from sentinella.core.models import DiskIO, DiskMetrics, SystemSnapshot
from sentinella.core.sort import PROCESS_SORT_KEYS
from sentinella.export.text_export import TextExporter
from sentinella.tui.widgets.disk_widget import DiskWidget
from sentinella.web.server import create_app


def _disk_snap(ts, read, write):
    return SystemSnapshot(
        timestamp=ts, disk=DiskMetrics(io=DiskIO(read_bytes=read, write_bytes=write))
    )


def _render(widget, snapshots):
    captured = {}
    with patch.object(type(widget), "update", lambda self, t: captured.__setitem__("t", t)):
        for snap in snapshots:
            widget.update_data(snap)
    return captured["t"].plain


# ── Disk I/O as a rate ──────────────────────────────────────────────────────


def test_tui_disk_io_is_a_rate_not_a_lifetime_total():
    """The panel showed bytes read since boot next to live bars, which reads
    as current activity: 863 GB "Read" on an idle machine."""
    out = _render(DiskWidget(), [_disk_snap(100.0, 0, 0), _disk_snap(102.0, 2048, 4096)])
    assert "Read: 1.0 KB/s" in out
    assert "Write: 2.0 KB/s" in out


def test_tui_disk_io_waits_for_a_second_sample():
    out = _render(DiskWidget(), [_disk_snap(100.0, 10**12, 10**12)])
    assert "Read: —" in out
    assert "GB" not in out  # the lifetime total is not passed off as a rate


def test_tui_disk_rate_survives_a_repeated_snapshot():
    second = _disk_snap(102.0, 2048, 0)
    out = _render(DiskWidget(), [_disk_snap(100.0, 0, 0), second, second])
    assert "Read: 1.0 KB/s" in out


def test_tui_disk_rate_treats_a_counter_reset_as_zero():
    out = _render(DiskWidget(), [_disk_snap(100.0, 5000, 5000), _disk_snap(101.0, 10, 10)])
    assert "Read: 0.0 B/s" in out


def test_print_labels_disk_totals_as_since_boot():
    out = TextExporter().format(_disk_snap(1.0, 2048, 4096), ["disk"])
    assert "since boot" in out


# ── What the web dashboard needs from /health ───────────────────────────────


def test_health_reports_refresh_interval_and_rankable_sort_keys():
    cfg = SentinellaConfig(general=GeneralConfig(refresh_interval=3.0))
    with TestClient(create_app(cfg)) as client:
        health = client.get("/health").json()
    assert health["refresh_interval"] == 3.0
    # Full definitions, so the dashboard needs no copy of PROCESS_SORT_KEYS.
    assert health["sort_keys"] == {
        key: {"field": field, "descending": descending}
        for key, (field, descending) in PROCESS_SORT_KEYS.items()
    }
    assert list(health["sort_keys"]) == list(PROCESS_SORT_ORDER)
