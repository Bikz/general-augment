"""Install the official launch skill for Codex and Claude Code."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer

from platform_cli.errors import CLIError
from platform_cli.output import print_json, table
from platform_cli.skill_distribution import (
    LAUNCH_SKILL_VERSION,
    bundled_launch_skill,
    install_launch_skill,
    launch_skill_status,
    remove_launch_skill,
)

app = typer.Typer(help="Install the versioned General Augment launch skill.")
AgentOption = Literal["codex", "claude", "all"]
ScopeOption = Literal["project", "user"]


@app.command("install")
def install(
    agent: Annotated[AgentOption, typer.Option(help="Coding-agent installation target.")] = "all",
    scope: Annotated[
        ScopeOption, typer.Option(help="Install for this project or user.")
    ] = "project",
    workspace: Annotated[
        Path, typer.Option(help="Project workspace for project scope.")
    ] = Path("."),
    version: Annotated[
        str, typer.Option(help="Required launch-skill version.")
    ] = LAUNCH_SKILL_VERSION,
    force: Annotated[
        bool,
        typer.Option(help="Replace an unmanaged destination only after explicit review."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print stable machine-readable JSON."),
    ] = False,
) -> None:
    """Verify and atomically install the bundled official launch skill."""

    with bundled_launch_skill() as source:
        results = [
            install_launch_skill(
                source=source,
                agent=target,
                scope=scope,
                workspace=workspace,
                requested_version=version,
                force=force,
            )
            for target in _targets(agent)
        ]
    _emit("install", results, json_output)


@app.command("status")
def status(
    agent: Annotated[AgentOption, typer.Option(help="Coding-agent installation target.")] = "all",
    scope: Annotated[ScopeOption, typer.Option(help="Inspect project or user scope.")] = "project",
    workspace: Annotated[
        Path, typer.Option(help="Project workspace for project scope.")
    ] = Path("."),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print stable machine-readable JSON."),
    ] = False,
) -> None:
    """Verify bundled and installed skill integrity without changing files."""

    with bundled_launch_skill() as source:
        results = [
            launch_skill_status(
                source=source,
                agent=target,
                scope=scope,
                workspace=workspace,
            )
            for target in _targets(agent)
        ]
    _emit("status", results, json_output)


@app.command("remove")
def remove(
    agent: Annotated[AgentOption, typer.Option(help="Coding-agent installation target.")] = "all",
    scope: Annotated[
        ScopeOption, typer.Option(help="Remove from project or user scope.")
    ] = "project",
    workspace: Annotated[
        Path, typer.Option(help="Project workspace for project scope.")
    ] = Path("."),
    force: Annotated[
        bool,
        typer.Option(help="Remove an unmanaged destination only after explicit review."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print stable machine-readable JSON."),
    ] = False,
) -> None:
    """Remove only managed launch-skill installations by default."""

    results = [
        remove_launch_skill(
            agent=target,
            scope=scope,
            workspace=workspace,
            force=force,
        )
        for target in _targets(agent)
    ]
    _emit("remove", results, json_output)


def _targets(agent: AgentOption) -> list[Literal["codex", "claude"]]:
    if agent == "all":
        return ["codex", "claude"]
    if agent in {"codex", "claude"}:
        return [agent]
    raise CLIError(f"Unsupported coding-agent target: {agent}")


def _emit(action: str, results: list[dict[str, object]], json_output: bool) -> None:
    payload = {"action": action, "skill": "generalaugment-launch", "results": results}
    if json_output:
        print_json(payload)
        return
    table(
        "General Augment launch skill",
        ["Agent", "Scope", "State", "Path"],
        [
            [
                result.get("agent"),
                result.get("scope"),
                result.get("action") or result.get("installed_integrity"),
                result.get("path"),
            ]
            for result in results
        ],
    )
