"""Typer runtime object shared by command modules."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platform_cli.client import PlatformClient
from platform_cli.config import CLIConfig


@dataclass(frozen=True)
class Runtime:
    """CLI runtime state after config and overrides are loaded."""

    config: CLIConfig
    config_path: Path
    loaded_config_path: Path

    def client(self) -> PlatformClient:
        """Create a platform API client for one command."""
        return PlatformClient(self.config)
