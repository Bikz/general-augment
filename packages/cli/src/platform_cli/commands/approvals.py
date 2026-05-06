"""Approval queue management commands."""

from __future__ import annotations

from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage governed tool approvals.")
APPROVAL_STATUSES = {"pending", "all"}


@app.command("list")
def list_approvals(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    status: Annotated[
        str,
        typer.Option(help="Approval rows to list: pending or all."),
    ] = "pending",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List approval rows for one project."""
    normalized_status = _approval_status(status)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/approvals",
            params={"status": normalized_status},
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    rows = [_approval_row(item) for item in items if isinstance(item, dict)]
    table(
        "Approvals",
        ["Approval ID", "Tool", "Status", "Action", "Channel", "Expires At"],
        rows,
    )


@app.command("approve")
def approve_approval(
    ctx: typer.Context,
    approval_id: Annotated[str, typer.Argument(help="Approval id to approve.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm approving and resuming this governed action."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Approve one pending governed tool action."""
    _resolve_approval(
        ctx,
        approval_id,
        project=project,
        action="approve",
        yes=yes,
        json_output=json_output,
    )


@app.command("deny")
def deny_approval(
    ctx: typer.Context,
    approval_id: Annotated[str, typer.Argument(help="Approval id to deny.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm denying and resuming this governed action."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Deny one pending governed tool action."""
    _resolve_approval(
        ctx,
        approval_id,
        project=project,
        action="deny",
        yes=yes,
        json_output=json_output,
    )


def _resolve_approval(
    ctx: typer.Context,
    approval_id: str,
    *,
    project: str,
    action: str,
    yes: bool,
    json_output: bool,
) -> None:
    """Approve or deny one approval row."""
    if not yes and not typer.confirm(f"{action.title()} approval {approval_id}?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/approvals/"
                f"{encode_path_segment(approval_id)}/{action}"
            ),
        )
    if json_output:
        print_json(response)
        return
    approval = response.get("approval", {}) if isinstance(response, dict) else {}
    enqueued = response.get("enqueued", False) if isinstance(response, dict) else False
    status = approval.get("status", action + "d") if isinstance(approval, dict) else action + "d"
    print_success(f"Approval {approval_id} {status}; enqueued={enqueued}.")


def _approval_row(approval: dict[str, object]) -> list[object]:
    """Return a compact approval row."""
    return [
        approval.get("approval_id", ""),
        approval.get("tool_id", ""),
        approval.get("status", ""),
        approval.get("action_summary", ""),
        approval.get("channel", ""),
        approval.get("expires_at", ""),
    ]


def _approval_status(value: str) -> str:
    """Validate approval list status."""
    normalized = value.strip().lower()
    if normalized not in APPROVAL_STATUSES:
        raise typer.BadParameter("--status must be one of: all, pending.")
    return normalized
