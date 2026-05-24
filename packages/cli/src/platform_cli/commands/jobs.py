"""Scheduled job lifecycle commands."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage project scheduled jobs.")


@app.command("create")
def create_job(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    target_app_user_id: Annotated[
        str,
        typer.Option("--target-app-user-id", help="App-owned user id to run the job for."),
    ],
    prompt: Annotated[str, typer.Option(help="Prompt to send through the Hermes turn path.")],
    name: Annotated[str | None, typer.Option(help="Human-readable job name.")] = None,
    target_channel: Annotated[
        str | None,
        typer.Option("--target-channel", help="Optional app/channel label."),
    ] = None,
    interval_seconds: Annotated[
        int | None,
        typer.Option("--interval-seconds", min=1, help="Run on a fixed interval."),
    ] = None,
    cron: Annotated[str | None, typer.Option("--cron", help="Cron expression for the job.")] = None,
    timezone: Annotated[str, typer.Option(help="Timezone for --cron schedules.")] = "UTC",
    run_at: Annotated[
        str | None,
        typer.Option("--run-at", help="ISO 8601 timestamp for a one-time job."),
    ] = None,
    schedule_json: Annotated[
        str | None,
        typer.Option("--schedule-json", help="Full schedule object as JSON."),
    ] = None,
    instructions: Annotated[
        str | None,
        typer.Option(help="Optional additional instructions."),
    ] = None,
    context_json: Annotated[
        str | None,
        typer.Option("--context-json", help="Context object as JSON."),
    ] = None,
    retry_policy_json: Annotated[
        str | None,
        typer.Option("--retry-policy-json", help="Retry policy object as JSON."),
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Allowed tool id. Repeatable."),
    ] = None,
    max_tokens: Annotated[int, typer.Option(min=1, help="Maximum output tokens.")] = 2000,
    response_format: Annotated[
        str,
        typer.Option(help="Response format: text or json."),
    ] = "text",
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create a generic Hermes-backed scheduled job."""
    payload = {
        "name": name,
        "target_app_user_id": target_app_user_id,
        "target_channel": target_channel,
        "prompt": prompt,
        "instructions": instructions,
        "context": _json_object(context_json, option="--context-json"),
        "schedule": _schedule(
            schedule_json=schedule_json,
            interval_seconds=interval_seconds,
            cron=cron,
            timezone=timezone,
            run_at=run_at,
        ),
        "tools": tool,
        "retry_policy": _json_object(retry_policy_json, option="--retry-policy-json"),
        "max_tokens": max_tokens,
        "response_format": response_format,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin("POST", _jobs_path(project_payload), json=payload)
    if json_output:
        print_json(response)
        return
    print_success(f"Scheduled job {_value(response, 'id')} created.")


@app.command("list")
def list_jobs(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    status: Annotated[str | None, typer.Option(help="Filter by job status.")] = None,
    target_app_user_id: Annotated[
        str | None,
        typer.Option("--target-app-user-id", help="Filter by app-owned user id."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Maximum jobs to fetch.")] = 50,
    offset: Annotated[int, typer.Option(min=0, help="Pagination offset.")] = 0,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List scheduled jobs for one project."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            _jobs_path(project_payload),
            params=_params(
                status=status,
                target_app_user_id=target_app_user_id,
                limit=limit,
                offset=offset if offset else None,
            ),
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    table("Scheduled jobs", ["Job", "Status", "User", "Next Run", "Latest Trace"], _job_rows(items))


@app.command("detail")
def get_job(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Scheduled job id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Fetch one scheduled job with recent execution history."""
    response = _job_request(ctx, project, job_id, "GET")
    if json_output:
        print_json(response)
        return
    linked_run_ids = ""
    if isinstance(response, dict):
        linked_run_ids = ", ".join(str(item) for item in response.get("linked_run_ids", []))
    table(
        "Scheduled job",
        ["Field", "Value"],
        [
            ["Job", _value(response, "id") or job_id],
            ["Name", _value(response, "name")],
            ["Status", _value(response, "status")],
            ["Target User", _value(response, "target_app_user_id")],
            ["Target Channel", _value(response, "target_channel")],
            ["Next Run", _value(response, "next_run_at")],
            ["Last Run", _value(response, "last_run_at")],
            ["Latest Trace", _value(response, "latest_trace_id")],
            ["Linked Runs", linked_run_ids],
            ["Terminal Reason", _value(response, "terminal_reason")],
        ],
    )


@app.command("runs")
def list_job_runs(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Scheduled job id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    limit: Annotated[int, typer.Option(min=1, max=100, help="Maximum runs to fetch.")] = 20,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List execution history for one scheduled job."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"{_job_path(project_payload, job_id)}/runs",
            params={"limit": limit},
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    table("Scheduled job runs", ["Run", "Status", "Attempts", "Trace", "Reason"], _run_rows(items))


@app.command("run")
def dispatch_job(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Scheduled job id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    dispatch_key: Annotated[
        str | None,
        typer.Option("--dispatch-key", help="Idempotency key for the forced dispatch."),
    ] = None,
    record_only: Annotated[
        bool,
        typer.Option("--record-only", help="Create the dispatch record without executing Hermes."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Force-dispatch one scheduled job for validation."""
    response = _job_request(
        ctx,
        project,
        job_id,
        "POST",
        suffix="/dispatch",
        json={"dispatch_key": dispatch_key, "execute": not record_only},
    )
    if json_output:
        print_json(response)
        return
    run = response.get("run", {}) if isinstance(response, dict) else {}
    print_success(f"Scheduled job {job_id} dispatched as run {_value(run, 'id')}.")


@app.command("pause")
def pause_job(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Scheduled job id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Pause one scheduled job."""
    response = _job_request(ctx, project, job_id, "POST", suffix="/pause")
    if json_output:
        print_json(response)
        return
    print_success(f"Scheduled job {job_id} paused.")


@app.command("resume")
def resume_job(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Scheduled job id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Resume one scheduled job."""
    response = _job_request(ctx, project, job_id, "POST", suffix="/resume")
    if json_output:
        print_json(response)
        return
    print_success(f"Scheduled job {job_id} resumed.")


@app.command("delete")
def delete_job(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Scheduled job id.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm cancelling this scheduled job."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Cancel one scheduled job while preserving its run history."""
    if not yes and not typer.confirm(f"Cancel scheduled job {job_id}?"):
        raise typer.Exit(1)
    response = _job_request(ctx, project, job_id, "DELETE")
    if json_output:
        print_json(response)
        return
    print_success(f"Scheduled job {job_id} cancelled.")


def _job_request(
    ctx: typer.Context,
    project: str,
    job_id: str,
    method: str,
    *,
    suffix: str = "",
    json: dict[str, Any] | None = None,
) -> Any:
    """Issue one project-scoped scheduled-job request."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        return client.admin(method, f"{_job_path(project_payload, job_id)}{suffix}", json=json)


def _jobs_path(project_payload: dict[str, Any]) -> str:
    """Return the scheduled-jobs collection path for a project."""
    return f"/projects/{encode_path_segment(str(project_payload['id']))}/scheduled-jobs"


def _job_path(project_payload: dict[str, Any], job_id: str) -> str:
    """Return a scheduled-job item path."""
    return f"{_jobs_path(project_payload)}/{encode_path_segment(job_id)}"


def _schedule(
    *,
    schedule_json: str | None,
    interval_seconds: int | None,
    cron: str | None,
    timezone: str,
    run_at: str | None,
) -> dict[str, Any]:
    """Return a scheduled-job schedule object from CLI flags."""
    if schedule_json:
        return _json_object(schedule_json, option="--schedule-json")
    selected = [interval_seconds is not None, bool(cron), bool(run_at)]
    if sum(1 for value in selected if value) != 1:
        raise typer.BadParameter(
            "Provide exactly one of --interval-seconds, --cron, --run-at, or --schedule-json."
        )
    if interval_seconds is not None:
        return {"type": "interval", "every_seconds": interval_seconds}
    if cron:
        return {"type": "cron", "expression": cron, "timezone": timezone}
    return {"type": "once", "at": run_at}


def _json_object(value: str | None, *, option: str) -> dict[str, Any]:
    """Parse an optional JSON object flag."""
    if value is None:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{option} must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{option} must be a JSON object.")
    return payload


def _params(**values: object | None) -> dict[str, object]:
    """Return query params with omitted values removed."""
    return {key: value for key, value in values.items() if value is not None}


def _job_rows(items: object) -> list[list[object]]:
    """Return table rows for scheduled jobs."""
    if not isinstance(items, list):
        return []
    return [
        [
            _value(item, "id"),
            _value(item, "status"),
            _value(item, "target_app_user_id"),
            _value(item, "next_run_at"),
            _value(item, "latest_trace_id"),
        ]
        for item in items
        if isinstance(item, dict)
    ]


def _run_rows(items: object) -> list[list[object]]:
    """Return table rows for scheduled job runs."""
    if not isinstance(items, list):
        return []
    return [
        [
            _value(item, "id"),
            _value(item, "status"),
            _value(item, "attempt_count"),
            _value(item, "trace_id"),
            _value(item, "terminal_reason"),
        ]
        for item in items
        if isinstance(item, dict)
    ]


def _value(payload: object, key: str) -> object:
    """Safely read a display value from a JSON object."""
    if not isinstance(payload, dict):
        return ""
    value = payload.get(key)
    return "" if value is None else value
