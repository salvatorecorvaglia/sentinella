"""Sentinella export formatters.

``get_exporter`` is the single lookup point for format name → exporter, so
adding a format means registering it here rather than editing the CLI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinella.export.base import BaseExporter

if TYPE_CHECKING:
    from sentinella.config import SentinellaConfig

__all__ = ["BaseExporter", "EXPORT_FORMATS", "get_exporter"]

# Format names accepted by `sentinella print --format` and `[export] format`.
EXPORT_FORMATS = ("text", "csv", "json")


def get_exporter(fmt: str, config: SentinellaConfig | None = None) -> BaseExporter:
    """Return an exporter for *fmt*, falling back to text for unknown names."""
    if fmt == "json":
        from sentinella.export.json_export import JsonExporter

        return JsonExporter(config)
    if fmt == "csv":
        from sentinella.export.csv_export import CsvExporter

        return CsvExporter(config)

    from sentinella.export.text_export import TextExporter

    return TextExporter(config)
