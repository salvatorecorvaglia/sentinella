"""JSON export formatter."""

from __future__ import annotations

import json

from sentinella.core.models import SystemSnapshot
from sentinella.core.utils import serialize_model as asdict
from sentinella.export.base import BaseExporter


class JsonExporter(BaseExporter):
    name = "json"

    def format(self, snapshot: SystemSnapshot, modules: list[str] | None = None) -> str:
        data = asdict(snapshot)
        # Match the process count text/CSV export show, instead of dumping the
        # full pre-truncation list (up to max(100, max_display * 2)).
        data["processes"] = [asdict(p) for p in self.sorted_processes(snapshot)]
        if modules:
            filtered = {"timestamp": data["timestamp"]}
            for mod in modules:
                if mod in data:
                    filtered[mod] = data[mod]
            data = filtered
        return json.dumps(data, indent=2, default=str)
