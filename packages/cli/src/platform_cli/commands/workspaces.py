"""Workspace context and creation commands."""

from __future__ import annotations

import typer

from platform_cli.config import save_config
from platform_cli.errors import CLIError
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import installer_access_token

app = typer.Typer(help="Manage top-level General Augment Workspaces.")


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
        raise CLIError("Workspace reference must match exactly one visible Workspace.")
    return matches[0]


@app.command("list")
def list_workspaces(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List Workspaces visible to the signed-in account."""
    runtime: Runtime = ctx.obj
    token = installer_access_token(runtime)
    with runtime.client() as client:
        payload = client.installer("GET", "/workspaces", token=token)
    if json_output:
        print_json(payload)
        return
    rows = _items(payload)
    table(
        "Workspaces",
        ["Name", "Kind", "Role", "Slug", "ID"],
        [
            [row.get("name"), row.get("kind"), row.get("role"), row.get("slug"), row.get("id")]
            for row in rows
        ],
    )


@app.command("create")
def create_workspace(
    ctx: typer.Context,
    name: str = typer.Option(...),
    slug: str = typer.Option(...),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create and select one shared Workspace."""
    runtime: Runtime = ctx.obj
    token = installer_access_token(runtime)
    with runtime.client() as client:
        row = client.installer(
            "POST", "/workspaces", token=token, json={"name": name, "slug": slug}
        )
    workspace_id = str(row.get("id") or "")
    updated = runtime.config.model_copy(
        update={"active_workspace": workspace_id, "active_project": None}
    )
    save_config(updated, runtime.config_path)
    if json_output:
        print_json(row)
    else:
        print_success(f"Created and selected Workspace {row.get('name', name)}.")


@app.command("use")
def use_workspace(ctx: typer.Context, workspace: str) -> None:
    """Select the active Workspace and clear any stale Project selection."""
    runtime: Runtime = ctx.obj
    token = installer_access_token(runtime)
    with runtime.client() as client:
        payload = client.installer("GET", "/workspaces", token=token)
    row = _resolve(_items(payload), workspace)
    updated = runtime.config.model_copy(
        update={"active_workspace": str(row["id"]), "active_project": None}
    )
    save_config(updated, runtime.config_path)
    print_success(f"Using Workspace {row.get('name', row['id'])}.")
