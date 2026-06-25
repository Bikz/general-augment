"""Platform status command."""

from __future__ import annotations

import re
from typing import Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import panel, print_json, table
from platform_cli.runtime import Runtime

QUEUE_DEPTH_RE = re.compile(
    r"^general_augment_queue_depth(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)"
)
LABEL_RE = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')


def status(
    ctx: typer.Context,
    project: str | None = typer.Option(None, help="Optional project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show platform health, metrics reachability, and optional project usage."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        live = client.public("GET", "/health/live")
        ready = client.public("GET", "/health/ready")
        metrics = client.public("GET", "/metrics")
        queue_depths = _queue_depths(metrics)
        payload: dict[str, Any] = {
            "live": live,
            "ready": ready,
            "metrics": {
                "available": metrics is not None,
                "queue_depths": queue_depths,
            },
        }
        project_summary: dict[str, Any] | None = None
        if project:
            project_payload = resolve_project(client, project)
            usage = client.admin(
                "GET",
                f"/projects/{encode_path_segment(str(project_payload['id']))}/usage",
            )
            totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
            project_summary = {
                "id": project_payload.get("id"),
                "slug": project_payload.get("slug", project),
                "usage": {
                    "agent_turns_count": totals.get("agent_turns_count", 0),
                    "messages_count": totals.get("messages_count", 0),
                    "tool_calls_count": totals.get("tool_calls_count", 0),
                    "total_cost_usd": totals.get("total_cost_usd", 0),
                },
            }
            payload["project"] = project_summary
        if json_output:
            print_json(payload)
            return
        rows: list[list[object]] = [
            ["Live", _status_text(live)],
            ["Ready", _status_text(ready)],
            ["Metrics", "available" if metrics is not None else "empty"],
        ]
        table("Platform Status", ["Check", "State"], rows)
        if queue_depths:
            table(
                "Queue Depth",
                ["Queue", "Depth"],
                [[item["queue"], item["depth"]] for item in queue_depths],
            )
        if project:
            assert project_summary is not None
            usage_summary = project_summary["usage"]
            panel(
                f"Project {project_summary.get('slug', project)}",
                f"Agent turns: {usage_summary.get('agent_turns_count', 0)}\n"
                f"Stored messages: {usage_summary.get('messages_count', 0)}\n"
                f"Tool calls: {usage_summary.get('tool_calls_count', 0)}\n"
                f"Cost: {usage_summary.get('total_cost_usd', 0)}",
            )


def _status_text(payload: object) -> str:
    """Return a compact status string from health JSON."""
    if isinstance(payload, dict):
        return str(payload.get("status") or payload)
    return str(payload)


def _queue_depths(metrics: object) -> list[dict[str, str | int | float]]:
    """Parse queue depth gauges from a Prometheus text payload."""
    if not isinstance(metrics, str):
        return []
    queue_depths: list[dict[str, str | int | float]] = []
    for line in metrics.splitlines():
        if not line or line.startswith("#"):
            continue
        match = QUEUE_DEPTH_RE.match(line.strip())
        if match is None:
            continue
        labels = _prometheus_labels(match.group("labels") or "")
        queue = labels.get("queue", "default")
        try:
            raw_depth = float(match.group("value"))
        except ValueError:
            continue
        depth: int | float = int(raw_depth) if raw_depth.is_integer() else raw_depth
        queue_depths.append({"queue": queue, "depth": depth})
    return sorted(queue_depths, key=lambda item: str(item["queue"]))


def _prometheus_labels(raw_labels: str) -> dict[str, str]:
    """Parse simple Prometheus label strings."""
    return {
        match.group("key"): match.group("value").replace(r"\"", '"').replace(r"\\", "\\")
        for match in LABEL_RE.finditer(raw_labels)
    }
