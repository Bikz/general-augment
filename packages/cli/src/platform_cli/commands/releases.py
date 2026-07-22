"""Immutable Project release commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from platform_cli.errors import CLIError
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import installer_access_token

app = typer.Typer(help="Verify and promote immutable Project releases.")


def _project_id(runtime: Runtime) -> str:
    project_id = runtime.config.active_project
    if not project_id:
        raise CLIError("Select a Project first with genaug project use.")
    return project_id


def _call(
    runtime: Runtime,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
) -> object:
    with runtime.client() as client:
        return client.installer(
            method,
            path,
            token=installer_access_token(runtime),
            json=body,
        )


@app.command("list")
def list_releases(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List immutable releases in the active Project."""
    runtime: Runtime = ctx.obj
    payload = _call(runtime, "GET", f"/projects/{_project_id(runtime)}/releases")
    if json_output:
        print_json(payload)
        return
    rows = payload if isinstance(payload, list) else []
    table(
        "Project releases",
        ["Version", "Status", "Fingerprint", "ID"],
        [
            [row.get("version"), row.get("status"), row.get("fingerprint"), row.get("id")]
            for row in rows
            if isinstance(row, dict)
        ],
    )


@app.command("create")
def create_release(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Snapshot current Project draft state into an immutable candidate."""
    runtime: Runtime = ctx.obj
    payload = _call(runtime, "POST", f"/projects/{_project_id(runtime)}/releases")
    if json_output:
        print_json(payload)
    else:
        fingerprint = payload.get("fingerprint", "") if isinstance(payload, dict) else ""
        print_success(f"Created candidate release {fingerprint}.")


@app.command("verify")
def verify_release_command(
    ctx: typer.Context,
    release_id: str,
    evidence: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Attach a complete evidence artifact to one candidate release."""
    runtime: Runtime = ctx.obj
    parsed = json.loads(evidence.read_text(encoding="utf-8"))
    checks = parsed.get("checks") if isinstance(parsed, dict) else None
    if not isinstance(checks, list):
        raise CLIError("Verification evidence must contain a checks array.")
    project_id = _project_id(runtime)
    payload = _call(
        runtime,
        "POST",
        f"/projects/{project_id}/releases/{release_id}/verify",
        body={"checks": checks},
    )
    if json_output:
        print_json(payload)
    else:
        print_success("Release verification recorded.")


@app.command("promote")
def promote_release_command(
    ctx: typer.Context,
    release_id: str,
    mode: Literal["test", "live"] = typer.Option("test"),
    confirm_live: bool = typer.Option(False, "--confirm-live"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Promote the exact verified fingerprint to Test or explicitly confirmed Live."""
    if mode == "live" and not confirm_live:
        raise CLIError("Live promotion requires --confirm-live.")
    runtime: Runtime = ctx.obj
    project_id = _project_id(runtime)
    payload = _call(
        runtime,
        "POST",
        f"/projects/{project_id}/releases/{release_id}/promote",
        body={
            "runtime_mode": mode,
            "idempotency_key": f"cli-promote-{project_id}-{release_id}-{mode}",
        },
    )
    if json_output:
        print_json(payload)
    else:
        print_success(f"Promoted verified release to {mode.title()}.")


@app.command("rollback")
def rollback_release(
    ctx: typer.Context,
    mode: Literal["test", "live"] = typer.Option(...),
    confirm_live: bool = typer.Option(False, "--confirm-live"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Restore the release preceding the current Test or Live binding."""
    if mode == "live" and not confirm_live:
        raise CLIError("Live rollback requires --confirm-live.")
    runtime: Runtime = ctx.obj
    project_id = _project_id(runtime)
    deployments = _call(runtime, "GET", f"/projects/{project_id}/deployments")
    active_release_id = (
        next(
            (
                str(row.get("active_release_id"))
                for row in deployments
                if isinstance(row, dict) and row.get("runtime_mode") == mode
            ),
            None,
        )
        if isinstance(deployments, list)
        else None
    )
    if not active_release_id:
        raise CLIError(f"No active {mode.title()} deployment exists to roll back.")
    payload = _call(
        runtime,
        "POST",
        f"/projects/{project_id}/deployments/rollback",
        body={
            "runtime_mode": mode,
            "idempotency_key": f"cli-rollback-{project_id}-{mode}-{active_release_id}",
        },
    )
    if json_output:
        print_json(payload)
    else:
        print_success(f"Rolled back {mode.title()} deployment.")
