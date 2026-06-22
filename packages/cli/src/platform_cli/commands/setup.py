"""Self-serve General Augment setup command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from platform_cli.client import PlatformClient, encode_path_segment
from platform_cli.config import save_config
from platform_cli.errors import CLIError
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import (
    build_setup_payload,
    installer_auth_metadata,
    write_payload,
)


def setup(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option(help="App workspace to inspect."),
    ] = Path("."),
    project: Annotated[str | None, typer.Option(help="Project id, slug, or name.")] = None,
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability to configure, repeatable."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the setup plan JSON to this path."),
    ] = None,
    bootstrap: Annotated[
        bool,
        typer.Option(
            "--bootstrap",
            help="Use browser installer auth to select/create a project and mint a runtime key.",
        ),
    ] = False,
    project_name: Annotated[
        str | None,
        typer.Option(help="Project name to create when bootstrapping."),
    ] = None,
    project_slug: Annotated[
        str | None,
        typer.Option(help="Project slug to create when bootstrapping."),
    ] = None,
    runtime_key_name: Annotated[
        str,
        typer.Option(help="Display name for the bootstrapped runtime key."),
    ] = "Self-serve app backend",
    skip_runtime_key: Annotated[
        bool,
        typer.Option("--skip-runtime-key", help="Do not mint a project runtime key."),
    ] = False,
    print_env: Annotated[
        bool,
        typer.Option(
            "--print-env",
            help="Print the generated runtime env block once; never writes it to artifacts.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Inspect an app and produce a non-destructive General Augment setup plan."""
    runtime: Runtime = ctx.obj
    if json_output and print_env:
        raise CLIError("--print-env is only available in human output mode.")
    bootstrap_payload: dict[str, object] | None = None
    runtime_env: dict[str, str] | None = None
    selected_project: str | None = project
    setup_config = runtime.config
    if bootstrap:
        bootstrap_payload, runtime_env = _bootstrap_setup(
            runtime,
            workspace=workspace,
            project=project,
            project_name=project_name,
            project_slug=project_slug,
            runtime_key_name=runtime_key_name,
            skip_runtime_key=skip_runtime_key,
        )
        project_payload = bootstrap_payload.get("project")
        if isinstance(project_payload, dict):
            selected_project = str(project_payload.get("id") or selected_project or "")
            config_update: dict[str, object] = {"active_project": selected_project}
            # Persist the minted runtime key so smoke/verify/doctor authenticate
            # without a manual `export`. The config file is chmod 600 and the raw
            # key is never written to artifacts or echoed unless --print-env asks.
            if runtime_env and runtime_env.get("GENAUG_API_KEY"):
                config_update["api_key"] = runtime_env["GENAUG_API_KEY"]
            setup_config = runtime.config.model_copy(update=config_update)
            save_config(setup_config, runtime.config_path)
    payload = build_setup_payload(
        workspace=workspace,
        config=setup_config,
        requested_capabilities=capability or [],
        project=selected_project,
        bootstrap=bootstrap_payload,
    )
    artifact_path = write_payload(payload, output, workspace)
    payload["artifact_path"] = str(artifact_path)
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if json_output:
        print_json(payload)
        return
    table(
        "General Augment setup plan",
        ["Field", "Value"],
        [
            ["Workspace", payload["workspace"]["root"]],
            ["Frameworks", ", ".join(payload["detected"]["frameworks"])],
            [
                "OpenAI Responses call sites",
                payload["detected"]["openai"]["responses_api_call_count"],
            ],
            ["Auth", payload["auth"]["status"]],
            ["Artifact", artifact_path],
        ],
    )
    print_success("Setup plan written without changing app code or storing secrets.")
    if print_env and runtime_env:
        print_success("Runtime env generated. It is shown once and was not written to disk.")
        typer.echo(_format_env_block(runtime_env))


def _bootstrap_setup(
    runtime: Runtime,
    *,
    workspace: Path,
    project: str | None,
    project_name: str | None,
    project_slug: str | None,
    runtime_key_name: str,
    skip_runtime_key: bool,
) -> tuple[dict[str, object], dict[str, str] | None]:
    """Bootstrap remote tenant resources through installer auth."""
    installer = installer_auth_metadata(runtime.config)
    if installer is None:
        raise CLIError("Run genaug auth login before genaug setup --bootstrap.")
    token = str(installer["access_token"])
    with runtime.client() as client:
        projects_payload = client.installer("GET", "/projects", token=token)
        project_payload = _select_or_create_project(
            client,
            token=token,
            workspace=workspace,
            projects_payload=projects_payload,
            project=project,
            project_name=project_name,
            project_slug=project_slug,
        )
        runtime_key_payload = None
        if not skip_runtime_key:
            runtime_key_payload = client.installer(
                "POST",
                f"/projects/{encode_path_segment(str(project_payload['id']))}/runtime-keys",
                token=token,
                json={"name": runtime_key_name, "scopes": ["responses:create"]},
            )
    redacted = {
        "applied": True,
        "project": _redacted_project(project_payload),
        "runtime_key": _redacted_runtime_key(runtime_key_payload)
        if runtime_key_payload is not None
        else None,
    }
    return redacted, _runtime_env(
        runtime.config.base_url,
        project_payload,
        runtime_key_payload,
    )


def _select_or_create_project(
    client: PlatformClient,
    *,
    token: str,
    workspace: Path,
    projects_payload: object,
    project: str | None,
    project_name: str | None,
    project_slug: str | None,
) -> dict[str, object]:
    """Select an existing installer project or create one."""
    items = _project_items(projects_payload)
    if project:
        for item in items:
            if project in {
                str(item.get("id", "")),
                str(item.get("slug", "")),
                str(item.get("name", "")),
            }:
                return item
    elif len(items) == 1 and not project_name and not project_slug:
        return items[0]
    elif len(items) > 1 and not project_name and not project_slug:
        raise CLIError("Multiple projects are available. Pass --project or --project-slug.")

    name = project_name or project or _default_project_name(workspace)
    slug = project_slug or _slugify(name)
    return client.installer(
        "POST",
        "/projects",
        token=token,
        json={"name": name, "slug": slug, "system_prompt": "You are a helpful agent."},
    )


def _project_items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items", [])
    return [item for item in items if isinstance(item, dict)]


def _redacted_project(payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": payload.get("id", ""),
        "name": payload.get("name", ""),
        "slug": payload.get("slug", ""),
    }


def _redacted_runtime_key(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "id": payload.get("id", ""),
        "name": payload.get("name", ""),
        "masked_key": payload.get("masked_key", ""),
        "project_id": payload.get("project_id", ""),
        "scopes": payload.get("scopes", []),
    }


def _runtime_env(
    base_url: str,
    project_payload: dict[str, object],
    runtime_key_payload: object,
) -> dict[str, str] | None:
    if not isinstance(runtime_key_payload, dict) or not runtime_key_payload.get("api_key"):
        return None
    api_base = base_url.rstrip("/")
    return {
        "GENAUG_API_KEY": str(runtime_key_payload["api_key"]),
        "GENAUG_PROJECT_ID": str(project_payload.get("id", "")),
        "GENAUG_API_BASE_URL": api_base,
        "GENAUG_OPENAI_BASE_URL": f"{api_base}/v1",
    }


def _format_env_block(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in values.items())


def _default_project_name(workspace: Path) -> str:
    name = workspace.expanduser().resolve().name.strip()
    return name.replace("-", " ").replace("_", " ").title() or "General Augment Project"


def _slugify(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    parts = [part for part in slug.split("-") if part]
    return "-".join(parts)[:50] or "general-augment-project"
