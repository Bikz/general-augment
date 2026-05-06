"""Project log streaming command."""

from __future__ import annotations

import time

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import table
from platform_cli.runtime import Runtime


def logs(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    follow: bool = typer.Option(False, "--follow", help="Poll continuously."),
    limit: int = typer.Option(25, min=1, max=200, help="Log row limit."),
) -> None:
    """Show recent project logs."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        while True:
            payload = client.admin(
                "GET",
                f"/projects/{encode_path_segment(str(project_payload['id']))}/logs",
                params={"limit": limit},
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            rows = [
                [
                    item.get("created_at", ""),
                    item.get("role", ""),
                    str(item.get("content", ""))[:80],
                ]
                for item in items
                if isinstance(item, dict)
            ]
            table("Logs", ["Time", "Role", "Content"], rows)
            if not follow:
                return
            time.sleep(2)
