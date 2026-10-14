"""Package boundaries and shared definitions."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

WEB_STACK = ("fastapi", "starlette", "uvicorn")


def _loaded_after_import(modules):
    code = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in modules)
        + f"print(*[m for m in {WEB_STACK!r} if m in sys.modules])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO)},
        check=True,
    )
    return set(out.stdout.split())


def test_core_does_not_depend_on_the_web_stack():
    """core/ is the domain — collection, models, shared names. Serving it over
    HTTP lives in sentinella.server, so the one-shot commands and the TUI
    don't pay for FastAPI and uvicorn."""
    core = [
        f"sentinella.core.{p.stem}"
        for p in (REPO / "sentinella" / "core").glob("*.py")
        if p.stem not in ("__init__", "api", "broadcast", "runner")  # the shims
    ]
    assert "sentinella.core.collector" in core
    assert _loaded_after_import(core) == set()


@pytest.mark.parametrize("name", ["api", "broadcast", "runner"])
def test_old_core_import_paths_are_the_moved_modules(name):
    """Kept for one release so outside imports (and patch targets) still work."""
    import importlib

    old = importlib.import_module(f"sentinella.core.{name}")
    new = importlib.import_module(f"sentinella.server.{name}")
    assert old is new


def test_tui_sort_cycle_is_every_valid_sort_key():
    from sentinella.core.constants import PROCESS_SORT_ORDER
    from sentinella.core.sort import PROCESS_SORT_KEYS
    from sentinella.tui.app import _SORT_CYCLE

    assert tuple(_SORT_CYCLE) == PROCESS_SORT_ORDER == tuple(PROCESS_SORT_KEYS)


def test_module_names_match_the_snapshot():
    from sentinella.core.constants import MODULE_NAMES

    assert MODULE_NAMES == {
        "cpu",
        "memory",
        "disk",
        "network",
        "processes",
        "users",
        "sensors",
        "containers",
        "system_info",
    }


# ── Payload contract version ────────────────────────────────────────────────


def test_health_reports_the_api_version():
    from loopback_client import TestClient

    from sentinella.config import SentinellaConfig
    from sentinella.core.constants import API_VERSION
    from sentinella.web.server import create_app

    with TestClient(create_app(SentinellaConfig())) as client:
        assert client.get("/health").json()["api_version"] == API_VERSION


def _remote_with_health(body):
    from unittest.mock import MagicMock

    from sentinella.remote.client import RemoteCollector

    remote = RemoteCollector("127.0.0.1:9")
    response = MagicMock()
    response.json.return_value = body
    remote._client = MagicMock()
    remote._client.get.return_value = response
    return remote


def test_remote_client_warns_about_a_mismatched_agent(caplog):
    remote = _remote_with_health({"active_modules": ["cpu"], "api_version": 99})
    assert remote.active_modules == {"cpu"}  # still usable, just flagged
    assert "API v99" in caplog.text


def test_remote_client_is_quiet_with_a_matching_or_older_unversioned_agent(caplog):
    from sentinella.core.constants import API_VERSION

    for body in ({"active_modules": ["cpu"], "api_version": API_VERSION}, {"active_modules": []}):
        assert isinstance(_remote_with_health(body).active_modules, frozenset)
    assert "API v" not in caplog.text


def test_every_dashboard_card_names_a_real_module():
    """The web cards declare their module in markup (data-module), which
    app.js reads to grey out unmonitored ones; they must name real modules,
    and every module with a card of its own must have one."""
    import re

    from sentinella.core.constants import MODULE_NAMES

    html = (REPO / "sentinella" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    declared = re.findall(r'data-module="([^"]+)"', html)
    assert len(declared) == len(set(declared)), "a module is claimed by two cards"
    # system_info is the page header, not a card.
    assert set(declared) == MODULE_NAMES - {"system_info"}
