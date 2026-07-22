"""Provider setup helpers for self-serve onboarding."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import APIError, CLIError
from platform_cli.output import print_json, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import (
    installer_access_token,
    installer_auth_metadata,
    normalize_capabilities,
    provider_setup_recipes,
    provider_setup_recipes_for_providers,
    resolve_installer_project_id,
)

app = typer.Typer(help="Plan capability provider setup.")
SMOKE_SCHEMA_VERSION = "general-augment-provider-smoke/v1"
SMOKE_MARKER = "tenant-provider-smoke-ok"
XAI_TOOL_AUDIT_IDS = {
    "x_search",
    "x-search",
    "xai.search",
    "xai_search",
    "video_gen",
    "video-generation",
    "xai.video",
    "xai_video",
}
SMOKE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "anthropic-managed-agents": (
        "provider_health",
        "managed_agent_session_id",
        "specialist_artifact",
        "usage_event",
        "tool_call_audit",
        "trace_id",
    ),
    "codex-mcp": (
        "provider_health",
        "mcp_discovery",
        "codex_thread_id",
        "usage_event",
        "tool_call_audit",
        "trace_id",
    ),
    "browserbase": (
        "provider_health",
        "mcp_discovery",
        "browser_session_id",
        "recording_url",
        "usage_event",
        "tool_call_audit",
        "trace_id",
    ),
    "xai": (
        "provider_health",
        "response_id",
        "trace_id",
        "usage_event",
        "tool_call_audit",
        "support_bundle",
    ),
    "fal": (
        "provider_health",
        "response_id",
        "trace_id",
        "usage_event",
        "media_asset_id",
        "signed_media_url",
        "retention_policy",
        "support_bundle",
    ),
    "veo": (
        "provider_health",
        "response_id",
        "trace_id",
        "usage_event",
        "media_asset_id",
        "signed_media_url",
        "retention_policy",
        "support_bundle",
    ),
}


@app.command("readiness")
def provider_readiness(
    ctx: typer.Context,
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Show delegated provider readiness for productized and planned workflows."""
    runtime: Runtime = ctx.obj
    project_ref = project or runtime.config.active_project
    if not project_ref:
        raise CLIError("Pass --project or run genaug setup --bootstrap first.")
    with runtime.client() as client:
        project_payload = resolve_project(client, str(project_ref))
        project_id = str(project_payload["id"])
        payload = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/coding-providers",
        )
    if json_output:
        print_json(payload)
        return
    items = payload.get("items", []) if isinstance(payload, dict) else []
    rows: list[list[object]] = [
        [
            str(item.get("provider") or ""),
            str(item.get("readiness") or item.get("status") or ""),
            _workflow_text(item.get("delegated_workflows")),
            _workflow_text(item.get("planned_workflows")),
            str(item.get("setup_hint") or ""),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    table(
        "Delegated Provider Readiness",
        ["Provider", "Readiness", "Productized", "Planned", "Setup"],
        rows,
    )
    evidence_rows: list[list[object]] = [
        [str(item.get("provider") or ""), signal, detail]
        for item in items
        if isinstance(item, dict)
        for signal, detail in _readiness_evidence_rows(item.get("readiness_details"))
    ]
    if evidence_rows:
        table(
            "Delegated Provider Evidence",
            ["Provider", "Signal", "Detail"],
            evidence_rows,
        )


def _readiness_evidence_rows(value: object) -> list[tuple[str, str]]:
    """Return compact secret-free provider evidence posture."""
    if not isinstance(value, dict):
        return []
    rows: list[tuple[str, str]] = []
    blockers = _readiness_blockers(value.get("blockers"))
    rows.extend(("blocker", blocker) for blocker in blockers[:2])
    screenshot_state = str(value.get("hosted_screenshot_storage") or "").strip()
    screenshot_backend = str(value.get("browser_artifact_storage_backend") or "").strip()
    if screenshot_state:
        label = screenshot_state.replace("_", " ")
        rows.append(
            (
                "hosted screenshots",
                f"{label}" + (f" ({screenshot_backend})" if screenshot_backend else ""),
            )
        )
    return rows


def _readiness_blockers(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


@app.command("setup")
def setup_providers(
    ctx: typer.Context,
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability to configure, repeatable."),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option(
            "--provider",
            help=(
                "Provider id to configure, repeatable. Examples: browserbase, "
                "anthropic-managed-agents, codex-mcp, xai, fal, veo."
            ),
        ),
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
    capabilities = normalize_capabilities(capability or [])
    try:
        recipes = (
            provider_setup_recipes_for_providers(provider)
            if provider
            else provider_setup_recipes(capabilities or ["code", "browse", "search-x"])
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    secret = _provider_api_key(api_key=api_key, api_key_env=api_key_env)
    if secret is not None:
        if len(recipes) != 1:
            raise CLIError(
                "Pass exactly one --capability or --provider when providing provider credentials."
            )
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
            for item in recipes
        ],
    )


@app.command("smoke")
def smoke_providers(
    ctx: typer.Context,
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability smoke to plan, repeatable."),
    ] = None,
    provider: Annotated[
        list[str] | None,
        typer.Option("--provider", help="Provider id to smoke, repeatable."),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name for a live smoke."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Provider API key. Prefer --api-key-env."),
    ] = None,
    api_key_env: Annotated[
        str | None,
        typer.Option("--api-key-env", help="Read the provider API key from this env var."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option(help="Optional provider base URL override for live smokes."),
    ] = None,
    model_tier: Annotated[
        str,
        typer.Option("--model-tier", help="Responses model tier to probe for model providers."),
    ] = "balanced",
    skip_upsert: Annotated[
        bool,
        typer.Option(
            "--skip-upsert",
            help="Use an already-stored provider credential for the live smoke.",
        ),
    ] = False,
    evidence_output: Annotated[
        Path | None,
        typer.Option(
            "--evidence-output",
            "-o",
            help="Write redacted provider smoke evidence JSON for launch/support review.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Plan or run provider launch-smoke evidence checks."""
    runtime: Runtime = ctx.obj
    capabilities = normalize_capabilities(capability or [])
    try:
        recipes = (
            provider_setup_recipes_for_providers(provider)
            if provider
            else provider_setup_recipes(capabilities or ["code", "browse", "search-x"])
        )
    except ValueError as exc:
        raise CLIError(str(exc)) from exc
    if (api_key or api_key_env or skip_upsert) and len(recipes) != 1:
        raise CLIError("Live provider smokes require exactly one --provider or --capability.")
    secret = _optional_provider_api_key(api_key=api_key, api_key_env=api_key_env)
    live_requested = bool(project and len(recipes) == 1 and (secret is not None or skip_upsert))
    items: list[dict[str, object]] = [
        _run_provider_smoke(
            runtime,
            recipes[0],
            project=project,
            api_key=secret,
            base_url=base_url,
            model_tier=model_tier,
            skip_upsert=skip_upsert,
        )
        if live_requested
        else _provider_smoke_plan(recipe, project=project)
        for recipe in recipes
    ]
    payload: dict[str, object] = {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "mode": "live" if live_requested else "plan",
        "providers": items,
        "security": {
            "raw_secrets_in_output": False,
            "raw_provider_payloads_in_output": False,
        },
    }
    if evidence_output is not None:
        payload["artifact_path"] = str(evidence_output)
        _write_provider_smoke_evidence(evidence_output, payload)
    if json_output:
        print_json(payload)
        return
    table(
        "Provider smoke",
        ["Capability", "Provider", "Status", "Blockers"],
        [
            [
                item["capability"],
                item["provider"],
                item["status"],
                _provider_blockers_text(item),
            ]
            for item in items
        ],
    )
    if evidence_output is not None:
        typer.echo(f"Evidence: {evidence_output}")


def _write_provider_smoke_evidence(path: Path, payload: dict[str, object]) -> None:
    """Write provider smoke evidence JSON, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _provider_blockers_text(item: dict[str, object]) -> str:
    """Return a display-safe blockers summary."""
    raw_blockers = item.get("blockers", [])
    blockers = raw_blockers if isinstance(raw_blockers, list) else []
    return "; ".join(str(blocker) for blocker in blockers) or "none"


def _workflow_text(value: object) -> str:
    """Return a compact workflow list for readiness tables."""
    if not isinstance(value, list):
        return "none"
    workflows = [str(item).strip() for item in value if str(item).strip()]
    return ", ".join(workflows) or "none"


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
    credential_kind = str(recipe.get("credential_kind") or "")
    project_ref = project or runtime.config.active_project
    if not project_ref:
        raise CLIError("Pass --project or run genaug setup --bootstrap first.")
    credential_payload: dict[str, object] = {"api_key": api_key}
    configured_base_url = base_url or str(recipe.get("base_url") or "")
    if configured_base_url:
        credential_payload["base_url"] = configured_base_url
    if credential_kind == "model_provider":
        return _configure_model_provider(
            runtime,
            recipe,
            project_ref=project_ref,
            credential_payload=credential_payload,
            health_check=health_check,
        )
    if base_url:
        credential_payload["base_url"] = base_url
    installer = installer_auth_metadata(runtime.config)
    token = installer_access_token(runtime) if installer is not None else None
    with runtime.client() as client:
        if installer is not None:
            assert token is not None
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


def _configure_model_provider(
    runtime: Runtime,
    recipe: dict[str, object],
    *,
    project_ref: str,
    credential_payload: dict[str, object],
    health_check: bool,
) -> dict[str, object]:
    """Write one model-provider credential through admin custody."""
    installer = installer_auth_metadata(runtime.config)
    if installer is not None and not runtime.config.api_key:
        raise CLIError(
            "Model provider setup currently requires admin API-key auth. "
            "Use genaug auth login --api-key or genaug model-providers set."
        )
    provider = str(recipe["provider"])
    api_mode = recipe.get("api_mode")
    model_prefixes = recipe.get("model_prefixes")
    if api_mode:
        credential_payload["api_mode"] = api_mode
    if isinstance(model_prefixes, list) and model_prefixes:
        credential_payload["model_prefixes"] = model_prefixes
    with runtime.client() as client:
        project_payload = resolve_project(client, str(project_ref))
        project_id = str(project_payload["id"])
        credential = client.admin(
            "PUT",
            (
                f"/projects/{encode_path_segment(project_id)}"
                f"/model-providers/{encode_path_segment(provider)}"
            ),
            json=credential_payload,
        )
        health = (
            client.admin(
                "POST",
                (
                    f"/projects/{encode_path_segment(project_id)}"
                    f"/model-providers/{encode_path_segment(provider)}/health-check"
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


def _optional_provider_api_key(*, api_key: str | None, api_key_env: str | None) -> str | None:
    """Resolve a provider secret for smoke; missing env vars become smoke blockers."""
    if api_key and api_key_env:
        raise typer.BadParameter("Use only one of --api-key or --api-key-env.")
    if api_key_env:
        return os.getenv(api_key_env)
    return api_key


def _provider_smoke_plan(recipe: dict[str, object], *, project: str | None) -> dict[str, object]:
    """Return a secret-free launch-smoke plan for one provider."""
    provider = str(recipe["provider"])
    env_name = str(recipe.get("api_key_env") or "PROVIDER_API_KEY")
    blockers = []
    if not project:
        blockers.append("Pass --project")
    blockers.append(f"Set {env_name}")
    blockers.extend(_provider_launch_blockers(recipe))
    return {
        "capability": recipe["capability"],
        "provider": provider,
        "credential_kind": recipe["credential_kind"],
        "status": "blocked",
        "required_evidence": list(_required_evidence(provider)),
        "blockers": blockers,
        "smoke_command": (
            f"genaug providers smoke --provider {provider} --project <project> "
            f"--api-key-env {env_name}"
        ),
    }


def _run_provider_smoke(
    runtime: Runtime,
    recipe: dict[str, object],
    *,
    project: str | None,
    api_key: str | None,
    base_url: str | None,
    model_tier: str,
    skip_upsert: bool,
) -> dict[str, object]:
    """Run the strongest CLI-owned smoke available for one provider."""
    provider = str(recipe["provider"])
    if recipe.get("credential_kind") != "model_provider":
        return _run_capability_provider_health_smoke(
            runtime,
            recipe,
            project=project,
            api_key=api_key,
            base_url=base_url,
            skip_upsert=skip_upsert,
        )
    return _run_model_provider_smoke(
        runtime,
        recipe,
        project=project,
        api_key=api_key,
        base_url=base_url,
        model_tier=model_tier,
        skip_upsert=skip_upsert,
        provider=provider,
    )


def _run_capability_provider_health_smoke(
    runtime: Runtime,
    recipe: dict[str, object],
    *,
    project: str | None,
    api_key: str | None,
    base_url: str | None,
    skip_upsert: bool,
) -> dict[str, object]:
    """Run custody and health for external capability providers."""
    blockers = _preflight_blockers(project=project, api_key=api_key, skip_upsert=skip_upsert)
    if blockers:
        item = _provider_smoke_plan(recipe, project=project)
        item["blockers"] = blockers + _provider_launch_blockers(recipe)
        return item
    assert project is not None
    try:
        configured = (
            _configure_provider(
                runtime,
                recipe,
                project=project,
                api_key=api_key or "",
                base_url=base_url,
                health_check=True,
            )
            if not skip_upsert
            else _health_check_existing_provider(runtime, recipe, project=project)
        )
    except APIError as exc:
        return _provider_api_error_item(recipe, exc)
    health = configured.get("health") if isinstance(configured, dict) else None
    evidence = {"provider_health": _safe_provider_health(health)}
    blockers = []
    health_passed = isinstance(health, dict) and health.get("status") == "available"
    if not health_passed:
        blockers.append(_health_blocker(health))
    blockers.extend(_provider_launch_blockers(recipe))
    return {
        "capability": recipe["capability"],
        "provider": recipe["provider"],
        "credential_kind": recipe["credential_kind"],
        "status": "blocked",
        "required_evidence": list(_required_evidence(str(recipe["provider"]))),
        "checks": [
            {"name": "credential_custody", "status": "passed"},
            {
                "name": "provider_health",
                "status": "passed" if health_passed else "blocked",
            },
        ],
        "evidence": evidence,
        "blockers": blockers,
    }


def _run_model_provider_smoke(
    runtime: Runtime,
    recipe: dict[str, object],
    *,
    project: str | None,
    api_key: str | None,
    base_url: str | None,
    model_tier: str,
    skip_upsert: bool,
    provider: str,
) -> dict[str, object]:
    """Run provider custody, health, Responses, and support-bundle checks."""
    blockers = _preflight_blockers(project=project, api_key=api_key, skip_upsert=skip_upsert)
    if blockers:
        item = _provider_smoke_plan(recipe, project=project)
        item["blockers"] = blockers
        return item
    assert project is not None
    try:
        configured = (
            _configure_provider(
                runtime,
                recipe,
                project=project,
                api_key=api_key or "",
                base_url=base_url,
                health_check=True,
            )
            if not skip_upsert
            else _health_check_existing_provider(runtime, recipe, project=project)
        )
    except APIError as exc:
        return _provider_api_error_item(recipe, exc)
    project_id = str(configured.get("project_id") or project)
    health = configured.get("health") if isinstance(configured, dict) else {}
    checks = [
        {"name": "credential_custody", "status": "passed"},
        {
            "name": "provider_health",
            "status": "passed"
            if isinstance(health, dict) and health.get("status") == "available"
            else "blocked",
        },
    ]
    evidence: dict[str, object] = {"provider_health": _safe_provider_health(health)}
    blockers = []
    if checks[-1]["status"] != "passed":
        blockers.append(_health_blocker(health))
        return _smoke_item(
            recipe,
            status="blocked",
            checks=checks,
            evidence=evidence,
            blockers=blockers,
        )

    with runtime.client() as client:
        response = client.app(
            "POST",
            "/v1/responses",
            json=_responses_payload(provider=provider, model_tier=model_tier),
            headers={"X-Project-ID": project_id},
        )
        response_check, response_blockers = _response_check(provider, response)
        checks.append(response_check)
        evidence.update(_response_evidence(response))
        if response_blockers:
            blockers.extend(response_blockers)
        support_bundle: dict[str, Any] = {}
        response_id = str(evidence.get("response_id") or "")
        trace_id = str(evidence.get("trace_id") or "")
        if response_id:
            support_bundle = client.admin(
                "GET",
                (
                    f"/projects/{encode_path_segment(project_id)}"
                    "/observability/support-bundle"
                ),
                params={
                    "limit": 50,
                    "response_id": response_id,
                    "trace_id": trace_id,
                },
            )
        support_check, support_blockers = _support_bundle_check(
            provider,
            support_bundle,
            response_id=response_id,
        )
        checks.append(support_check)
        evidence["support_bundle"] = _support_bundle_evidence(support_bundle)
        blockers.extend(support_blockers)
        launch_check, launch_evidence, launch_blockers = _launch_evidence_check(
            provider,
            response=response,
            support_bundle=support_bundle,
        )
        checks.append(launch_check)
        evidence.update(launch_evidence)
        blockers.extend(launch_blockers)
    status = "passed" if not blockers else "blocked"
    return _smoke_item(recipe, status=status, checks=checks, evidence=evidence, blockers=blockers)


def _health_check_existing_provider(
    runtime: Runtime,
    recipe: dict[str, object],
    *,
    project: str,
) -> dict[str, object]:
    """Run health against an already-stored credential."""
    provider = str(recipe["provider"])
    credential_kind = str(recipe.get("credential_kind") or "")
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        project_id = str(project_payload["id"])
        if credential_kind == "model_provider":
            health = client.admin(
                "POST",
                (
                    f"/projects/{encode_path_segment(project_id)}"
                    f"/model-providers/{encode_path_segment(provider)}/health-check"
                ),
            )
        else:
            health = client.admin(
                "POST",
                (
                    f"/projects/{encode_path_segment(project_id)}"
                    f"/capability-providers/{encode_path_segment(provider)}/health-check"
                ),
            )
    next_recipe = dict(recipe)
    next_recipe["project_id"] = project_id
    next_recipe["health"] = health
    return next_recipe


def _smoke_item(
    recipe: dict[str, object],
    *,
    status: str,
    checks: list[dict[str, str]],
    evidence: dict[str, object],
    blockers: list[str],
) -> dict[str, object]:
    """Return one normalized provider smoke item."""
    return {
        "capability": recipe["capability"],
        "provider": recipe["provider"],
        "credential_kind": recipe["credential_kind"],
        "status": status,
        "required_evidence": list(_required_evidence(str(recipe["provider"]))),
        "checks": checks,
        "evidence": evidence,
        "blockers": blockers,
    }


def _provider_api_error_item(
    recipe: dict[str, object],
    exc: APIError,
) -> dict[str, object]:
    """Return a secret-free blocked smoke item for platform API failures."""
    message = str(exc)
    if len(message) > 240:
        message = f"{message[:237]}..."
    blocker = f"Provider smoke platform API returned {exc.status_code}: {message}"
    blockers = [blocker]
    blockers.extend(_provider_launch_blockers(recipe))
    return _smoke_item(
        recipe,
        status="blocked",
        checks=[{"name": "platform_api", "status": "blocked"}],
        evidence={
            "platform_api": {
                "status_code": exc.status_code,
                "message": message,
            }
        },
        blockers=blockers,
    )


def _preflight_blockers(
    *,
    project: str | None,
    api_key: str | None,
    skip_upsert: bool,
) -> list[str]:
    """Return blockers before live smoke network calls."""
    blockers = []
    if not project:
        blockers.append("Pass --project to run a live provider smoke.")
    if not skip_upsert and not api_key:
        blockers.append(
            "Pass --api-key-env with a set env var, pass --api-key, or use --skip-upsert."
        )
    return blockers


def _provider_launch_blockers(recipe: dict[str, object]) -> list[str]:
    """Return honest blockers that health alone cannot satisfy."""
    provider = str(recipe["provider"])
    credential_kind = str(recipe.get("credential_kind") or "")
    if credential_kind == "model_provider":
        if provider in {"fal", "veo"}:
            return [
                "Generated-video launch still requires media storage, signed URL, "
                "retention, and deletion evidence.",
            ]
        if provider == "xai":
            return [
                "X search/video launch still requires Hermes tool-call audit and usage evidence.",
            ]
        return []
    if provider == "browserbase":
        return [
            "Browser launch still requires MCP discovery, browser session id, "
            "and recording evidence.",
        ]
    if provider == "anthropic-managed-agents":
        return [
            "Managed-agent launch still requires a delegated session and artifact evidence.",
        ]
    if provider == "codex-mcp":
        return [
            "Codex MCP launch still requires managed MCP discovery and Codex thread evidence.",
        ]
    return []


def _required_evidence(provider: str) -> tuple[str, ...]:
    """Return provider launch evidence names."""
    return SMOKE_EVIDENCE.get(provider, ("provider_health", "response_id", "trace_id"))


def _responses_payload(*, provider: str, model_tier: str) -> dict[str, object]:
    """Return the live Responses smoke payload."""
    return {
        "model": model_tier,
        "input": f"Reply exactly with: {SMOKE_MARKER}",
        "user": f"genaug-provider-smoke-{provider}",
        "metadata": {
            "source": "genaug-provider-smoke",
            "provider": provider,
        },
    }


def _response_check(
    provider: str,
    response: object,
) -> tuple[dict[str, str], list[str]]:
    """Validate Responses metadata proves tenant provider routing."""
    if not isinstance(response, dict):
        return (
            {"name": "responses_smoke", "status": "blocked"},
            ["Responses smoke returned no JSON object."],
        )
    raw_metadata = response.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    blockers = []
    if response.get("status") != "completed":
        blockers.append(f"Responses smoke status was {response.get('status')}.")
    if metadata.get("general_augment_model_provider") != provider:
        blockers.append("Responses metadata did not show the requested provider.")
    if metadata.get("general_augment_model_provider_source") != "tenant":
        blockers.append("Responses metadata did not prove tenant-owned provider routing.")
    if SMOKE_MARKER not in _response_output_text(response).lower():
        blockers.append("Responses output did not include the smoke marker.")
    return {"name": "responses_smoke", "status": "passed" if not blockers else "blocked"}, blockers


def _response_evidence(response: object) -> dict[str, object]:
    """Return secret-free Responses evidence."""
    if not isinstance(response, dict):
        return {}
    raw_metadata = response.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    return {
        "response_id": response.get("id"),
        "trace_id": metadata.get("general_augment_trace_id") or metadata.get("trace_id"),
        "model_provider": metadata.get("general_augment_model_provider"),
        "model_provider_source": metadata.get("general_augment_model_provider_source"),
    }


def _support_bundle_check(
    provider: str,
    payload: dict[str, Any],
    *,
    response_id: str,
) -> tuple[dict[str, str], list[str]]:
    """Validate support bundle has usage and control-plane attribution."""
    blockers = []
    if not response_id:
        blockers.append("Support bundle lookup skipped because response_id was missing.")
    if not _support_has_usage_event(payload, provider=provider, response_id=response_id):
        blockers.append("Support bundle did not include tenant provider usage attribution.")
    if not _support_has_health_event(payload, provider=provider):
        blockers.append("Support bundle did not include provider health evidence.")
    return {"name": "support_bundle", "status": "passed" if not blockers else "blocked"}, blockers


def _support_bundle_evidence(payload: dict[str, Any]) -> dict[str, int]:
    """Return bounded support-bundle counts."""
    return {
        "usage_event_count": len(_items(payload, "usage_events")),
        "audit_event_count": len(_items(payload, "audit_events")),
        "control_plane_event_count": len(_items(payload, "control_plane_events")),
    }


def _launch_evidence_check(
    provider: str,
    *,
    response: object,
    support_bundle: dict[str, Any],
) -> tuple[dict[str, str], dict[str, object], list[str]]:
    """Validate provider-specific launch evidence beyond generic model routing."""

    if provider == "xai":
        evidence = _tool_call_audit_evidence(support_bundle)
        blockers = []
        if evidence["matching_event_count"] == 0:
            blockers.append(
                "Support bundle did not include X search/video tool-call audit evidence."
            )
        return (
            {"name": "launch_evidence", "status": "passed" if not blockers else "blocked"},
            {"tool_call_audit": evidence},
            blockers,
        )
    if provider in {"fal", "veo"}:
        evidence = _generated_media_evidence(response)
        blockers = []
        if not evidence["media_asset_id"]:
            blockers.append("Generated-video response did not include media asset evidence.")
        if not evidence["signed_media_url_present"]:
            blockers.append("Generated-video response did not include signed media URL evidence.")
        if not evidence["retention_policy"]:
            blockers.append("Generated-video response did not include retention policy evidence.")
        return (
            {"name": "launch_evidence", "status": "passed" if not blockers else "blocked"},
            {"generated_media": evidence},
            blockers,
        )
    return {"name": "launch_evidence", "status": "passed"}, {}, []


def _tool_call_audit_evidence(payload: dict[str, Any]) -> dict[str, object]:
    """Return redacted X tool-call audit evidence from a support bundle."""

    matching_tool_ids: list[str] = []
    for item in _items(payload, "audit_events"):
        tool_id = str(item.get("tool_id") or "")
        action_type = str(item.get("action_type") or "")
        if (
            action_type == "tool_call"
            and item.get("success") is not False
            and tool_id in XAI_TOOL_AUDIT_IDS
        ):
            matching_tool_ids.append(tool_id)
    return {
        "matching_event_count": len(matching_tool_ids),
        "tool_ids": sorted(set(matching_tool_ids)),
    }


def _generated_media_evidence(response: object) -> dict[str, object]:
    """Return redacted generated-media evidence from a Responses payload."""

    metadata = response.get("metadata") if isinstance(response, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    media_asset_id = _first_metadata_value(
        metadata,
        "general_augment_media_asset_id",
        "general_augment_video_asset_id",
        "media_asset_id",
        "video_asset_id",
    )
    signed_media_url = _first_metadata_value(
        metadata,
        "general_augment_signed_media_url",
        "general_augment_signed_url",
        "signed_media_url",
        "signed_url",
    )
    retention_policy = _first_metadata_value(
        metadata,
        "general_augment_retention_policy",
        "general_augment_media_retention_policy",
        "retention_policy",
        "media_retention_policy",
        "retention_expires_at",
    )
    return {
        "media_asset_id": media_asset_id,
        "signed_media_url_present": bool(signed_media_url),
        "retention_policy": retention_policy,
    }


def _first_metadata_value(metadata: dict[str, Any], *keys: str) -> str | None:
    """Return the first non-empty metadata value as a string."""

    for key in keys:
        value = metadata.get(key)
        if value:
            return str(value)
    return None


def _support_has_usage_event(
    payload: dict[str, Any],
    *,
    provider: str,
    response_id: str,
) -> bool:
    """Return whether support evidence includes the response usage attribution."""
    for item in _items(payload, "usage_events"):
        raw_metadata = item.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        if (
            metadata.get("response_id") == response_id
            and metadata.get("model_provider") == provider
            and metadata.get("model_provider_source") == "tenant"
        ):
            return True
    return False


def _support_has_health_event(payload: dict[str, Any], *, provider: str) -> bool:
    """Return whether support evidence includes provider health."""
    for item in _items(payload, "control_plane_events"):
        raw_metadata = item.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        if (
            item.get("event_type") == "model_provider_credential.health_check"
            and metadata.get("provider") == provider
            and metadata.get("status") == "available"
        ):
            return True
    return False


def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return dict items for a support-bundle collection."""
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _response_output_text(response: dict[str, Any]) -> str:
    """Extract text from a Responses-style object."""
    texts: list[str] = []
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        texts.append(output_text)
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text" and part.get("text"):
                texts.append(str(part["text"]))
    return "\n".join(texts)


def _safe_provider_health(payload: object) -> dict[str, object]:
    """Return secret-free provider health evidence."""
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key in {"provider", "status", "message", "checked_at", "last_validated_at", "latency_ms"}
    }


def _health_blocker(payload: object) -> str:
    """Return a compact provider-health blocker."""
    if not isinstance(payload, dict):
        return "Provider health check did not return a JSON object."
    return f"Provider health status is {payload.get('status')}: {payload.get('message')}"
