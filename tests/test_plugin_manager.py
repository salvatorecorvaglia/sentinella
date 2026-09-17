from sentinella.config import ModulesConfig, SentinellaConfig
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
