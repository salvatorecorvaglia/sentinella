import argparse
from unittest.mock import patch

import pytest

from sentinella.cli import main, remote_address_type
from sentinella.config import (
    RemoteConfig,
    SentinellaConfig,
    WebConfig,
)
from sentinella.core.models import SystemSnapshot


def test_remote_address_validation_valid():
    assert remote_address_type("localhost:8080") == "localhost:8080"
    assert remote_address_type("192.168.1.10:9090") == "192.168.1.10:9090"
    assert remote_address_type("http://myagent:80") == "http://myagent:80"
    assert remote_address_type("https://myagent:443") == "https://myagent:443"


def test_remote_address_validation_invalid():
    # Empty hostname or missing host
    with pytest.raises(argparse.ArgumentTypeError, match="Invalid remote address format"):
        remote_address_type(":8080")

    # Invalid port bounds
    with pytest.raises(argparse.ArgumentTypeError, match="Invalid remote address format"):
        remote_address_type("localhost:999999")

    with pytest.raises(argparse.ArgumentTypeError, match="Invalid remote address format"):
        remote_address_type("localhost:0")


def test_cli_subcommands_routing():
    with patch("sentinella.cli._cmd_tui") as mock_tui:
        main([])
        mock_tui.assert_called_once()

    with patch("sentinella.fetch.run_fetch") as mock_fetch:
        main(["fetch"])
        mock_fetch.assert_called_once()

    with patch("sentinella.cli._cmd_print") as mock_print:
        main(["print", "cpu", "-f", "json"])
        mock_print.assert_called_once()

    with patch("sentinella.cli._cmd_web") as mock_web:
        main(["web", "--host", "127.0.0.1", "-p", "8080"])
        mock_web.assert_called_once()

    with patch("sentinella.cli._cmd_serve") as mock_serve:
        main(["serve", "--host", "0.0.0.0", "-p", "9090"])
        mock_serve.assert_called_once()


def test_cli_remote_collector_key_propagation(dummy_snapshot):
    from sentinella.config import SentinellaConfig

    mock_cfg = SentinellaConfig(remote=RemoteConfig(api_key="super-secret"))
    with patch("sentinella.cli.load_config", return_value=mock_cfg):
        with patch("sentinella.remote.client.RemoteCollector") as mock_rc:
            mock_rc.return_value.collect.return_value = dummy_snapshot
            main(["--remote", "localhost:9090", "print"])
            mock_rc.assert_called_once_with("localhost:9090", api_key="super-secret")

        with patch("sentinella.remote.client.RemoteCollector") as mock_rc:
            with patch("sentinella.tui.app.SentinellaApp"):
                main(["--remote", "localhost:9090"])
                mock_rc.assert_called_once_with("localhost:9090", api_key="super-secret")


# ── Log records must not be written over the TUI ────────────────────────────


def _handlers_during_tui_run(argv):
    """Root handlers as SentinellaApp.run sees them.

    pytest installs its own root handlers (so basicConfig is a no-op here);
    put back the one a real run gets — a StreamHandler on the terminal.
    """
    import logging
    import sys

    root = logging.getLogger()
    saved = root.handlers[:]
    terminal = logging.StreamHandler(sys.__stderr__)
    root.handlers = [terminal]
    seen = {}

    def fake_run(self):
        seen["handlers"] = list(logging.getLogger().handlers)

    with (
        patch("sentinella.tui.app.SentinellaApp.run", fake_run),
        patch("sentinella.server.runner.start_background_servers"),
        patch("sentinella.core.collector.Collector"),
    ):
        try:
            main(argv)
            seen["after"] = list(root.handlers)
        finally:
            root.handlers = saved
    assert seen["after"] == [terminal], "handlers not restored after the TUI exits"
    return seen["handlers"]


def _writes_to_real_terminal(handler):
    import logging
    import sys

    return isinstance(handler, logging.StreamHandler) and handler.stream in (
        sys.stderr,
        sys.stdout,
        sys.__stderr__,
        sys.__stdout__,
    )


def test_tui_detaches_terminal_log_handlers_while_running():
    """Textual draws on the terminal's stderr; a StreamHandler bound to it
    printed every warning straight over the dashboard."""
    handlers = _handlers_during_tui_run([])
    assert handlers
    assert not any(_writes_to_real_terminal(h) for h in handlers)


def test_verbose_tui_logs_to_a_file(tmp_path, monkeypatch):
    import logging

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    handlers = _handlers_during_tui_run(["-v"])
    files = [h for h in handlers if isinstance(h, logging.FileHandler)]
    assert [h.baseFilename.startswith(str(tmp_path)) for h in files] == [True]


# ── Bad CLI overrides are user errors, not tracebacks ───────────────────────


@pytest.mark.parametrize("argv", [["web", "--port", "70000"], ["serve", "--port", "0"]])
def test_invalid_port_override_is_a_usage_error(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "port must be between 1 and 65535" in capsys.readouterr().err


def test_unreadable_config_is_a_usage_error(capsys):
    with patch("sentinella.cli.load_config", side_effect=PermissionError("denied: x.toml")):
        with pytest.raises(SystemExit) as exc:
            main(["print"])
    assert exc.value.code == 2
    assert "denied: x.toml" in capsys.readouterr().err


# ── C3: --host must reach the app, not just uvicorn ──────────────────────────


def test_cli_host_override_reaches_open_bind_warning():
    """`sentinella web --host 0.0.0.0` with no API key must warn."""
    from sentinella.cli import main

    cfg = SentinellaConfig(web=WebConfig(host="127.0.0.1", api_key=""))
    with (
        patch("sentinella.cli.load_config", return_value=cfg),
        patch("uvicorn.run"),
        patch("sentinella.server.api.warn_open_bind") as mock_warn,
        patch("sentinella.web.server.warn_open_bind") as mock_warn_web,
    ):
        main(["web", "--host", "0.0.0.0"])

    called = mock_warn_web.call_args or mock_warn.call_args
    assert called is not None, "warn_open_bind was never called"
    assert called.args[0] == "0.0.0.0", "warning saw the config host, not the CLI override"


def test_cli_host_override_is_what_uvicorn_binds():
    from sentinella.cli import main

    cfg = SentinellaConfig(web=WebConfig(host="127.0.0.1", port=8080, api_key=""))
    with (
        patch("sentinella.cli.load_config", return_value=cfg),
        patch("uvicorn.run") as mock_run,
    ):
        main(["web", "--host", "0.0.0.0", "--port", "9999"])
    assert mock_run.call_args.kwargs["host"] == "0.0.0.0"
    assert mock_run.call_args.kwargs["port"] == 9999


# ── M7: global flags work on either side of the subcommand ───────────────────


def test_global_flags_accepted_after_subcommand(tmp_path):
    from sentinella.cli import _build_parser

    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[general]\nrefresh_interval = 3\n", encoding="utf-8")
    parser = _build_parser()

    after = parser.parse_args(["print", "-c", str(cfg_file)])
    before = parser.parse_args(["-c", str(cfg_file), "print"])
    assert after.config == before.config == str(cfg_file)


def test_subcommand_does_not_clobber_root_global_flag(tmp_path):
    """argparse parents= would reset --config to None without SUPPRESS."""
    from sentinella.cli import _build_parser

    args = _build_parser().parse_args(["-c", "root.toml", "print"])
    assert args.config == "root.toml"


# ── Phase 1 (C3/C4): the CLI must not silently do the wrong thing ────────────


def test_fetch_honours_remote_flag(monkeypatch):
    """``--remote`` is advertised on every subparser, but ``fetch`` always
    built a local Collector — so ``sentinella --remote host fetch`` summarised the
    *local* machine with no indication anything was ignored."""
    seen = {}

    class FakeRemote:
        def __init__(self, address, api_key=""):
            seen["address"] = address
            seen["api_key"] = api_key

        def collect(self):
            return SystemSnapshot()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr("sentinella.remote.client.RemoteCollector", FakeRemote)
    monkeypatch.setattr("sentinella.fetch.Console", lambda *a, **k: _SilentConsole())

    main(["--remote", "10.0.0.5:9090", "fetch"])

    assert seen["address"] == "10.0.0.5:9090"
    assert seen.get("closed"), "remote collector was not released"


class _SilentConsole:
    def print(self, *args, **kwargs):
        pass


@pytest.mark.parametrize("command", ["web", "serve"])
def test_remote_with_a_server_command_is_rejected(command, capsys):
    """Silently ignoring --remote here meant `sentinella --remote host serve`
    started an agent reporting the *local* host."""
    with pytest.raises(SystemExit):
        main(["--remote", "10.0.0.5:9090", command])
    assert "--remote cannot be used" in capsys.readouterr().err


def test_print_rejects_an_unknown_module(capsys):
    """A typo used to exit 0 with empty output, reading as 'this host reports
    nothing' rather than 'you misspelled it'."""
    with pytest.raises(SystemExit):
        main(["print", "cpu", "bogusmodule"])
    err = capsys.readouterr().err
    assert "bogusmodule" in err
    assert "Valid modules" in err


def test_print_still_accepts_valid_modules(capsys):
    main(["print", "cpu", "--format", "json"])
    out = capsys.readouterr().out
    assert '"cpu"' in out


@pytest.mark.parametrize("command", ["fetch", "print"])
def test_unreachable_remote_is_a_user_error_not_a_traceback(command, capsys):
    """An agent that is down is a predictable user error. It used to surface as
    a raw httpx traceback; config errors have always been reported cleanly and
    this should match."""
    with pytest.raises(SystemExit):
        main(["--remote", "127.0.0.1:9099", command])
    err = capsys.readouterr().err
    assert "could not reach remote agent at 127.0.0.1:9099" in err
    assert "Traceback" not in err


def test_remote_auth_failure_names_the_api_key(monkeypatch, capsys):
    import httpx

    class Unauthorized:
        def __init__(self, address, api_key=""):
            pass

        def collect(self):
            request = httpx.Request("GET", "http://x/api/v1/snapshot")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("401", request=request, response=response)

        def close(self):
            pass

    monkeypatch.setattr("sentinella.remote.client.RemoteCollector", Unauthorized)
    with pytest.raises(SystemExit):
        main(["--remote", "10.0.0.5:9090", "print"])
    assert "rejected the API key" in capsys.readouterr().err
