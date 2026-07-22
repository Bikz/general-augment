"""Project context and creation commands."""

from __future__ import annotations

import typer

from platform_cli.config import save_config
from platform_cli.errors import CLIError
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import installer_access_token

app = typer.Typer(help="Manage application Projects inside a Workspace.")


def _items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [dict(row) for row in payload["items"] if isinstance(row, dict)]


def _resolve(rows: list[dict[str, object]], reference: str) -> dict[str, object]:
    normalized = reference.strip().casefold()
    matches = [
        row
        for row in rows
        if normalized
        in {
            str(row.get("id") or "").casefold(),
            str(row.get("slug") or "").casefold(),
            str(row.get("name") or "").casefold(),
        }
    ]
    if len(matches) != 1:
        raise CLIError("Project reference must match exactly one visible Project.")
    return matches[0]


@app.command("list")
def list_projects(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List Projects visible to the signed-in account."""
    runtime: Runtime = ctx.obj
    token = installer_access_token(runtime)
    with runtime.client() as client:
        payload = client.installer("GET", "/projects", token=token)
    if json_output:
        print_json(payload)
        return
    rows = _items(payload)
    table(
        "Projects",
        ["Name", "Workspace", "Slug", "ID"],
        [
            [row.get("name"), row.get("workspace_id"), row.get("slug"), row.get("id")]
            for row in rows
        ],
    )


@app.command("create")
def create_project(
    ctx: typer.Context,
    name: str = typer.Option(...),
    slug: str = typer.Option(...),
    workspace: str | None = typer.Option(None, help="Workspace ID; defaults to active context."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create and select one Project in the chosen Workspace."""
    runtime: Runtime = ctx.obj
    workspace_id = workspace or runtime.config.active_workspace
    if not workspace_id:
        raise CLIError("Select a Workspace first with genaug workspace use.")
    token = installer_access_token(runtime)
    with runtime.client() as client:
        row = client.installer(
            "POST",
            "/projects",
            token=token,
            json={"name": name, "slug": slug, "workspace_id": workspace_id},
        )
    updated = runtime.config.model_copy(
        update={
            "active_workspace": str(row.get("workspace_id") or workspace_id),
            "active_project": str(row.get("id") or ""),
        }
    )
    save_config(updated, runtime.config_path)
    if json_output:
        print_json(row)
    else:
        print_success(f"Created and selected Project {row.get('name', name)}.")


@app.command("use")
def use_project(ctx: typer.Context, project: str) -> None:
    """Select the active Project and its owning Workspace."""
    runtime: Runtime = ctx.obj
    token = installer_access_token(runtime)
    with runtime.client() as client:
        payload = client.installer("GET", "/projects", token=token)
    row = _resolve(_items(payload), project)
    updated = runtime.config.model_copy(
        update={
            "active_workspace": str(row.get("workspace_id") or ""),
            "active_project": str(row["id"]),
        }
    )
    save_config(updated, runtime.config_path)
    print_success(f"Using Project {row.get('name', row['id'])}.")
