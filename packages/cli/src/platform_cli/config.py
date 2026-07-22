"""Local CLI configuration stored in ~/.genaug/config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_BASE_URL = "https://api.generalaugment.com"
CONFIG_PATH_ENV = "GENAUG_CLI_CONFIG"
CONFIG_DIR = ".genaug"


@dataclass(frozen=True)
class ConfigPaths:
    """Resolved config paths for reading existing auth and writing new auth."""

    load_path: Path
    save_path: Path


class CLIConfig(BaseModel):
    """Persisted CLI configuration."""

    base_url: str = DEFAULT_BASE_URL
    # Deprecated compatibility field for management API keys. Runtime credentials
    # must never be written here.
    api_key: str | None = None
    runtime_api_key: str | None = None
    runtime_key_id: str | None = None
    runtime_key_project_id: str | None = None
    runtime_key_scopes: list[str] = Field(default_factory=list)
    runtime_key_mode: Literal["test", "live"] | None = None
    release_preview_binding_id: str | None = None
    release_preview_release_id: str | None = None
    release_preview_fingerprint: str | None = None
    release_preview_expires_at: str | None = None
    active_workspace: str | None = None
    active_project: str | None = None
    profile: str = "default"
    metadata: dict[str, Any] = Field(default_factory=dict)


def default_config_path() -> Path:
    """Return the preferred config path."""
    override = _config_path_override()
    if override:
        return override
    return Path.home() / CONFIG_DIR / "config.yaml"


def resolve_config_paths(path: Path | None = None) -> ConfigPaths:
    """Resolve read and write paths."""
    if path:
        resolved = path.expanduser()
        return ConfigPaths(load_path=resolved, save_path=resolved)
    override = _config_path_override()
    if override:
        return ConfigPaths(load_path=override, save_path=override)
    preferred = default_config_path()
    return ConfigPaths(load_path=preferred, save_path=preferred)


def load_config(path: Path | None = None) -> CLIConfig:
    """Load CLI config from disk or return defaults."""
    resolved = path or default_config_path()
    if not resolved.exists():
        return CLIConfig()
    payload = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return CLIConfig()
    return CLIConfig.model_validate(payload)


def save_config(config: CLIConfig, path: Path | None = None) -> Path:
    """Write CLI config and return the path."""
    resolved = path or default_config_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False),
        encoding="utf-8",
    )
    resolved.chmod(0o600)
    return resolved


def clear_config(path: Path | None = None) -> None:
    """Remove the persisted config file when it exists."""
    resolved = path or default_config_path()
    if resolved.exists():
        resolved.unlink()


def apply_runtime_overrides(
    config: CLIConfig,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> CLIConfig:
    """Apply CLI flags and environment variables without mutating the stored config."""
    return config.model_copy(
        update={
            "base_url": (
                base_url
                or os.getenv("GENAUG_ADMIN_BASE_URL")
                or os.getenv("GENAUG_API_BASE_URL")
                or config.base_url
            ),
            "api_key": (api_key or os.getenv("GENAUG_ADMIN_API_KEY") or config.api_key),
            "runtime_api_key": os.getenv("GENAUG_API_KEY") or config.runtime_api_key,
        }
    )


def _config_path_override() -> Path | None:
    """Return a configured path."""
    override = os.getenv(CONFIG_PATH_ENV)
    return Path(override).expanduser() if override else None
