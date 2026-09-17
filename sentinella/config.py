"""TOML-based configuration for Sentinella.

Search order (first match wins):
1. ``--config`` CLI flag
2. ``./sentinella.toml``
3. ``~/.config/sentinella/sentinella.toml``
4. Built-in defaults
"""

from __future__ import annotations

import logging
import stat
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULTS: dict[str, Any] = {
    "general": {
        "refresh_interval": 2.0,
        "theme": "dark",
    },
    "modules": {
        "cpu": True,
        "memory": True,
        "disk": True,
        "network": True,
        "processes": True,
        "users": True,
        "sensors": True,
        "containers": True,
        "container_stats": False,
    },
    "web": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8080,
        "api_key": "",
    },
    "remote": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 9090,
        "api_key": "",
    },
    "export": {
        "format": "text",
    },
    "processes": {
        "max_display": 25,
        "sort_by": "cpu",
    },
}


# ── Config Dataclass ─────────────────────────────────────────────────────────


@dataclass
class GeneralConfig:
    # Float rather than int: the value is divided (cache TTL is interval/2) and
    # fed to timers, and TOML users reasonably write ``2.5``.
    refresh_interval: float = 2.0
    theme: str = "dark"


@dataclass
class ModulesConfig:
    cpu: bool = True
    memory: bool = True
    disk: bool = True
    network: bool = True
    processes: bool = True
    users: bool = True
    sensors: bool = True
    containers: bool = True
    # Per-container CPU/memory stats cost one blocking Docker API call per
    # running container, so they are opt-in rather than on by default.
    container_stats: bool = False


@dataclass
class WebConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8080
    api_key: str = ""


@dataclass
class RemoteConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 9090
    api_key: str = ""


@dataclass
class ExportConfig:
    format: str = "text"


@dataclass
class ProcessesConfig:
    max_display: int = 25
    sort_by: str = "cpu"


@dataclass
class SentinellaConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    modules: ModulesConfig = field(default_factory=ModulesConfig)
    web: WebConfig = field(default_factory=WebConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    processes: ProcessesConfig = field(default_factory=ProcessesConfig)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (non-destructive)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce(section: str, key: str, declared: Any, value: Any) -> Any:
    """Check a TOML value against its declared field type.

    ``from __future__ import annotations`` makes ``dataclasses.fields()``
    report types as strings, so this matches on the annotation text.  Raises
    ``ValueError`` naming the offending section and key, rather than letting a
    wrong type surface later as an opaque ``TypeError`` during validation.
    """
    type_name = declared if isinstance(declared, str) else getattr(declared, "__name__", "")

    # bool is a subclass of int, so it must be rejected explicitly for numbers.
    checks: dict[str, tuple[type | tuple[type, ...], str]] = {
        "bool": (bool, "a boolean"),
        "str": (str, "a string"),
        "int": (int, "an integer"),
        "float": ((int, float), "a number"),
    }
    if type_name not in checks:
        return value

    expected, label = checks[type_name]
    is_bool = isinstance(value, bool)
    if type_name == "bool":
        if not is_bool:
            raise ValueError(f"{section}.{key} must be {label}, got {value!r}")
        return value

    if is_bool or not isinstance(value, expected):
        raise ValueError(f"{section}.{key} must be {label}, got {value!r}")
    if type_name == "float":
        assert isinstance(value, int | float)
        return float(value)
    return value


def _find_config_file(explicit_path: str | None = None) -> Path | None:
    """Locate the first existing config file in search order.

    Raises
    ------
    FileNotFoundError
        If *explicit_path* is given but does not point to an existing file.
    """
    if explicit_path:
        p = Path(explicit_path)
        if not p.is_file():
            raise FileNotFoundError(f"Config file not found: {explicit_path!r}")
        return p

    candidates = [
        Path.cwd() / "sentinella.toml",
        Path.home() / ".config" / "sentinella" / "sentinella.toml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _dict_to_config(data: dict[str, Any]) -> SentinellaConfig:
    """Convert a merged dict to a ``SentinellaConfig`` instance.

    Unknown keys in each section are filtered out with a warning.
    """
    section_classes: dict[str, type] = {
        "general": GeneralConfig,
        "modules": ModulesConfig,
        "web": WebConfig,
        "remote": RemoteConfig,
        "export": ExportConfig,
        "processes": ProcessesConfig,
    }

    sections: dict[str, Any] = {}
    for section_name, cls in section_classes.items():
        raw = data.get(section_name, {})
        if not isinstance(raw, dict):
            log.warning(
                "Config section [%s] must be a table — ignoring invalid values",
                section_name,
            )
            raw = {}
        declared = {f.name: f.type for f in fields(cls)}
        filtered: dict[str, Any] = {}
        for k, v in raw.items():
            if k in declared:
                filtered[k] = _coerce(section_name, k, declared[k], v)
            else:
                log.warning(
                    "Unknown config key '%s' in [%s] — ignoring",
                    k,
                    section_name,
                )
        sections[section_name] = cls(**filtered)

    return SentinellaConfig(
        general=sections["general"],
        modules=sections["modules"],
        web=sections["web"],
        remote=sections["remote"],
        export=sections["export"],
        processes=sections["processes"],
    )


def validate_config(config: SentinellaConfig) -> None:
    """Validate config values, raising ValueError for invalid settings."""
    if config.general.refresh_interval < 1:
        raise ValueError(
            f"general.refresh_interval must be >= 1, got {config.general.refresh_interval}"
        )
    if config.general.theme not in ("dark", "light"):
        raise ValueError(f"general.theme must be 'dark' or 'light', got {config.general.theme!r}")
    if not (1 <= config.web.port <= 65535):
        raise ValueError(f"web.port must be between 1 and 65535, got {config.web.port}")
    if not (1 <= config.remote.port <= 65535):
        raise ValueError(f"remote.port must be between 1 and 65535, got {config.remote.port}")
    if config.export.format not in ("text", "csv", "json"):
        raise ValueError(
            f"export.format must be 'text', 'csv', or 'json', got {config.export.format!r}"
        )
    if config.processes.max_display < 1:
        raise ValueError(f"processes.max_display must be >= 1, got {config.processes.max_display}")
    if config.processes.sort_by not in ("cpu", "memory", "pid", "name"):
        raise ValueError(
            "processes.sort_by must be one of 'cpu', 'memory', 'pid', 'name', "
            f"got {config.processes.sort_by!r}"
        )


def warn_insecure_config_permissions(path: Path, cfg: SentinellaConfig) -> None:
    """Warn if a config file holding a plaintext API key is group/world readable.

    POSIX permission bits aren't meaningful on Windows, so this is a no-op
    there. Mirrors the ``warn_open_bind`` pattern in ``core.api``: a
    ``log.warning`` for anyone watching logs, plus a stderr print for anyone
    just running the command interactively.
    """
    if sys.platform == "win32":
        return
    if not (cfg.web.api_key or cfg.remote.api_key):
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning(
            "⚠️  %s contains a plaintext api_key but is readable by other "
            "users on this system. Run `chmod 600 %s` to restrict access.",
            path,
            path,
        )
        print(
            f"⚠️  WARNING: {path} contains a plaintext api_key but is readable "
            f"by other users. Run `chmod 600 {path}` to restrict access.",
            file=sys.stderr,
        )


# ── Public API ───────────────────────────────────────────────────────────────


def load_config(explicit_path: str | None = None) -> SentinellaConfig:
    """Load and merge configuration, returning a ``SentinellaConfig``.

    Parameters
    ----------
    explicit_path:
        Optional path to a TOML config file.  Overrides auto-discovery.
    """
    config_file = _find_config_file(explicit_path)
    if config_file is not None:
        with open(config_file, "rb") as fh:
            user_data = tomllib.load(fh)
    else:
        user_data = {}

    merged = _deep_merge(_DEFAULTS, user_data)
    cfg = _dict_to_config(merged)
    validate_config(cfg)
    if config_file is not None:
        warn_insecure_config_permissions(config_file, cfg)
    return cfg
