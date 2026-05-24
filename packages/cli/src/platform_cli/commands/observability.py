"""Observability and support-evidence commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Inspect traces and support evidence.")


@app.command("trace")
def get_trace(
    ctx: typer.Context,
    trace_id: Annotated[str, typer.Argument(help="Trace id returned by /v1/responses.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Fetch one project-scoped assistant trace."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/traces/"
                f"{encode_path_segment(trace_id)}"
            ),
        )
    if json_output:
        print_json(response)
        return
    table(
        "Trace",
        ["Field", "Value"],
        [
            ["Trace ID", _value(response, "trace_id") or trace_id],
            ["Response", _value(response, "id")],
            ["User", _value(response, "user_id")],
            ["Session", _value(response, "session_id")],
            ["Model", _value(response, "model_used")],
            ["Input Tokens", _value(response, "input_tokens")],
            ["Output Tokens", _value(response, "output_tokens")],
            ["Cost USD", _value(response, "cost_usd")],
            ["Latency MS", _value(response, "latency_ms")],
            ["Langfuse", _value(response, "langfuse_url")],
        ],
    )


@app.command("runs")
def list_runs(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    response_id: Annotated[
        str | None,
        typer.Option("--response-id", help="Filter by Responses API response id."),
    ] = None,
    trace_id: Annotated[
        str | None,
        typer.Option("--trace-id", help="Filter by General Augment trace id."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(help="Filter by durable run status."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Maximum runs to fetch.")] = 50,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List durable agent runs for one project."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/runs",
            params=_support_params(
                limit=limit,
                response_id=response_id,
                trace_id=trace_id,
                status=status,
            ),
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    table(
        "Agent runs",
        ["Run", "Status", "Response", "Trace", "Model", "Latency"],
        [
            [
                _value(item, "id"),
                _value(item, "status"),
                _value(item, "response_id"),
                _value(item, "trace_id"),
                _value(item, "model_used"),
                _value(item, "latency_ms"),
            ]
            for item in items
            if isinstance(item, dict)
        ],
    )


@app.command("run")
def get_run(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Durable General Augment run id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Fetch one durable agent run with step and event evidence."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/runs/"
                f"{encode_path_segment(run_id)}"
            ),
        )
    if json_output:
        print_json(response)
        return
    table(
        "Agent run",
        ["Field", "Value"],
        [
            ["Run", _value(response, "id") or run_id],
            ["Status", _value(response, "status")],
            ["Response", _value(response, "response_id")],
            ["Trace", _value(response, "trace_id")],
            ["Model", _value(response, "model_used")],
            ["User", _value(response, "user_id")],
            ["Session", _value(response, "session_id")],
            ["Steps", len(response.get("steps", [])) if isinstance(response, dict) else 0],
            [
                "Platform Events",
                len(response.get("platform_events", [])) if isinstance(response, dict) else 0,
            ],
        ],
    )


@app.command("support-bundle")
def support_bundle(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    trace_id: Annotated[
        str | None,
        typer.Option("--trace-id", help="Filter by General Augment trace id."),
    ] = None,
    response_id: Annotated[
        str | None,
        typer.Option("--response-id", help="Filter by Responses API response id."),
    ] = None,
    user_id: Annotated[
        str | None,
        typer.Option("--user-id", help="Filter by General Augment user id."),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Filter by General Augment session id."),
    ] = None,
    feature: Annotated[
        str | None,
        typer.Option(help="Filter by response metadata feature."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(help="Filter by response metadata source."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(help="Filter by success/failure status."),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(help="Filter start timestamp, ISO 8601."),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(help="Filter end timestamp, ISO 8601."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Maximum rows per section.")] = 50,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write support bundle JSON to a file."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Export a bounded project support bundle."""
    params = _support_params(
        limit=limit,
        trace_id=trace_id,
        response_id=response_id,
        user_id=user_id,
        session_id=session_id,
        feature=feature,
        source=source,
        status=status,
        start=start,
        end=end,
    )
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}"
                "/observability/support-bundle"
            ),
            params=params,
        )
    if output is not None:
        output.write_text(json.dumps(response, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print_success(f"Wrote support bundle to {output}.")
        return
    if json_output:
        print_json(response)
        return
    metrics = response.get("metrics", {}) if isinstance(response, dict) else {}
    table(
        "Support bundle",
        ["Field", "Value"],
        [
            ["Project", _value(response, "project_id")],
            ["Generated At", _value(response, "generated_at")],
            ["Traces", _metric(metrics, "trace_count")],
            ["Logs", _metric(metrics, "log_count")],
            ["Audit Events", _metric(metrics, "audit_event_count")],
            ["Memory Facts", _metric(metrics, "memory_fact_count")],
            ["Usage Events", _metric(metrics, "usage_event_count")],
            ["Timeline Events", _metric(metrics, "timeline_event_count")],
        ],
    )


def _support_params(**values: object) -> dict[str, object]:
    """Return support-bundle query params without unset filters."""
    return {key: value for key, value in values.items() if value is not None}


def _metric(payload: object, key: str) -> object:
    """Read a metric from the support-bundle metrics map."""
    if isinstance(payload, dict):
        return payload.get(key, 0)
    return 0


def _value(payload: object, key: str) -> object:
    """Safely read a value from a response mapping."""
    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
