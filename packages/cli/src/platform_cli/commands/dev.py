"""Local development command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from platform_cli.output import panel


def dev(
    config_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="genaug-agent.yaml manifest to run locally.",
        ),
    ],
    message: Annotated[
        str | None,
        typer.Option(help="Run one message and exit."),
    ] = None,
) -> None:
    """Run a local mock REPL for config and personality iteration."""
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    name = metadata.get("display_name") or metadata.get("name") or "Agent"
    if message:
        panel("Local dev response", f"{name} would respond to: {message}")
        return
    panel("Local dev", f"Loaded {name}. Type Ctrl+C to exit.")
    while True:
        user_message = typer.prompt("you")
        panel("Local dev response", f"{name} would respond to: {user_message}")
