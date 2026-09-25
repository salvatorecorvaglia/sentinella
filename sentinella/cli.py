"""CLI entry point for Sentinella.

Usage::

    sentinella              # TUI dashboard
    sentinella fetch        # Quick system summary
    sentinella web          # Web dashboard
    sentinella serve        # Remote monitoring agent
    sentinella print [MOD]  # Print stats to stdout
"""

from __future__ import annotations

import argparse
import logging
import tomllib
import urllib.parse
from typing import TYPE_CHECKING

import uvicorn

from sentinella.config import load_config, validate_config

if TYPE_CHECKING:
    from sentinella.config import SentinellaConfig


def remote_address_type(value: str) -> str:
    """Argparse type validator for remote host:port addresses."""
    url_str = value
    if not url_str.startswith(("http://", "https://")):
        url_str = f"http://{url_str}"
    try:
        parsed = urllib.parse.urlparse(url_str)
        if not parsed.hostname:
            raise ValueError("Hostname is required")
        if parsed.port is not None:
            if not (1 <= parsed.port <= 65535):
                raise ValueError("Port must be between 1 and 65535")
    except Exception as e:
        raise argparse.ArgumentTypeError(f"Invalid remote address format '{value}': {e}") from e
    return value


def _build_global_options() -> argparse.ArgumentParser:
    """Global flags, shared as a parent parser.

    Attaching these to every subparser as well as the root lets users write
    either ``sentinella -c sentinella.toml print`` or ``sentinella print -c sentinella.toml``.

    Every option defaults to ``SUPPRESS`` so that an unsupplied flag on the
    subparser does not overwrite the value already parsed by the root parser;
    callers read them with ``getattr(args, name, default)``.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        "-c",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="Path to sentinella.toml config file",
    )
    common.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Enable verbose debug logging",
    )
    common.add_argument(
        "--remote",
        "-r",
        type=remote_address_type,
        metavar="HOST:PORT",
        default=argparse.SUPPRESS,
        help="Connect to a remote Sentinella agent",
    )
    return common


def _build_parser() -> argparse.ArgumentParser:
    common = _build_global_options()
    parser = argparse.ArgumentParser(
        prog="sentinella",
        description="Sentinella — cross-platform system monitor",
        parents=[common],
    )

    sub = parser.add_subparsers(dest="command")

    # ── fetch ────────────────────────────────────────────────────────
    sub.add_parser("fetch", help="Quick system summary (neofetch-style)", parents=[common])

    # ── web ──────────────────────────────────────────────────────────
    web_p = sub.add_parser("web", help="Start the web dashboard", parents=[common])
    web_p.add_argument("--host", default=None, help="Bind host (default: from config)")
    web_p.add_argument(
        "--port", "-p", type=int, default=None, help="Bind port (default: from config)"
    )

    # ── serve ────────────────────────────────────────────────────────
    serve_p = sub.add_parser("serve", help="Start remote monitoring agent", parents=[common])
    serve_p.add_argument("--host", default=None, help="Bind host")
    serve_p.add_argument("--port", "-p", type=int, default=None, help="Bind port")

    # ── print ────────────────────────────────────────────────────────
    print_p = sub.add_parser("print", help="Print selected stats to stdout", parents=[common])
    print_p.add_argument(
        "modules",
        nargs="*",
        default=None,
        help=(
            "Module names to print (cpu, memory, disk, network, "
            "processes, users, sensors, containers, system_info)"
        ),
    )
    print_p.add_argument(
        "--format",
        "-f",
        choices=["text", "csv", "json"],
        default=None,
        help="Output format (default: from config)",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    """Main entry point."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Config problems are user errors, not crashes — report them the way
    # argparse reports a bad flag rather than as a traceback.
    try:
        config = load_config(getattr(args, "config", None))
    except (FileNotFoundError, ValueError, tomllib.TOMLDecodeError) as exc:
        parser.error(str(exc))

    command = args.command

    # ``--remote`` is a global flag, so it reaches every subcommand's namespace.
    # Only the collector-backed commands can act on it; the servers would
    # silently ignore it and monitor the local host instead, so say so.
    if getattr(args, "remote", None) and command in ("web", "serve"):
        parser.error(
            f"--remote cannot be used with '{command}': it starts a server that "
            "reports this host. Run it on the host you want to monitor, or use "
            f"'sentinella --remote ... print' / 'sentinella --remote ...' instead."
        )

    # ── fetch ────────────────────────────────────────────────────────
    if command == "fetch":
        from sentinella.fetch import run_fetch

        # Was always a local Collector, so `sentinella --remote host fetch`
        # silently summarised the local machine.
        collector = _get_collector(args, config)
        try:
            run_fetch(config, collector=collector)
        except Exception as exc:
            remote_addr = getattr(args, "remote", None)
            if remote_addr:
                parser.error(_remote_error_message(remote_addr, exc))
            raise
        finally:
            collector.close()
        return

    # ── print ────────────────────────────────────────────────────────
    if command == "print":
        try:
            _cmd_print(args, config, parser)
        except RemoteUnavailable as exc:
            parser.error(str(exc))
        return

    # ── web ──────────────────────────────────────────────────────────
    if command == "web":
        _cmd_web(args, config)
        return

    # ── serve ────────────────────────────────────────────────────────
    if command == "serve":
        _cmd_serve(args, config)
        return

    # ── default: TUI ─────────────────────────────────────────────────
    _cmd_tui(args, config)


# ── Sub-command handlers ─────────────────────────────────────────────────────


class RemoteUnavailable(Exception):
    """A remote agent could not be reached or refused the request."""


def _remote_error_message(addr: str, exc: Exception) -> str:
    """Turn an httpx failure into something a user can act on."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
        return (
            f"remote agent at {addr} rejected the API key. "
            "Set a matching [remote] api_key in your config."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return f"remote agent at {addr} returned HTTP {exc.response.status_code}"
    return f"could not reach remote agent at {addr}: {exc}"


def _get_collector(args: argparse.Namespace, config: SentinellaConfig):
    """Instantiate a local or remote collector based on CLI arguments."""
    remote_addr = getattr(args, "remote", None)
    if remote_addr:
        from sentinella.remote.client import RemoteCollector

        return RemoteCollector(remote_addr, api_key=config.remote.api_key)

    from sentinella.core.collector import Collector

    return Collector(config)


def _cmd_print(
    args: argparse.Namespace, config: SentinellaConfig, parser: argparse.ArgumentParser
) -> None:
    fmt = args.format or config.export.format
    modules = args.modules or None

    # An unrecognised name used to produce empty output and exit 0, so a typo
    # looked like "this host reports nothing" rather than a mistake.
    if modules:
        from sentinella.core.api import VALID_MODULES

        unknown = [m for m in modules if m not in VALID_MODULES]
        if unknown:
            parser.error(
                f"unknown module(s): {', '.join(sorted(unknown))}. "
                f"Valid modules: {', '.join(sorted(VALID_MODULES))}"
            )

    from sentinella.export import get_exporter

    remote_addr = getattr(args, "remote", None)
    collector = _get_collector(args, config)
    # JSON and CSV carry the cmdline field, so pay for it here — it is skipped
    # everywhere else because nothing renders it. Text output doesn't show it.
    if fmt in ("json", "csv"):
        set_cmdline = getattr(collector, "set_collect_cmdline", None)
        if set_cmdline is not None:
            set_cmdline(True)
    try:
        try:
            snapshot = collector.collect()
        except Exception as exc:
            # An unreachable agent is a user error, not a crash — report it the
            # way a bad config is reported rather than as a traceback.
            if remote_addr:
                raise RemoteUnavailable(_remote_error_message(remote_addr, exc)) from exc
            raise
        print(get_exporter(fmt, config).format(snapshot, modules))
    finally:
        # RemoteCollector holds an HTTP connection pool; Collector a thread pool.
        collector.close()


def _cmd_web(args: argparse.Namespace, config: SentinellaConfig) -> None:
    from sentinella.web.server import create_app

    # Fold CLI overrides into the config *before* building the app, so the
    # app's own view of the bind address (and its open-bind security warning)
    # matches where uvicorn actually listens.
    if args.host is not None:
        config.web.host = args.host
    if args.port is not None:
        config.web.port = args.port
    validate_config(config)

    app = create_app(config)
    print(f"🔔 Sentinella web dashboard → http://{config.web.host}:{config.web.port}")
    uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info")


def _cmd_serve(args: argparse.Namespace, config: SentinellaConfig) -> None:
    from sentinella.remote.server import create_remote_app

    # See _cmd_web: overrides must reach create_remote_app, not just uvicorn.
    if args.host is not None:
        config.remote.host = args.host
    if args.port is not None:
        config.remote.port = args.port
    validate_config(config)

    app = create_remote_app(config)
    print(f"🔔 Sentinella remote agent → http://{config.remote.host}:{config.remote.port}")
    uvicorn.run(app, host=config.remote.host, port=config.remote.port, log_level="info")


def _cmd_tui(args: argparse.Namespace, config: SentinellaConfig) -> None:
    collector = _get_collector(args, config)

    if not getattr(args, "remote", None):
        from sentinella.core.runner import start_background_servers

        start_background_servers(config, collector=collector)

    from sentinella.tui.app import SentinellaApp

    app = SentinellaApp(collector=collector, config=config)
    app.run()


if __name__ == "__main__":
    main()
