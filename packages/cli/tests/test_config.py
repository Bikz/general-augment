"""Tests for standalone CLI config persistence."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from platform_cli.config import (
    CLIConfig,
    apply_runtime_overrides,
    clear_config,
    default_config_path,
    load_config,
    resolve_config_paths,
    save_config,
)


def test_config_file_crud(tmp_path: Path) -> None:
    """Config should save, load, and clear from a custom path."""
    config_path = tmp_path / "config.yaml"
    config = CLIConfig(base_url="https://api.example.test", api_key="secret", active_project="p1")

    saved = save_config(config, config_path)
    loaded = load_config(saved)
    mode = stat.S_IMODE(saved.stat().st_mode)
    clear_config(saved)

    assert saved == config_path
    assert loaded.base_url == "https://api.example.test"
    assert loaded.api_key == "secret"
    assert loaded.active_project == "p1"
    assert mode == 0o600
    assert not saved.exists()


def test_default_config_path_uses_genaug_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public default config path should live under ~/.genaug."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENAUG_CLI_CONFIG", raising=False)

    assert default_config_path() == tmp_path / ".genaug" / "config.yaml"


def test_config_path_env_uses_genaug_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GENAUG_CLI_CONFIG should override the default config path."""
    public_path = tmp_path / "public.yaml"
    monkeypatch.setenv("GENAUG_CLI_CONFIG", str(public_path))

    assert default_config_path() == public_path

def test_resolve_config_paths_uses_public_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read and write paths should both use the public config location."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENAUG_CLI_CONFIG", raising=False)

    paths = resolve_config_paths()

    assert paths.load_path == tmp_path / ".genaug" / "config.yaml"
    assert paths.save_path == tmp_path / ".genaug" / "config.yaml"


def test_runtime_env_overrides_use_genaug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public GENAUG_* env vars should override stored values."""
    config = CLIConfig(base_url="https://stored.test", api_key="stored")
    monkeypatch.delenv("GENAUG_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("GENAUG_API_BASE_URL", raising=False)
    monkeypatch.delenv("GENAUG_ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("GENAUG_API_KEY", raising=False)

    monkeypatch.setenv("GENAUG_API_BASE_URL", "https://public.test")
    monkeypatch.setenv("GENAUG_ADMIN_API_KEY", "public")

    preferred = apply_runtime_overrides(config)

    assert preferred.base_url == "https://public.test"
    assert preferred.api_key == "public"
