"""Logged-in users plugin."""

from __future__ import annotations

import psutil

from sentinella.core.models import UserInfo
from sentinella.plugins.base import MonitorPlugin


class UsersPlugin(MonitorPlugin):
    name = "users"
    category = "users"

    def is_available(self) -> bool:
        return True

    def collect(self) -> list[UserInfo]:
        users: list[UserInfo] = []
        seen: set[str] = set()
        try:
            for u in psutil.users():
                name = getattr(u, "name", "") or ""
                terminal = getattr(u, "terminal", None)
                host = getattr(u, "host", "") or ""
                started = getattr(u, "started", 0.0) or 0.0
                key = f"{name}:{terminal}"
                if key in seen:
                    continue
                seen.add(key)
                users.append(
                    UserInfo(
                        name=name,
                        terminal=terminal,
                        host=host,
                        started=started,
                    )
                )
        except Exception:
            pass
        return users
