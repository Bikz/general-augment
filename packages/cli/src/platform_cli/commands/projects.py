"""Project management commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage projects.")


@app.command("list")
def list_projects(ctx: typer.Context) -> None:
    """List visible projects."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        payload = client.admin("GET", "/projects")
    items = payload.get("items", []) if isinstance(payload, dict) else []
    rows = [
        [item.get("name", ""), item.get("slug", ""), item.get("status", ""), item.get("id", "")]
        for item in items
        if isinstance(item, dict)
    ]
    table("Projects", ["Name", "Slug", "Status", "ID"], rows)


@app.command("create")
def create_project(
    ctx: typer.Context,
    name: str = typer.Option(..., help="Project display name."),
    slug: str = typer.Option(..., help="Project slug."),
    system_prompt: str = typer.Option("You are a helpful agent.", help="Initial system prompt."),
) -> None:
    """Create a project."""
    runtime: Runtime = ctx.obj
    payload = {
        "name": name,
        "slug": slug,
        "system_prompt": system_prompt,
    }
    with runtime.client() as client:
        project = client.admin("POST", "/projects", json=payload)
    print_success(f"Created project {project.get('name', name)} ({project.get('id', 'unknown')}).")


@app.command("usage")
def project_usage(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    start_date: str | None = typer.Option(None, help="Inclusive start date, YYYY-MM-DD."),
    end_date: str | None = typer.Option(None, help="Inclusive end date, YYYY-MM-DD."),
) -> None:
    """Show project usage and billing aggregates."""
    runtime: Runtime = ctx.obj
    params = {
        key: value
        for key, value in {"start_date": start_date, "end_date": end_date}.items()
        if value is not None
    }
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        usage = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/usage",
            params=params,
        )
    totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
    rows: list[list[object]] = [
        ["Agent turns", _metric(totals, "agent_turns_count")],
        ["Stored messages", _metric(totals, "messages_count")],
        ["Tool calls", _metric(totals, "tool_calls_count")],
        ["Cost USD", _metric(totals, "total_cost_usd")],
    ]
    table(f"Usage for {project_payload.get('slug', project)}", ["Metric", "Value"], rows)

    days = usage.get("days", []) if isinstance(usage, dict) else []
    day_rows = [
        [
            item.get("date", item.get("day", "")),
            _metric(item, "agent_turns_count"),
            _metric(item, "tool_calls_count"),
            _metric(item, "total_cost_usd"),
        ]
        for item in days
        if isinstance(item, dict)
    ]
    if day_rows:
        table("Daily Usage", ["Date", "Agent turns", "Tool calls", "Cost USD"], day_rows)


@app.command("runtime-policy")
def project_runtime_policy(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show the tenant-governed Hermes runtime policy."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        policy = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/runtime-policy",
        )
    if json_output:
        print_json(policy)
        return
    routing = policy.get("model_routing", {}) if isinstance(policy, dict) else {}
    tiers = routing.get("tiers", {}) if isinstance(routing, dict) else {}
    table(
        f"Runtime Policy for {project_payload.get('slug', project)}",
        ["Surface", "Value"],
        [
            ["Model routing", _model_routing_summary(routing)],
            ["Simple model", _field(tiers, "simple")],
            ["Balanced model", _field(tiers, "balanced")],
            ["Complex model", _field(tiers, "complex")],
            ["Tool discovery", _nested_metric(policy, "tool_discovery", "mode")],
            ["Enabled platform tools", _joined(policy, "platform_tools", "enabled_tool_ids")],
            ["MCP tools", _joined(policy, "mcp", "enabled_tool_ids")],
            ["Skills", _joined(policy, "skills", "names")],
        ],
    )


@app.command("export")
def export_project(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    include: Annotated[
        list[str] | None,
        typer.Option("--include", help="Export section to include. Repeatable."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=500, help="Maximum rows per section.")] = 100,
    user_id: Annotated[str | None, typer.Option("--user-id", help="Filter by user id.")] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Filter by session id."),
    ] = None,
    trace_id: Annotated[str | None, typer.Option("--trace-id", help="Filter by trace id.")] = None,
    start: Annotated[str | None, typer.Option(help="Filter start timestamp, ISO 8601.")] = None,
    end: Annotated[str | None, typer.Option(help="Filter end timestamp, ISO 8601.")] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write export JSON to a file."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Export bounded project data for operator review."""
    runtime: Runtime = ctx.obj
    params = _export_params(
        include=include,
        limit=limit,
        user_id=user_id,
        session_id=session_id,
        trace_id=trace_id,
        start=start,
        end=end,
    )
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/export",
            params=params,
        )
    if output is not None:
        output.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print_success(f"Wrote project export to {output}.")
        return
    if json_output:
        print_json(response)
        return
    filters = response.get("filters", {}) if isinstance(response, dict) else {}
    include_sections = filters.get("include", include or []) if isinstance(filters, dict) else []
    table(
        "Project export",
        ["Field", "Value"],
        [
            ["Project", _value(response, "project_id")],
            ["Exported At", _value(response, "exported_at")],
            ["Sections", ", ".join(str(item) for item in include_sections)],
            ["Logs", _section_count(response, "logs")],
            ["Traces", _section_count(response, "traces")],
            ["Audit Events", _section_count(response, "audit_events")],
            ["Memory Facts", _section_count(response, "memory_facts")],
            ["Usage Events", _section_count(response, "usage_events")],
        ],
    )


@app.command("archive")
def archive_project(
    ctx: typer.Context,
    project: Annotated[str, typer.Argument(help="Project id, slug, or name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm archiving this project."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Archive one project."""
    if not yes and not typer.confirm(f"Archive project {project}?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/archive",
        )
    if json_output:
        print_json(response)
        return
    archived_project = response.get("slug", project) if isinstance(response, dict) else project
    print_success(f"Archived project {archived_project}.")


def _metric(payload: object, *keys: str) -> object:
    """Return the first present metric value from a usage payload."""
    if not isinstance(payload, dict):
        return 0
    for key in keys:
        if key in payload:
            return payload[key]
    return 0


def _nested_metric(payload: object, section: str, key: str) -> object:
    """Return a nested field value from a JSON object."""

    if not isinstance(payload, dict):
        return ""
    nested = payload.get(section)
    if not isinstance(nested, dict):
        return ""
    return nested.get(key, "")


def _field(payload: object, key: str) -> object:
    """Return a field value from a JSON object."""

    if not isinstance(payload, dict):
        return ""
    return payload.get(key, "")


def _joined(payload: object, section: str, key: str) -> str:
    """Return a compact joined list from a nested JSON object."""

    if not isinstance(payload, dict):
        return ""
    nested = payload.get(section)
    if not isinstance(nested, dict):
        return ""
    values = nested.get(key)
    if not isinstance(values, list):
        return ""
    return ", ".join(str(item) for item in values) or "none"


def _model_routing_summary(payload: object) -> str:
    """Return a compact model-routing mode summary."""

    if not isinstance(payload, dict):
        return ""
    mode = str(payload.get("mode") or "")
    default_tier = str(payload.get("default_tier") or "")
    parity = payload.get("channel_parity")
    return f"{mode}, default={default_tier}, channel_parity={parity}"


def _export_params(**values: object) -> dict[str, object]:
    """Return project export query params without unset filters."""
    return {key: value for key, value in values.items() if value is not None}


def _section_count(payload: object, key: str) -> int:
    """Return the length of a list section in a project export."""
    if not isinstance(payload, dict):
        return 0
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _value(payload: object, key: str) -> object:
    """Safely read a value from a response mapping."""
    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
