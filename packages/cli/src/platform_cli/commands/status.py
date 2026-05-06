"""Platform status command."""

from __future__ import annotations

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import panel, table
from platform_cli.runtime import Runtime


def status(
    ctx: typer.Context,
    project: str | None = typer.Option(None, help="Optional project id, slug, or name."),
) -> None:
    """Show platform health, metrics reachability, and optional project usage."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        live = client.public("GET", "/health/live")
        ready = client.public("GET", "/health/ready")
        metrics = client.public("GET", "/metrics")
        rows: list[list[object]] = [
            ["Live", _status_text(live)],
            ["Ready", _status_text(ready)],
            ["Metrics", "available" if metrics is not None else "empty"],
        ]
        table("Platform Status", ["Check", "State"], rows)
        if project:
            project_payload = resolve_project(client, project)
            usage = client.admin(
                "GET",
                f"/projects/{encode_path_segment(str(project_payload['id']))}/usage",
            )
            totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
            agent_turns = totals.get("agent_turns_count", 0)
            panel(
                f"Project {project_payload.get('slug', project)}",
                f"Agent turns: {agent_turns}\n"
                f"Stored messages: {totals.get('messages_count', 0)}\n"
                f"Tool calls: {totals.get('tool_calls_count', 0)}\n"
                f"Cost: {totals.get('total_cost_usd', 0)}",
            )


def _status_text(payload: object) -> str:
    """Return a compact status string from health JSON."""
    if isinstance(payload, dict):
        return str(payload.get("status") or payload)
    return str(payload)
