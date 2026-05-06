"""Starter agent scaffold command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from platform_cli.errors import CLIError
from platform_cli.openapi import scaffold_basic_agent
from platform_cli.output import print_success, print_warning, table


def init(
    name: Annotated[str, typer.Argument(help="Agent/project name, such as dayplan.")],
    output_dir: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
    display_name: Annotated[str | None, typer.Option(help="Tenant-facing display name.")] = None,
    description: Annotated[
        str | None,
        typer.Option(help="Agent purpose shown in SOUL.md and the handoff prompt."),
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Builtin tool ID to enable, for example web_search."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing starter files.")] = False,
) -> None:
    """Create a starter genaug-agent.yaml workspace without an OpenAPI spec."""
    try:
        result = scaffold_basic_agent(
            name=name,
            output_dir=output_dir,
            display_name=display_name,
            description=description,
            builtin_tools=tool,
            force=force,
        )
    except FileExistsError as exc:
        raise CLIError(str(exc)) from exc
    rows: list[list[object]] = [
        ["Manifest", result.config_path],
        ["Personality", result.soul_path],
        ["Skills", result.skills_dir],
        ["Tools", result.tools_dir],
        ["Handoff", result.agent_prompt_path],
    ]
    table("Starter agent scaffold", ["File", "Path"], rows)
    print_success(f"Generated starter agent in {result.root}")
    if result.builtin_tools:
        print_success(f"Enabled builtin tools: {', '.join(result.builtin_tools)}")
    else:
        print_warning("No builtin tools enabled yet. Use --tool or genaug tools toggle later.")
    typer.echo(f"Next: genaug dev {result.config_path} --message \"What can you help me with?\"")
    typer.echo(f"Then: genaug deploy {result.config_path}")
