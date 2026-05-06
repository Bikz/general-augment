"""Deploy local agent configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.openapi import load_deploy_payload, project_name_from_config
from platform_cli.output import panel, print_success
from platform_cli.runtime import Runtime


def deploy(
    ctx: typer.Context,
    config_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="genaug-agent.yaml manifest to deploy.",
        ),
    ],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project id, slug, or name."),
    ] = None,
) -> None:
    """Validate and upload a local agent manifest."""
    runtime: Runtime = ctx.obj
    deploy_path(runtime, config_path, project_ref=project)


def deploy_path(runtime: Runtime, config_path: Path, project_ref: str | None) -> dict[str, Any]:
    """Deploy a local config path."""
    payload = load_deploy_payload(config_path)
    local_name = project_ref or project_name_from_config(config_path)
    with runtime.client() as client:
        try:
            existing = resolve_project(client, local_name)
        except CLIError as exc:
            if "Project not found" not in exc.message:
                raise
            existing = None
        if existing:
            response = cast(
                dict[str, Any],
                client.admin(
                    "PUT",
                    f"/projects/{encode_path_segment(str(existing['id']))}/config",
                    json=payload,
                ),
            )
            action = "updated"
        else:
            response = cast(
                dict[str, Any],
                client.admin("POST", "/projects/from-config", json=payload),
            )
            action = "created"
    name = response.get("name") or response.get("slug") or local_name
    print_success(f"Project {action}: {name}")
    panel(
        "Webhook URLs",
        f"WhatsApp: {runtime.config.base_url}/api/v1/webhooks/whatsapp\n"
        f"Telegram: {runtime.config.base_url}/api/v1/webhooks/telegram\n"
        f"SMS: {runtime.config.base_url}/api/v1/webhooks/sms",
    )
    return response
