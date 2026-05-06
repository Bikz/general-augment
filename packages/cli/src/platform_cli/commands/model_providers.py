"""Tenant-owned model provider credential commands."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage tenant-owned model provider keys.")


@app.command("list")
def list_model_providers(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List model provider credentials without exposing secrets."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/model-providers",
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    rows = [_provider_row(item) for item in items if isinstance(item, dict)]
    table(
        "Model providers",
        ["Provider", "Status", "API Mode", "Base URL", "Prefixes", "Validated At"],
        rows,
    )


@app.command("set")
def set_model_provider(
    ctx: typer.Context,
    provider: Annotated[str, typer.Argument(help="Provider id, such as openai or anthropic.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Provider API key. Omit to enter a hidden prompt."),
    ] = None,
    api_key_env: Annotated[
        str | None,
        typer.Option("--api-key-env", help="Read the provider API key from this env var."),
    ] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Optional base URL.")] = None,
    api_mode: Annotated[str | None, typer.Option("--api-mode", help="Optional API mode.")] = None,
    model_prefix: Annotated[
        list[str] | None,
        typer.Option("--model-prefix", help="Model prefix this key can serve. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Store or rotate one tenant-owned model provider API key."""
    secret = _provider_api_key(api_key=api_key, api_key_env=api_key_env)
    payload: dict[str, object] = {"api_key": secret}
    if base_url is not None:
        payload["base_url"] = base_url
    if api_mode is not None:
        payload["api_mode"] = api_mode
    if model_prefix:
        payload["model_prefixes"] = list(model_prefix)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "PUT",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/model-providers/"
                f"{encode_path_segment(provider)}"
            ),
            json=payload,
        )
    if json_output:
        print_json(response)
        return
    stored_provider = _value(response, "provider") or provider
    print_success(f"Stored model provider credential for {stored_provider}.")
    table(
        "Model provider",
        ["Field", "Value"],
        [
            ["Provider", _value(response, "provider") or provider],
            ["Status", _value(response, "status")],
            ["API Mode", _value(response, "api_mode")],
            ["Base URL Configured", _value(response, "base_url_configured")],
            ["Model Prefixes", _prefixes(response)],
            ["Validated At", _value(response, "last_validated_at")],
        ],
    )


@app.command("health")
def check_model_provider_health(
    ctx: typer.Context,
    provider: Annotated[str, typer.Argument(help="Provider id, such as openai or anthropic.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Validate one stored model provider credential."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/model-providers/"
                f"{encode_path_segment(provider)}/health-check"
            ),
        )
    if json_output:
        print_json(response)
        return
    table(
        "Model provider health",
        ["Field", "Value"],
        [
            ["Provider", _value(response, "provider") or provider],
            ["Status", _value(response, "status")],
            ["Message", _value(response, "message")],
            ["HTTP Status", _value(response, "status_code")],
            ["Retryable", _value(response, "retryable")],
            ["Latency MS", _value(response, "latency_ms")],
            ["Validated At", _value(response, "last_validated_at")],
        ],
    )


@app.command("revoke")
def revoke_model_provider(
    ctx: typer.Context,
    provider: Annotated[str, typer.Argument(help="Provider id, such as openai or anthropic.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm revoking this tenant-owned provider key."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Revoke one tenant-owned model provider credential."""
    if not yes and not typer.confirm(f"Revoke model provider credential for {provider}?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "DELETE",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/model-providers/"
                f"{encode_path_segment(provider)}"
            ),
        )
    if json_output:
        print_json(response)
        return
    revoked_provider = _value(response, "provider") or provider
    print_success(f"Revoked model provider credential for {revoked_provider}.")


def _provider_api_key(*, api_key: str | None, api_key_env: str | None) -> str:
    """Resolve a provider API key from an option, env var, or hidden prompt."""

    if api_key and api_key_env:
        raise typer.BadParameter("Use only one of --api-key or --api-key-env.")
    if api_key_env:
        value = os.getenv(api_key_env)
        if not value:
            raise typer.BadParameter(f"Environment variable {api_key_env} is not set.")
        return value
    if api_key:
        return api_key
    return str(typer.prompt("Provider API key", hide_input=True))


def _provider_row(item: dict[str, object]) -> list[object]:
    """Return one model provider list row."""

    return [
        item.get("provider", ""),
        item.get("status", ""),
        item.get("api_mode", "") or "",
        item.get("base_url_configured", ""),
        _prefixes(item),
        item.get("last_validated_at", "") or "",
    ]


def _prefixes(payload: object) -> str:
    """Return model prefixes as a printable string."""

    if not isinstance(payload, dict):
        return ""
    prefixes = payload.get("model_prefixes")
    if not isinstance(prefixes, list):
        return ""
    return ", ".join(str(prefix) for prefix in prefixes)


def _value(payload: object, key: str) -> object:
    """Safely read one response value."""

    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
