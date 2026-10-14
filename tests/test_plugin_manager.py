from unittest.mock import patch

from sentinella.config import (
    ModulesConfig,
    SentinellaConfig,
)
from sentinella.core.plugin_manager import get_enabled_plugins


def test_plugin_manager_filtering():
    # Test enabling all
    cfg = SentinellaConfig(
        modules=ModulesConfig(
            cpu=True,
            memory=True,
            disk=True,
            network=True,
            processes=True,
            users=True,
            sensors=True,
            containers=True,
        )
    )
    plugins = get_enabled_plugins(cfg)
    names = {p.name for p in plugins}
    assert "cpu" in names
    assert "memory" in names

    # Test disabling cpu
    cfg_no_cpu = SentinellaConfig(
        modules=ModulesConfig(
            cpu=False,
            memory=True,
            disk=True,
            network=True,
            processes=True,
            users=True,
            sensors=True,
            containers=True,
        )
    )
    plugins_no_cpu = get_enabled_plugins(cfg_no_cpu)
    names_no_cpu = {p.name for p in plugins_no_cpu}
    assert "cpu" not in names_no_cpu
    assert "memory" in names_no_cpu


# ── Phase 2 (S4): plugin discovery must not pick up an imported class ───────


def test_plugin_discovery_ignores_imported_plugin_classes():
    """``dir(mod)`` is alphabetical, so a module importing a sibling plugin
    would silently load that one instead of its own."""
    import types

    from sentinella.core.plugin_manager import _load_plugin_module
    from sentinella.plugins.base import MonitorPlugin
    from sentinella.plugins.cpu import CpuPlugin

    module = types.ModuleType("sentinella.plugins.zzz_fake")

    class ActualPlugin(MonitorPlugin):
        name = "zzz_fake"

        def collect(self):
            return None

        def is_available(self):
            return True

    ActualPlugin.__module__ = "sentinella.plugins.zzz_fake"
    # "AaaImported" sorts before "ActualPlugin" — the old scan would win with it.
    module.AaaImported = CpuPlugin
    module.ActualPlugin = ActualPlugin

    with patch.dict("sys.modules", {"sentinella.plugins.zzz_fake": module}):
        assert _load_plugin_module("zzz_fake") is ActualPlugin
