"""Plugin discovery and loading.

Discovers built-in plugins from the ``sentinella.plugins`` package and
instantiates those enabled in the configuration.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

import sentinella.plugins
from sentinella.config import SentinellaConfig
from sentinella.plugins.base import MonitorPlugin

log = logging.getLogger(__name__)

# Dynamically discover plugin module names (relative to sentinella.plugins)
_BUILTIN_PLUGINS = sorted(
    [
        name
        for _, name, ispkg in pkgutil.iter_modules(sentinella.plugins.__path__)
        if not ispkg and name != "base"
    ]
)


# Map plugin names → module config field names
_NAME_TO_CONFIG = {
    "cpu": "cpu",
    "memory": "memory",
    "disk": "disk",
    "network": "network",
    "processes": "processes",
    "users": "users",
    "sensors": "sensors",
    "containers": "containers",
    "system_info": None,  # always enabled — lightweight
}


def _load_plugin_module(module_name: str) -> type[MonitorPlugin] | None:
    """Import a plugin module and return the ``MonitorPlugin`` class it defines.

    Only classes *defined in* the module count.  Scanning ``dir(mod)`` and
    taking the first match returned whatever came first alphabetically, so a
    module that imported a sibling plugin (for a type hint, say) would silently
    load the wrong class.
    """
    fqn = f"sentinella.plugins.{module_name}"
    try:
        mod = importlib.import_module(fqn)
    except Exception:
        log.exception("Failed to load plugin module %s", fqn)
        return None

    owned = [
        obj
        for name in dir(mod)
        if isinstance(obj := getattr(mod, name), type)
        and issubclass(obj, MonitorPlugin)
        and obj is not MonitorPlugin
        and obj.__module__ == fqn
    ]
    if not owned:
        log.warning("Plugin module %s defines no MonitorPlugin subclass — skipping", fqn)
        return None
    if len(owned) > 1:
        log.warning(
            "Plugin module %s defines %d MonitorPlugin subclasses (%s) — using %s",
            fqn,
            len(owned),
            [c.__name__ for c in owned],
            owned[0].__name__,
        )
    return owned[0]


def get_enabled_plugins(config: SentinellaConfig) -> list[MonitorPlugin]:
    """Return instantiated plugin objects for all enabled modules."""
    plugins: list[MonitorPlugin] = []

    for mod_name in _BUILTIN_PLUGINS:
        # Check if the module is enabled in config
        config_key = _NAME_TO_CONFIG.get(mod_name)
        if config_key is not None:
            if not getattr(config.modules, config_key, True):
                log.debug("Plugin %s disabled by config", mod_name)
                continue

        cls = _load_plugin_module(mod_name)
        if cls is None:
            continue

        # Every plugin takes the config (MonitorPlugin.__init__), so there is
        # no need to inspect signatures to decide how to construct it.
        try:
            instance: MonitorPlugin = cls(config=config)
        except Exception:
            log.exception("Plugin %s failed to instantiate — skipping", mod_name)
            continue

        if instance.is_available():
            plugins.append(instance)
            log.debug("Loaded plugin: %s", instance.name)
        else:
            log.debug("Plugin %s not available on this platform", instance.name)

    return plugins
