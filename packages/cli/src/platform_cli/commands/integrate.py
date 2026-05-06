"""OpenAPI integration scaffolding command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment
from platform_cli.openapi import scaffold_from_openapi
from platform_cli.output import print_success, print_warning, table
from platform_cli.runtime import Runtime


def integrate(
    ctx: typer.Context,
    spec_url: Annotated[str, typer.Argument(help="OpenAPI spec URL or file path.")],
    name: Annotated[str | None, typer.Option(help="Agent name.")] = None,
    output_dir: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
    description: Annotated[
        str | None,
        typer.Option(help="Agent personality description."),
    ] = None,
    target_count: Annotated[int, typer.Option(help="Target generated tool count.")] = 15,
    force: Annotated[bool, typer.Option(help="Overwrite existing generated files.")] = False,
    auto_deploy: Annotated[
        bool,
        typer.Option(help="Deploy immediately after scaffolding."),
    ] = False,
) -> None:
    """Generate a local agent scaffold from an OpenAPI spec."""
    runtime: Runtime = ctx.obj
    result = scaffold_from_openapi(
        spec_url,
        output_dir=output_dir,
        name=name,
        description=description,
        target_count=target_count,
        force=force,
    )
    rows: list[list[object]] = [
        [
            "disabled" if not tool.enabled else "enabled",
            tool.tool_id,
            tool.http_method,
            tool.risk_level,
            "yes" if tool.requires_approval else "no",
        ]
        for tool in result.tools
    ]
    table(
        f"Generated {len(result.tools)} tools from {len(result.parsed_api.tools)} endpoints",
        ["State", "Tool", "Method", "Risk", "Approval"],
        rows,
    )
    print_success(f"Generated scaffold in {result.root}")
    print_success(f"Coding-agent handoff written to {result.agent_prompt_path}")
    if any(not tool.enabled for tool in result.tools):
        print_warning("Destructive operations are disabled by default.")
    if auto_deploy:
        from platform_cli.commands.deploy import deploy_path

        project = deploy_path(runtime, result.config_path, project_ref=None)
        project_id = project.get("id")
        if not project_id:
            print_warning(
                "Project deployed, but OpenAPI tools were not registered: missing project id."
            )
            return
        with runtime.client() as client:
            response = client.admin(
                "POST",
                f"/projects/{encode_path_segment(str(project_id))}/tools/from-openapi",
                json={
                    "spec_url": _deployable_spec_source(spec_url),
                    "target_count": target_count,
                    "auto_deploy": True,
                },
            )
        generated = response.get("generated_count", 0)
        curated = response.get("curated_count", 0)
        enabled = len(response.get("enabled_tool_ids") or [])
        print_success(
            f"Registered OpenAPI tools: {enabled} enabled, {curated} curated, {generated} generated"
        )


def _deployable_spec_source(spec_url: str) -> str:
    """Return a server-readable OpenAPI source for hosted registration."""
    spec_path = Path(spec_url).expanduser()
    if spec_path.exists() and spec_path.is_file():
        return spec_path.read_text(encoding="utf-8")
    return spec_url
