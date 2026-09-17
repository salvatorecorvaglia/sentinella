import os
import sys
import tempfile

import pytest

from sentinella.config import (
    ExportConfig,
    GeneralConfig,
    ProcessesConfig,
    RemoteConfig,
    SentinellaConfig,
    WebConfig,
    _dict_to_config,
    load_config,
    validate_config,
    warn_insecure_config_permissions,
)


def test_default_config():
    cfg = load_config()
    assert isinstance(cfg, SentinellaConfig)
    assert cfg.general.refresh_interval == 2.0
    assert cfg.web.host == "127.0.0.1"
    assert cfg.remote.host == "127.0.0.1"


def test_dict_to_config_filtering():
    raw_data = {
        "general": {"refresh_interval": 5.0, "unknown_key": "ignored"},
        "modules": {"cpu": True},
        "web": {"port": 9999, "invalid": 123},
    }
    cfg = _dict_to_config(raw_data)
    assert cfg.general.refresh_interval == 5.0
    assert cfg.web.port == 9999


def test_load_explicit_config():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write("[general]\nrefresh_interval = 10.0\n[web]\nenabled = true\nport = 8888\n")
        f_path = f.name

    try:
        cfg = load_config(explicit_path=f_path)
        assert cfg.general.refresh_interval == 10.0
        assert cfg.web.enabled is True
        assert cfg.web.port == 8888
    finally:
        os.unlink(f_path)


def test_config_validation():
    # Valid config
    valid_cfg = SentinellaConfig()
    validate_config(valid_cfg)  # should not raise

    # Invalid refresh_interval
    with pytest.raises(ValueError, match="refresh_interval must be >= 1"):
        validate_config(SentinellaConfig(general=GeneralConfig(refresh_interval=0)))

    # Invalid theme
    with pytest.raises(ValueError, match="theme must be 'dark' or 'light'"):
        validate_config(SentinellaConfig(general=GeneralConfig(theme="blue")))

    # Invalid web port
    with pytest.raises(ValueError, match="web.port must be between 1 and 65535"):
        validate_config(SentinellaConfig(web=WebConfig(port=99999)))

    # Invalid remote port
    with pytest.raises(ValueError, match="remote.port must be between 1 and 65535"):
        validate_config(SentinellaConfig(remote=RemoteConfig(port=0)))

    # Invalid format
    with pytest.raises(ValueError, match="export.format must be 'text', 'csv', or 'json'"):
        validate_config(SentinellaConfig(export=ExportConfig(format="yaml")))

    # Invalid max_display
    with pytest.raises(ValueError, match="processes.max_display must be >= 1"):
        validate_config(SentinellaConfig(processes=ProcessesConfig(max_display=0)))

    # Invalid sort_by
    with pytest.raises(ValueError, match="processes.sort_by must be one of"):
        validate_config(SentinellaConfig(processes=ProcessesConfig(sort_by="invalid")))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_world_readable_config_with_api_key_warns(tmp_path, capsys):
    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[web]\napi_key = 'secret'\n", encoding="utf-8")
    cfg_file.chmod(0o644)  # group/other readable

    warn_insecure_config_permissions(cfg_file, SentinellaConfig(web=WebConfig(api_key="secret")))
    assert "readable by other users" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_private_config_with_api_key_does_not_warn(tmp_path, capsys):
    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[web]\napi_key = 'secret'\n", encoding="utf-8")
    cfg_file.chmod(0o600)  # owner-only

    warn_insecure_config_permissions(cfg_file, SentinellaConfig(web=WebConfig(api_key="secret")))
    assert capsys.readouterr().err == ""


def test_world_readable_config_without_api_key_does_not_warn(tmp_path, capsys):
    cfg_file = tmp_path / "sentinella.toml"
    cfg_file.write_text("[general]\nrefresh_interval = 2\n", encoding="utf-8")

    warn_insecure_config_permissions(cfg_file, SentinellaConfig())
    assert capsys.readouterr().err == ""


def test_config_scalar_sections():
    # Test that scalar values in place of config sections are ignored and fallback to defaults
    raw_data = {"general": 123, "web": True, "remote": {"port": 9090}}
    cfg = _dict_to_config(raw_data)
    assert cfg.general.refresh_interval == 2  # default
    assert cfg.web.port == 8080  # default
    assert cfg.remote.port == 9090  # custom overridden value
