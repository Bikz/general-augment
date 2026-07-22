"""Shared agent-manifest deploy logic used by public and internal commands."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.openapi import load_deploy_payload, project_name_from_config
from platform_cli.output import panel, print_success
from platform_cli.runtime import Runtime


def project_config_fingerprint(payload: Mapping[str, Any]) -> str:
    """Bind dashboard approval to the exact deployable configuration bundle."""

    artifact = {
        "yaml_content": str(payload.get("yaml_content") or ""),
        "soul_content": payload.get("soul_content"),
        "skills": list(payload.get("skills") or []),
    }
    encoded = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def deploy_path_with_installer(
    runtime: Runtime,
    config_path: Path,
    project_id: str,
    *,
    token: str,
    launch_session_id: str,
    yaml_content: str | None = None,
) -> dict[str, Any]:
    """Apply an approved manifest using installer control-plane authority."""
    payload = load_deploy_payload(config_path, yaml_content=yaml_content)
    payload["launch_session_id"] = launch_session_id
    with runtime.client() as client:
        response = client.installer(
            "PUT",
            f"/projects/{encode_path_segment(project_id)}/config",
            token=token,
            json=payload,
        )
    if not isinstance(response, dict):
        raise CLIError("Installer project configuration returned an invalid response.")
    return response
