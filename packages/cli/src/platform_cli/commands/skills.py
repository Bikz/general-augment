"""Skill management commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import (
    installer_auth_metadata,
    resolve_installer_project_id,
    skill_design_recipe,
)

app = typer.Typer(help="Manage tenant skills.")


@app.command("design")
def design_skill(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option(help="App workspace for the skill design session."),
    ] = Path("."),
    job_type: Annotated[
        str,
        typer.Option("--job-type", help="Skill job type, for example website-builder."),
    ] = "website-builder",
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name for optional write-through."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Push the generated skill and prompt-flow draft."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create an agent-friendly skill and prompt-flow design brief."""
    runtime: Runtime = ctx.obj
    bundle = _starter_skill_bundle(job_type)
    skill = skill_design_recipe(job_type)
    applied: dict[str, object] | None = None
    payload = {
        "schema_version": "general-augment-skill-design/v1",
        "workspace": str(workspace.expanduser().resolve()),
        "skill": skill,
        "bundle": {
            "skill_name": bundle["skill_name"],
            "flow_id": bundle["flow_id"],
            "version_id": bundle["version_id"],
        },
        "applied": applied,
        "next_actions": [
            "Answer the skill questions with the app owner.",
            "Write or update the local SKILL.md and prompt-flow files.",
            "Run genaug skills apply <SKILL.md> --project <project>.",
        ],
    }
    if apply:
        applied = _push_starter_skill_bundle(
            runtime,
            project=project,
            bundle=bundle,
        )
        payload["applied"] = applied
    if json_output:
        print_json(payload)
        return
    table(
        f"Skill design: {skill['name']}",
        ["Field", "Value"],
        [
            ["Job type", skill["job_type"]],
            ["Artifacts", ", ".join(skill["artifacts"])],
            ["Questions", len(skill["questions"])],
        ],
    )
    if applied:
        print_success("Applied starter skill and prompt-flow draft.")


@app.command("list")
def list_skills(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List SKILL.md files registered for a tenant."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/skills",
        )
    if json_output:
        print_json(payload)
        return
    items = payload.get("items", []) if isinstance(payload, dict) else []
    rows = [
        [
            item.get("name", ""),
            item.get("description", ""),
            item.get("version", ""),
            ", ".join(str(tag) for tag in item.get("tags", []) or []),
            ", ".join(str(tool) for tool in item.get("tools", []) or []),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    table(
        f"Skills for {project_payload.get('slug', project)}",
        ["Name", "Description", "Version", "Tags", "Tools"],
        rows,
    )


@app.command("view")
def view_skill(
    ctx: typer.Context,
    skill_name: str = typer.Argument(..., help="Skill name."),
    project: str = typer.Option(..., help="Project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show one tenant SKILL.md file."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "GET",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/skills/"
                f"{encode_path_segment(skill_name)}"
            ),
        )
    if json_output:
        print_json(payload)
        return
    typer.echo(str(payload.get("content", "")) if isinstance(payload, dict) else "")


@app.command("apply")
def apply_skill(
    ctx: typer.Context,
    skill_file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Path to a SKILL.md file."),
    ],
    project: str = typer.Option(..., help="Project id, slug, or name."),
) -> None:
    """Create or replace one tenant skill from a local SKILL.md file."""
    runtime: Runtime = ctx.obj
    content = skill_file.read_text(encoding="utf-8")
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/skills",
            json={"content": content},
        )
    print_success(f"Applied skill {payload.get('name', skill_file.stem)}.")


@app.command("delete")
def delete_skill(
    ctx: typer.Context,
    skill_name: str = typer.Argument(..., help="Skill name."),
    project: str = typer.Option(..., help="Project id, slug, or name."),
) -> None:
    """Delete one tenant skill."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "DELETE",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/skills/"
                f"{encode_path_segment(skill_name)}"
            ),
    )
    print_success(f"Deleted skill {payload.get('name', skill_name)}.")


def _push_starter_skill_bundle(
    runtime: Runtime,
    *,
    project: str | None,
    bundle: dict[str, object],
) -> dict[str, object]:
    """Push the generated skill and prompt-flow bundle to the selected project."""
    project_ref = project or runtime.config.active_project
    if not project_ref:
        raise CLIError("Pass --project or run genaug setup --bootstrap first.")
    installer = installer_auth_metadata(runtime.config)
    with runtime.client() as client:
        if installer is not None:
            token = str(installer["access_token"])
            project_id = resolve_installer_project_id(
                client, token=token, project_ref=str(project_ref)
            )
            skill = client.installer(
                "POST",
                f"/projects/{encode_path_segment(project_id)}/skills",
                token=token,
                json={"content": str(bundle["skill_content"])},
            )
            prompt_flow = client.installer(
                "PUT",
                (
                    f"/projects/{encode_path_segment(project_id)}/prompt-flows/"
                    f"{encode_path_segment(str(bundle['flow_id']))}"
                ),
                token=token,
                json={
                    "version_id": bundle["version_id"],
                    "name": bundle["skill_name"],
                    "status": "draft",
                    "graph": bundle["prompt_flow"],
                },
            )
        else:
            project_payload = resolve_project(client, str(project_ref))
            project_id = str(project_payload["id"])
            skill = client.admin(
                "POST",
                f"/projects/{encode_path_segment(project_id)}/skills",
                json={"content": str(bundle["skill_content"])},
            )
            prompt_flow = client.admin(
                "PUT",
                (
                    f"/projects/{encode_path_segment(project_id)}/prompt-flows/"
                    f"{encode_path_segment(str(bundle['flow_id']))}"
                ),
                json={
                    "version_id": bundle["version_id"],
                    "name": bundle["skill_name"],
                    "status": "draft",
                    "graph": bundle["prompt_flow"],
                },
            )
    return {"skill": skill, "prompt_flow": prompt_flow}


def _starter_skill_bundle(job_type: str) -> dict[str, object]:
    """Return portable SKILL.md and prompt-flow starter content."""
    if job_type != "website-builder":
        name = job_type.replace("-", " ").title()
        flow_id = job_type.replace("-", "_")
    else:
        name = "Website Builder"
        flow_id = "website_builder"
    version_id = f"{job_type}:v1"
    skill_content = f"""---
name: {name}
description: Build, review, and prepare website previews.
version: "1.0"
tags:
  - website
  - coding
tools:
  - delegated_coding
  - browser_preview
---
# {name}

Build safe website previews from user intent, tenant memory, approved assets, and
policy constraints. Review the output for request fidelity, visual polish,
responsive layout, accessibility, and preview-only deployment boundaries before
Hermes writes the final user-facing handoff.
"""
    prompt_flow = {
        "id": flow_id,
        "version_id": version_id,
        "name": name,
        "nodes": [
            {
                "id": "intake",
                "kind": "intake",
                "title": "Intake",
                "prompt_template": "Capture the user request, assets, constraints, and goal.",
                "depends_on": [],
                "metadata": {"owner": "hermes"},
            },
            {
                "id": "delegate_build",
                "kind": "managed_agent_run",
                "title": "Delegate Build",
                "prompt_template": (
                    "Delegate bounded website build work, require review and improvement, "
                    "and return preview artifacts only."
                ),
                "depends_on": ["intake"],
                "metadata": {"deploy_mode": "preview_only"},
            },
            {
                "id": "reply",
                "kind": "reply",
                "title": "Reply",
                "prompt_template": "Hermes summarizes the preview and safe next actions.",
                "depends_on": ["delegate_build"],
                "metadata": {"author": "hermes"},
            },
        ],
    }
    return {
        "skill_name": name,
        "flow_id": flow_id,
        "version_id": version_id,
        "skill_content": skill_content,
        "prompt_flow": prompt_flow,
    }
