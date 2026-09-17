"""CSV export formatter."""

from __future__ import annotations

import csv
import io

from sentinella.core.limits import EXPORT_LIMITS
from sentinella.core.models import SystemSnapshot
from sentinella.core.utils import serialize_model as asdict
from sentinella.export.base import BaseExporter

# Leading characters that spreadsheet apps (Excel, Sheets, LibreOffice) treat
# as the start of a formula when a CSV cell is opened — see OWASP's CSV
# injection guidance. Process names, cmdlines, and container labels are
# attacker-influenceable, so any cell starting with one of these is neutralised
# by prefixing it with a plain quote before it reaches csv.writer.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_csv_field(value: str) -> str:
    """Neutralise a value that would be interpreted as a formula by Excel/Sheets."""
    if value.startswith(_FORMULA_TRIGGER_CHARS):
        return "'" + value
    return value


# Flattened keys are dotted paths ("disk.partitions"), so the limit is keyed
# off the last segment — the plural noun EXPORT_LIMITS is indexed by.
_DEFAULT_NESTED_CAP = 5


def _nested_cap(full_key: str) -> int:
    """Row cap for a nested list, from EXPORT_LIMITS where one is defined."""
    return EXPORT_LIMITS.get(full_key.rsplit(".", 1)[-1], _DEFAULT_NESTED_CAP)


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """Flatten a nested dict into dot-separated keys."""
    items: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.update(_flatten(value, full_key))
        elif isinstance(value, list):
            if len(value) == 0:
                items[full_key] = ""
            elif isinstance(value[0], dict):
                # "processes" is already truncated to processes.max_display by
                # the exporter before this runs, so it isn't re-capped here —
                # only other, still-uncapped nested lists get a cap.  That cap
                # comes from EXPORT_LIMITS, the same table the text exporter
                # uses, so one host doesn't export 8 partitions as text and 5
                # as CSV.
                cap = len(value) if full_key == "processes" else _nested_cap(full_key)
                for i, item in enumerate(value[:cap]):
                    items.update(_flatten(item, f"{full_key}[{i}]"))
            else:
                items[full_key] = "; ".join(str(v) for v in value)
        else:
            items[full_key] = str(value) if value is not None else ""
    return {k: _sanitize_csv_field(v) for k, v in items.items()}


class CsvExporter(BaseExporter):
    name = "csv"

    def format(self, snapshot: SystemSnapshot, modules: list[str] | None = None) -> str:
        data = asdict(snapshot)
        # Match the process count text/JSON export show, instead of the
        # unrelated 5-item safety cap _flatten applies to other nested lists.
        data["processes"] = [asdict(p) for p in self.sorted_processes(snapshot)]
        if modules:
            filtered = {"timestamp": data["timestamp"]}
            for mod in modules:
                if mod in data:
                    filtered[mod] = data[mod]
            data = filtered

        flat = _flatten(data)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(flat.keys())
        writer.writerow(flat.values())
        return buf.getvalue()
