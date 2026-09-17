"""Tests for sentinella.core.runner.start_background_servers.

Previously had zero test references anywhere — nothing verified that web and
remote servers actually start when enabled, that both can run simultaneously,
or that neither starts when disabled.
"""

from unittest.mock import patch

from sentinella.config import RemoteConfig, SentinellaConfig, WebConfig
from sentinella.core.runner import start_background_servers


class _ImmediateThread:
    """Runs the thread target synchronously instead of spawning a real thread.

    Deterministic stand-in for ``threading.Thread`` so these tests don't need
    to poll/sleep waiting for a background thread to call the (mocked)
    ``uvicorn.run``.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


def test_start_background_servers_starts_web_when_enabled(make_collector, dummy_snapshot):
    cfg = SentinellaConfig(web=WebConfig(enabled=True, host="127.0.0.1", port=8123))
    with (
        patch("sentinella.core.runner.uvicorn.run") as mock_run,
        patch("sentinella.core.runner.threading.Thread", _ImmediateThread),
    ):
        start_background_servers(cfg, collector=make_collector(dummy_snapshot))

    assert mock_run.called
    assert mock_run.call_args.kwargs["host"] == "127.0.0.1"
    assert mock_run.call_args.kwargs["port"] == 8123


def test_start_background_servers_starts_remote_when_enabled(make_collector, dummy_snapshot):
    cfg = SentinellaConfig(remote=RemoteConfig(enabled=True, host="127.0.0.1", port=9191))
    with (
        patch("sentinella.core.runner.uvicorn.run") as mock_run,
        patch("sentinella.core.runner.threading.Thread", _ImmediateThread),
    ):
        start_background_servers(cfg, collector=make_collector(dummy_snapshot))

    assert mock_run.called
    assert mock_run.call_args.kwargs["port"] == 9191


def test_start_background_servers_starts_both_independently(make_collector, dummy_snapshot):
    cfg = SentinellaConfig(
        web=WebConfig(enabled=True, port=8281),
        remote=RemoteConfig(enabled=True, port=9282),
    )
    with (
        patch("sentinella.core.runner.uvicorn.run") as mock_run,
        patch("sentinella.core.runner.threading.Thread", _ImmediateThread),
    ):
        start_background_servers(cfg, collector=make_collector(dummy_snapshot))

    assert mock_run.call_count == 2
    ports = {call.kwargs["port"] for call in mock_run.call_args_list}
    assert ports == {8281, 9282}


def test_start_background_servers_noop_when_both_disabled(make_collector, dummy_snapshot):
    cfg = SentinellaConfig()  # web/remote disabled by default
    with (
        patch("sentinella.core.runner.uvicorn.run") as mock_run,
        patch("sentinella.core.runner.threading.Thread") as mock_thread,
    ):
        start_background_servers(cfg, collector=make_collector(dummy_snapshot))

    mock_run.assert_not_called()
    mock_thread.assert_not_called()
