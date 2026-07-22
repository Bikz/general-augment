"""Read-only Project Agent inspection commands.

Agent mutations are intentionally applied through the declarative launch contract so
the server can bind every consequential change to an exact reviewed fingerprint.
"""

from __future__ import annotations

import typer

from platform_cli.errors import CLIError
from platform_cli.output import print_json, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import installer_access_token

app = typer.Typer(help="Inspect Agents in the active Project.")
delegation_app = typer.Typer(help="Inspect governed relationships between peer Agents.")
app.add_typer(delegation_app, name="delegation")

_CONFIGURE_COMMAND = "genaug launch --activate --auto-approve-safe --json"


def _project_id(runtime: Runtime) -> str:
    project_id = runtime.config.active_project
    if not project_id:
        raise CLIError("Select a Project first with genaug project use.")
    return project_id


def _items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, list):
        return []
    return [dict(row) for row in payload if isinstance(row, dict)]


def _load_agents(runtime: Runtime, project_id: str) -> list[dict[str, object]]:
    with runtime.client() as client:
        payload = client.installer(
            "GET",
            f"/projects/{project_id}/agents",
            token=installer_access_token(runtime),
        )
    return _items(payload)


@app.command("list")
def list_agents(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List Agents in the active Project without changing configuration."""
    runtime: Runtime = ctx.obj
    rows = _load_agents(runtime, _project_id(runtime))
    if json_output:
        print_json(rows)
        return
    table(
        "Agents",
        ["Name", "Slug", "Entry", "Status", "ID"],
        [
            [
                row.get("name"),
                row.get("slug"),
                "yes" if row.get("is_entry") else "no",
                row.get("status"),
                row.get("id"),
            ]
            for row in rows
        ],
    )


@app.command("show")
def show_agent(
    ctx: typer.Context,
    agent: str = typer.Argument(..., help="Agent id, slug, or exact name."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show one Agent and its current Project-owned resource assignments."""
    runtime: Runtime = ctx.obj
    rows = _load_agents(runtime, _project_id(runtime))
    matches = [
        row
        for row in rows
        if agent
        in {
            str(row.get("id") or ""),
            str(row.get("slug") or ""),
            str(row.get("name") or ""),
        }
    ]
    if len(matches) != 1:
        raise CLIError("Agent reference did not match exactly one Agent in the active Project.")
    if json_output:
        print_json(matches[0])
        return
    row = matches[0]
    table(
        "Agent",
        ["Field", "Value"],
        [
            ["Name", row.get("name")],
            ["Slug", row.get("slug")],
            ["ID", row.get("id")],
            ["Entry", "yes" if row.get("is_entry") else "no"],
            ["Status", row.get("status")],
        ],
    )


@app.command("status")
def agent_status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explain the authoritative Agent configuration path for this Project."""
    runtime: Runtime = ctx.obj
    project_id = _project_id(runtime)
    rows = _load_agents(runtime, project_id)
    payload = {
        "project_id": project_id,
        "agent_count": len(rows),
        "entry_agent_ids": [str(row.get("id")) for row in rows if row.get("is_entry")],
        "configuration_source": "genaug-agent.yaml",
        "mutation_mode": "declarative_launch",
        "approval_enforcement": "server_fingerprint",
        "next": _CONFIGURE_COMMAND,
    }
    if json_output:
        print_json(payload)
        return
    table(
        "Agent configuration",
        ["Field", "Value"],
        [
            ["Project", project_id],
            ["Agents", len(rows)],
            ["Source", "genaug-agent.yaml"],
            ["Apply", _CONFIGURE_COMMAND],
        ],
    )


@delegation_app.command("list")
def list_delegations(
    ctx: typer.Context,
    agent_id: str = typer.Option(..., "--from"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List one Agent's outbound delegation graph without changing it."""
    runtime: Runtime = ctx.obj
    project_id = _project_id(runtime)
    with runtime.client() as client:
        payload = client.installer(
            "GET",
            f"/projects/{project_id}/agents/{agent_id}/delegations",
            token=installer_access_token(runtime),
        )
    if json_output:
        print_json(payload)
        return
    table(
        "Agent delegations",
        ["From", "To", "Mode", "ID"],
        [
            [
                row.get("source_agent_id"),
                row.get("destination_agent_id"),
                row.get("mode"),
                row.get("id"),
            ]
            for row in _items(payload)
        ],
    )
