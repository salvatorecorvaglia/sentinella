import dataclasses

from sentinella.config import ProcessesConfig, SentinellaConfig
from sentinella.core.models import ProcessInfo
from sentinella.export.csv_export import CsvExporter
from sentinella.export.json_export import JsonExporter
from sentinella.export.text_export import TextExporter


def test_json_exporter(dummy_snapshot):
    exporter = JsonExporter()
    output = exporter.format(dummy_snapshot)
    assert '"timestamp"' in output
    assert '"system_info"' in output


def test_csv_exporter(dummy_snapshot):
    exporter = CsvExporter()
    output = exporter.format(dummy_snapshot, modules=["cpu"])
    assert "cpu.percent_overall" in output
    assert "10.0" in output


def _with_processes(snapshot, count):
    procs = [
        ProcessInfo(pid=i, name=f"proc{i}", cpu_percent=float(count - i), memory_percent=1.0)
        for i in range(count)
    ]
    return dataclasses.replace(snapshot, processes=procs, process_count=count)


def test_json_csv_text_report_the_same_process_count(dummy_snapshot):
    """The same snapshot must yield the same process count regardless of
    export format — JSON used to dump up to max(100, max_display*2), and CSV
    hardcoded a cap of 5, while only text respected processes.max_display."""
    cfg = SentinellaConfig(processes=ProcessesConfig(max_display=3, sort_by="cpu"))
    snap = _with_processes(dummy_snapshot, count=10)

    json_out = JsonExporter(cfg).format(snap)
    csv_out = CsvExporter(cfg).format(snap)
    text_out = TextExporter(cfg).format(snap)

    assert json_out.count('"pid"') == 3
    assert csv_out.count("processes[") // len(ProcessInfo.__dataclass_fields__) == 3
    assert text_out.count("proc") - text_out.count("Procs") == 3


def test_csv_exporter_neutralises_formula_injection(dummy_snapshot):
    """A process name starting with '=' must not reach the CSV cell as-is —
    Excel/Sheets would interpret it as a formula on open."""
    snap = dataclasses.replace(
        dummy_snapshot,
        processes=[ProcessInfo(pid=1, name="=cmd|' /C calc'!A1", cpu_percent=1.0)],
        process_count=1,
    )
    output = CsvExporter().format(snap, modules=["processes"])
    assert ",=cmd" not in output  # unprefixed, would be read as a formula
    assert "'=cmd" in output


def test_text_exporter(dummy_snapshot):
    exporter = TextExporter()
    output = exporter.format(dummy_snapshot)
    assert "test-host" in output
    assert "RAM" in output


def test_exporter_filtering(dummy_snapshot):
    # JsonExporter filtering
    json_exp = JsonExporter()
    json_out = json_exp.format(dummy_snapshot, modules=["cpu"])
    assert "cpu" in json_out
    assert "memory" not in json_out

    # CsvExporter filtering
    csv_exp = CsvExporter()
    csv_out = csv_exp.format(dummy_snapshot, modules=["cpu"])
    assert "cpu.percent_overall" in csv_out
    assert "memory.percent" not in csv_out

    # TextExporter filtering
    text_exp = TextExporter()
    text_out = text_exp.format(dummy_snapshot, modules=["cpu"])
    assert "CPU" in text_out
    assert "RAM" not in text_out
