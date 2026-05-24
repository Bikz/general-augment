"""Starter agent scaffold command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from platform_cli.errors import CLIError
from platform_cli.openapi import scaffold_basic_agent
from platform_cli.output import print_json, print_success, print_warning, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import build_setup_payload, write_payload


def init(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(help="Agent/project name for scaffold mode, such as dayplan."),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option(help="Existing app workspace to inspect when NAME is omitted."),
    ] = Path("."),
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability to configure when NAME is omitted."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write setup plan JSON when NAME is omitted."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print setup plan JSON when NAME is omitted."),
    ] = False,
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
    """Create a starter agent scaffold, or inspect an existing app when NAME is omitted."""
    if name is None:
        runtime: Runtime = ctx.obj
        payload = build_setup_payload(
            workspace=workspace,
            config=runtime.config,
            requested_capabilities=capability or [],
        )
        artifact_path = write_payload(payload, output, workspace)
        payload["artifact_path"] = str(artifact_path)
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if json_output:
            print_json(payload)
            return
        table(
            "General Augment setup plan",
            ["Field", "Value"],
            [
                ["Workspace", payload["workspace"]["root"]],
                ["Frameworks", ", ".join(payload["detected"]["frameworks"])],
                ["Auth", payload["auth"]["status"]],
                ["Artifact", artifact_path],
            ],
        )
        print_success("Setup plan written without changing app code or storing secrets.")
        return
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
