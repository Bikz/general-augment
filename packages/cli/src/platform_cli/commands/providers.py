"""Provider setup helpers for self-serve onboarding."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import print_json, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import (
    installer_auth_metadata,
    normalize_capabilities,
    provider_setup_recipes,
    resolve_installer_project_id,
)

app = typer.Typer(help="Plan capability provider setup.")


@app.command("setup")
def setup_providers(
    ctx: typer.Context,
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability to configure, repeatable."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Provider API key. Omit for plan-only output."),
    ] = None,
    api_key_env: Annotated[
        str | None,
        typer.Option("--api-key-env", help="Read the provider API key from this env var."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(help="Optional provider base URL override."),
    ] = None,
    health_check: Annotated[
        bool,
        typer.Option("--health-check", help="Run a hosted health check after writing custody."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Produce provider setup steps with General Augment credential custody."""
    runtime: Runtime = ctx.obj
    capabilities = normalize_capabilities(capability or ["code", "browse", "search-x"])
    recipes = provider_setup_recipes(capabilities)
    secret = _provider_api_key(api_key=api_key, api_key_env=api_key_env)
    if secret is not None:
        if len(recipes) != 1:
            raise CLIError("Pass exactly one --capability when providing provider credentials.")
        recipes[0] = _configure_provider(
            runtime,
            recipes[0],
            project=project,
            api_key=secret,
            base_url=base_url,
            health_check=health_check,
        )
    payload = {
        "schema_version": "general-augment-provider-setup/v1",
        "providers": recipes,
        "health_check_requested": health_check,
        "security": {
            "credential_custody": "general_augment",
            "raw_secrets_in_repo": False,
            "raw_secrets_in_urls": False,
        },
    }
    if json_output:
        print_json(payload)
        return
    table(
        "Provider setup",
        ["Capability", "Provider", "Custody", "Health"],
        [
            [
                item["capability"],
                item["provider"],
                item["credential_custody"],
                item["health_command"],
            ]
            for item in payload["providers"]
        ],
    )


def _configure_provider(
    runtime: Runtime,
    recipe: dict[str, object],
    *,
    project: str | None,
    api_key: str,
    base_url: str | None,
    health_check: bool,
) -> dict[str, object]:
    """Write one provider credential through admin or installer auth."""
    provider = str(recipe["provider"])
    project_ref = project or runtime.config.active_project
    if not project_ref:
        raise CLIError("Pass --project or run genaug setup --bootstrap first.")
    credential_payload = {"api_key": api_key}
    if base_url:
        credential_payload["base_url"] = base_url
    installer = installer_auth_metadata(runtime.config)
    with runtime.client() as client:
        if installer is not None:
            token = str(installer["access_token"])
            project_id = resolve_installer_project_id(
                client, token=token, project_ref=str(project_ref)
            )
            credential = client.installer(
                "PUT",
                (
                    f"/projects/{encode_path_segment(project_id)}"
                    f"/capability-providers/{encode_path_segment(provider)}"
                ),
                token=token,
                json=credential_payload,
            )
            health = (
                client.installer(
                    "POST",
                    (
                        f"/projects/{encode_path_segment(project_id)}"
                        f"/capability-providers/{encode_path_segment(provider)}/health-check"
                    ),
                    token=token,
                )
                if health_check
                else None
            )
        else:
            project_payload = resolve_project(client, project_id := str(project_ref))
            project_id = str(project_payload["id"])
            credential = client.admin(
                "PUT",
                (
                    f"/projects/{encode_path_segment(project_id)}"
                    f"/capability-providers/{encode_path_segment(provider)}"
                ),
                json=credential_payload,
            )
            health = (
                client.admin(
                    "POST",
                    (
                        f"/projects/{encode_path_segment(project_id)}"
                        f"/capability-providers/{encode_path_segment(provider)}/health-check"
                    ),
                )
                if health_check
                else None
            )
    next_recipe = dict(recipe)
    next_recipe["project_id"] = project_id
    next_recipe["credential"] = credential
    next_recipe["health"] = health
    return next_recipe


def _provider_api_key(*, api_key: str | None, api_key_env: str | None) -> str | None:
    """Resolve an optional provider secret without printing it."""
    if api_key and api_key_env:
        raise typer.BadParameter("Use only one of --api-key or --api-key-env.")
    if api_key_env:
        value = os.getenv(api_key_env)
        if not value:
            raise typer.BadParameter(f"Environment variable {api_key_env} is not set.")
        return value
    return api_key
