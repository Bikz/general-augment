"""Tenant user management commands."""

from __future__ import annotations

from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage tenant users.")


@app.command("list")
def list_users(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    page: Annotated[int, typer.Option(min=1, help="Page number.")] = 1,
    page_size: Annotated[
        int,
        typer.Option("--page-size", min=1, max=500, help="Users per page."),
    ] = 50,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List users for one project."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/users",
            params={"page": page, "page_size": page_size},
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    rows = [
        [
            item.get("id", ""),
            item.get("phone_e164", ""),
            item.get("display_name", ""),
            item.get("message_count", 0),
            item.get("last_active_at", ""),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    table("Users", ["ID", "Phone", "Name", "Messages", "Last Active"], rows)


@app.command("detail")
def user_detail(
    ctx: typer.Context,
    user_id: Annotated[str, typer.Argument(help="General Augment user id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Show one tenant user's memory and credential summary."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/users/"
                f"{encode_path_segment(user_id)}"
            ),
        )
    if json_output:
        print_json(response)
        return
    user = response.get("user", {}) if isinstance(response, dict) else {}
    memory_facts = response.get("memory_facts", []) if isinstance(response, dict) else []
    credentials = response.get("credentials", []) if isinstance(response, dict) else []
    table(
        "User detail",
        ["Field", "Value"],
        [
            ["User ID", _value(user, "id")],
            ["Phone", _value(user, "phone_e164")],
            ["Display Name", _value(user, "display_name")],
            ["Messages", _value(response, "message_count")],
            ["Memory Facts", len(memory_facts) if isinstance(memory_facts, list) else 0],
            ["Credentials", _credential_summary(credentials)],
        ],
    )


@app.command("delete")
def delete_user(
    ctx: typer.Context,
    user_id: Annotated[str, typer.Argument(help="General Augment user id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm deleting this user and cascaded tenant data."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Delete one tenant user and cascaded tenant data."""
    if not yes and not typer.confirm(f"Delete user {user_id} and cascaded tenant data?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "DELETE",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/users/"
                f"{encode_path_segment(user_id)}"
            ),
        )
    if json_output:
        print_json(response)
        return
    deleted_user_id = response.get("user_id", user_id) if isinstance(response, dict) else user_id
    print_success(f"Deleted user {deleted_user_id}.")


def _credential_summary(value: object) -> str:
    """Return a compact credential summary without secret values."""
    if not isinstance(value, list) or not value:
        return "none"
    labels: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        provider = item.get("provider", "unknown")
        status = item.get("status", "unknown")
        labels.append(f"{provider}:{status}")
    return ", ".join(labels) if labels else "none"


def _value(payload: object, key: str) -> object:
    """Safely read a value from a response mapping."""
    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
