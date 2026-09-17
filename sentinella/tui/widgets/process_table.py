"""Process table widget — sortable DataTable of running processes."""

from __future__ import annotations

from textual.widgets import DataTable

from sentinella.core.models import SystemSnapshot
from sentinella.core.sort import PROCESS_SORT_KEYS, sort_processes
from sentinella.core.utils import human_bytes_compact


class ProcessTable(DataTable):
    """Process list as a sortable data table."""

    def on_mount(self) -> None:
        self.add_columns("PID", "Name", "User", "CPU%", "MEM%", "RSS", "Threads", "Status")
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_data(
        self, snap: SystemSnapshot, max_display: int = 25, sort_by: str = "cpu"
    ) -> None:
        from rich.text import Text
        from textual.coordinate import Coordinate

        # Update headers with sort indicators
        base_headers = ["PID", "Name", "User", "CPU%", "MEM%", "RSS", "Threads", "Status"]
        sort_columns = {"pid": 0, "name": 1, "cpu": 3, "memory": 4}
        cols = list(self.columns.values())
        for idx, base in enumerate(base_headers):
            if idx < len(cols):
                cols[idx].label = Text(base)
        if sort_by in sort_columns:
            col_idx = sort_columns[sort_by]
            _, descending = PROCESS_SORT_KEYS.get(sort_by, PROCESS_SORT_KEYS["cpu"])
            if col_idx < len(cols):
                arrow = "▼" if descending else "▲"
                cols[col_idx].label = Text(f"{base_headers[col_idx]} {arrow}")
        self.refresh()

        procs = sort_processes(snap.processes, sort_by)[:max_display]

        current_row_count = self.row_count
        target_row_count = len(procs)

        if current_row_count > target_row_count:
            # Remove extra rows from the end
            row_keys = list(self.rows.keys())
            for r_key in row_keys[target_row_count:]:
                self.remove_row(r_key)
        elif current_row_count < target_row_count:
            # Add missing rows
            for _ in range(target_row_count - current_row_count):
                self.add_row("", "", "", "", "", "", "", "")

        for row_idx, p in enumerate(procs):
            cpu_str = f"{p.cpu_percent:.1f}"
            mem_str = f"{p.memory_percent:.1f}"
            values = [
                str(p.pid),
                p.name[:25],
                p.username[:12] if p.username else "—",
                cpu_str,
                mem_str,
                human_bytes_compact(p.memory_rss),
                str(p.num_threads),
                p.status[:8],
            ]
            for col_idx, val in enumerate(values):
                self.update_cell_at(Coordinate(row_idx, col_idx), val)
