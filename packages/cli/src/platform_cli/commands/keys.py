"""API key management commands."""

from __future__ import annotations

from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import panel, print_success, print_warning, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage project-scoped API keys.")


@app.command("list")
def list_keys(ctx: typer.Context) -> None:
    """List masked API keys visible to this management credential."""

    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        payload = client.admin("GET", "/keys")
    items = payload.get("items", []) if isinstance(payload, dict) else []
    rows = [
        [
            item.get("name", ""),
            item.get("masked_key", ""),
            item.get("project_id", ""),
            ",".join(item.get("scopes", [])),
            item.get("expires_at", "") or "",
            item.get("id", ""),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    table("API Keys", ["Name", "Key", "Project", "Scopes", "Expires", "ID"], rows)


@app.command("create")
def create_key(
    ctx: typer.Context,
    name: str = typer.Option(..., help="Display name, for example Production backend."),
    project: str | None = typer.Option(None, help="Project id, slug, or name."),
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="Scope to grant; repeatable."),
    ] = None,
    expires_at: str | None = typer.Option(None, help="Optional ISO-8601 expiration timestamp."),
) -> None:
    """Create an API key and print the raw secret once."""

    runtime: Runtime = ctx.obj
    payload: dict[str, object] = {
        "name": name,
        "scopes": scope or ["admin"],
    }
    with runtime.client() as client:
        if project:
            project_payload = resolve_project(client, project)
            payload["project_id"] = str(project_payload["id"])
        if expires_at is not None:
            payload["expires_at"] = expires_at
        response = client.admin("POST", "/keys", json=payload)
    print_success(
        f"Created API key {response.get('name', name)} ({response.get('id', 'unknown')})."
    )
    print_warning("The raw API key is shown once. Store it in your backend secret manager.")
    panel(
        "API Key",
        f"api_key: {response.get('api_key', '')}\n"
        f"masked_key: {response.get('masked_key', '')}\n"
        f"project_id: {response.get('project_id', payload.get('project_id', ''))}\n"
        f"scopes: {','.join(response.get('scopes', []))}",
    )


@app.command("update")
def update_key(
    ctx: typer.Context,
    key_id: str = typer.Argument(..., help="API key id."),
    name: str | None = typer.Option(None, help="New display name."),
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="Replacement scope; repeatable."),
    ] = None,
    expires_at: str | None = typer.Option(None, help="Replacement ISO-8601 expiration timestamp."),
    clear_expiration: bool = typer.Option(False, help="Clear the key expiration."),
) -> None:
    """Update API key metadata without showing the raw secret."""

    runtime: Runtime = ctx.obj
    payload: dict[str, object | None] = {}
    if name is not None:
        payload["name"] = name
    if scope is not None:
        payload["scopes"] = scope
    if clear_expiration:
        payload["expires_at"] = None
    elif expires_at is not None:
        payload["expires_at"] = expires_at
    with runtime.client() as client:
        response = client.admin("PATCH", f"/keys/{encode_path_segment(key_id)}", json=payload)
    print_success(f"Updated API key {response.get('name', key_id)}.")


@app.command("revoke")
def revoke_key(
    ctx: typer.Context,
    key_id: str = typer.Argument(..., help="API key id."),
) -> None:
    """Revoke an API key."""

    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        response = client.admin("DELETE", f"/keys/{encode_path_segment(key_id)}")
    print_success(f"Revoked API key {response.get('id', key_id)}.")
