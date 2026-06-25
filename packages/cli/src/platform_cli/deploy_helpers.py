"""Shared agent-manifest deploy logic used by public and internal commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.openapi import load_deploy_payload, project_name_from_config
from platform_cli.output import panel, print_success
from platform_cli.runtime import Runtime


def deploy_path(
    runtime: Runtime,
    config_path: Path,
    project_ref: str | None,
    *,
    quiet: bool = False,
) -> dict[str, Any]:
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
    if not quiet:
        print_success(f"Project {action}: {name}")
        panel(
            "Webhook URLs",
            f"WhatsApp: {runtime.config.base_url}/api/v1/webhooks/whatsapp\n"
            f"Telegram: {runtime.config.base_url}/api/v1/webhooks/telegram\n"
            f"SMS: {runtime.config.base_url}/api/v1/webhooks/sms",
        )
    return response
