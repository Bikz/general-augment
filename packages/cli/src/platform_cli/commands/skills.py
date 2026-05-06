"""Skill management commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage tenant skills.")


@app.command("list")
def list_skills(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List SKILL.md files registered for a tenant."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/skills",
        )
    if json_output:
        print_json(payload)
        return
    items = payload.get("items", []) if isinstance(payload, dict) else []
    rows = [
        [
            item.get("name", ""),
            item.get("description", ""),
            item.get("version", ""),
            ", ".join(str(tag) for tag in item.get("tags", []) or []),
            ", ".join(str(tool) for tool in item.get("tools", []) or []),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    table(
        f"Skills for {project_payload.get('slug', project)}",
        ["Name", "Description", "Version", "Tags", "Tools"],
        rows,
    )


@app.command("view")
def view_skill(
    ctx: typer.Context,
    skill_name: str = typer.Argument(..., help="Skill name."),
    project: str = typer.Option(..., help="Project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show one tenant SKILL.md file."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "GET",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/skills/"
                f"{encode_path_segment(skill_name)}"
            ),
        )
    if json_output:
        print_json(payload)
        return
    typer.echo(str(payload.get("content", "")) if isinstance(payload, dict) else "")


@app.command("apply")
def apply_skill(
    ctx: typer.Context,
    skill_file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Path to a SKILL.md file."),
    ],
    project: str = typer.Option(..., help="Project id, slug, or name."),
) -> None:
    """Create or replace one tenant skill from a local SKILL.md file."""
    runtime: Runtime = ctx.obj
    content = skill_file.read_text(encoding="utf-8")
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/skills",
            json={"content": content},
        )
    print_success(f"Applied skill {payload.get('name', skill_file.stem)}.")


@app.command("delete")
def delete_skill(
    ctx: typer.Context,
    skill_name: str = typer.Argument(..., help="Skill name."),
    project: str = typer.Option(..., help="Project id, slug, or name."),
) -> None:
    """Delete one tenant skill."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "DELETE",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/skills/"
                f"{encode_path_segment(skill_name)}"
            ),
        )
    print_success(f"Deleted skill {payload.get('name', skill_name)}.")
