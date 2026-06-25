"""CLI environment and platform preflight checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import print_json, table
from platform_cli.runtime import Runtime

BROWSER_ARTIFACT_PRODUCTION_PROOF_SCHEMA_VERSION = (
    "2026-06-14.browser-artifact-production-proof.v1"
)


def doctor(
    ctx: typer.Context,
    project: Annotated[
        str | None,
        typer.Option(
            help=(
                "Optional project id, slug, or name for agent-cloud readiness checks. "
                "Defaults to active_project when configured."
            ),
        ),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", help="Optional tenant app user id for memory profile preflight."),
    ] = None,
    fail_on_launch_blocked: Annotated[
        bool,
        typer.Option(
            "--fail-on-launch-blocked",
            help="Exit nonzero when canonical project launch readiness is blocked.",
        ),
    ] = False,
    browser_artifact_production_proof: Annotated[
        Path | None,
        typer.Option(
            "--browser-artifact-production-proof",
            help=(
                "Path to genaug projects browser-artifacts prove-production output proving "
                "hosted browser screenshots are production-durable."
            ),
        ),
    ] = None,
    raw: Annotated[bool, typer.Option("--json", help="Print machine-readable results.")] = False,
) -> None:
    """Check local CLI config, API reachability, auth, and optional project readiness."""
    runtime: Runtime = ctx.obj
    checks: list[dict[str, str]] = []
    launch_readiness: dict[str, Any] | None = None
    browser_artifact_proof: dict[str, Any] | None = None

    checks.append(_config_check(runtime))
    checks.append(_base_url_check(runtime))
    checks.append(_api_key_check(runtime))
    if browser_artifact_production_proof is not None:
        browser_artifact_proof = _append_browser_artifact_production_proof_check(
            checks,
            browser_artifact_production_proof,
        )

    with runtime.client() as client:
        try:
            ready = client.public("GET", "/health/ready")
            checks.append(
                _check(
                    "api_ready",
                    "PASS",
                    _status_detail(ready),
                    "The platform API answered /health/ready.",
                )
            )
        except CLIError as exc:
            checks.append(
                _check(
                    "api_ready",
                    "FAIL",
                    str(exc),
                    "Check --base-url or GENAUG_ADMIN_BASE_URL, then retry.",
                )
            )

        if runtime.config.api_key:
            try:
                identity = client.admin("GET", "/me")
                project_ids = identity.get("project_ids", []) if isinstance(identity, dict) else []
                detail = (
                    f"auth_method={identity.get('auth_method', 'unknown')}, "
                    f"projects={len(project_ids or [])}"
                    if isinstance(identity, dict)
                    else "authenticated"
                )
                checks.append(
                    _check(
                        "auth",
                        "PASS",
                        detail,
                        "The configured key can call the admin API.",
                    )
                )
            except CLIError as exc:
                checks.append(
                    _check(
                        "auth",
                        "FAIL",
                        str(exc),
                        "Run genaug auth login with a valid key or fix API key env overrides.",
                    )
                )
        else:
            checks.append(
                _check(
                    "auth",
                    "FAIL",
                    "No API key configured.",
                    "Run genaug auth login or set GENAUG_ADMIN_API_KEY.",
                )
            )

        project_ref = project or runtime.config.active_project
        if runtime.config.api_key and project_ref:
            launch_readiness = _append_project_checks(
                client,
                checks,
                project_ref=project_ref,
                user=user,
                fail_on_launch_blocked=fail_on_launch_blocked,
            )
        elif project or user:
            checks.append(
                _check(
                    "project_readiness",
                    "FAIL" if project else "WARN",
                    "project was not resolved",
                    "Pass --project or set active_project with genaug setup/bootstrap.",
                )
            )

    summary: dict[str, Any] = {"verdict": _verdict(checks), "checks": checks}
    if launch_readiness is not None:
        summary["launch_readiness"] = launch_readiness
    if browser_artifact_proof is not None:
        summary["browser_artifact_production_proof"] = browser_artifact_proof
    if raw:
        print_json(summary)
    else:
        table(
            "General Augment Doctor",
            ["Check", "Status", "Detail", "Next action"],
            [
                [item["name"], item["status"], item["detail"], item["next_action"]]
                for item in checks
            ],
        )
        if launch_readiness is not None:
            action_rows = _launch_readiness_action_rows(launch_readiness)
            if action_rows:
                table("Launch Readiness Next Actions", ["Action", "Command or URL"], action_rows)

    if summary["verdict"] == "FAIL":
        raise typer.Exit(1)


def _config_check(runtime: Runtime) -> dict[str, str]:
    """Return the config-file check."""
    if runtime.loaded_config_path.exists():
        return _check(
            "config",
            "PASS",
            f"loaded={runtime.loaded_config_path}",
            "No action needed.",
        )
    return _check(
        "config",
        "WARN",
        f"no saved config at {runtime.loaded_config_path}",
        "Run genaug auth login to persist config, or keep using env overrides.",
    )


def _base_url_check(runtime: Runtime) -> dict[str, str]:
    """Return a base URL sanity check."""
    base_url = runtime.config.base_url.rstrip("/")
    if base_url.startswith(("http://", "https://")):
        return _check("base_url", "PASS", base_url, "No action needed.")
    return _check(
        "base_url",
        "FAIL",
        base_url or "<empty>",
        "Set --base-url or GENAUG_ADMIN_BASE_URL to an http(s) URL.",
    )


def _api_key_check(runtime: Runtime) -> dict[str, str]:
    """Return an API-key presence check without printing the key."""
    if runtime.config.api_key:
        return _check("api_key", "PASS", "configured", "No action needed.")
    return _check(
        "api_key",
        "FAIL",
        "missing",
        "Run genaug auth login or set GENAUG_ADMIN_API_KEY.",
    )


def _append_browser_artifact_production_proof_check(
    checks: list[dict[str, str]],
    path: Path,
) -> dict[str, Any] | None:
    """Append a local production-proof artifact check for browser artifact storage."""
    next_action = (
        "Run genaug projects browser-artifacts prove-production with retained bootstrap "
        "and --require-browser-run-artifact evidence."
    )
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        checks.append(
            _check(
                "browser_artifact_production_proof",
                "FAIL",
                f"could not read {path}: {exc}",
                next_action,
            )
        )
        return None
    except json.JSONDecodeError as exc:
        checks.append(
            _check(
                "browser_artifact_production_proof",
                "FAIL",
                f"{path} is not valid JSON: {exc}",
                next_action,
            )
        )
        return None
    if not isinstance(payload, dict):
        checks.append(
            _check(
                "browser_artifact_production_proof",
                "FAIL",
                f"{path} did not contain a JSON object",
                next_action,
            )
        )
        return None
    schema = str(payload.get("schema_version") or "")
    verdict = str(payload.get("verdict") or "unknown")
    check_counts = _browser_artifact_proof_check_counts(payload)
    if schema != BROWSER_ARTIFACT_PRODUCTION_PROOF_SCHEMA_VERSION:
        checks.append(
            _check(
                "browser_artifact_production_proof",
                "FAIL",
                f"schema={schema or 'missing'}",
                "Regenerate the proof with genaug projects browser-artifacts prove-production.",
            )
        )
    elif verdict == "PASS":
        checks.append(
            _check(
                "browser_artifact_production_proof",
                "PASS",
                f"verdict=PASS, checks={check_counts['passed']}/{check_counts['total']}",
                "No action needed.",
            )
        )
    else:
        checks.append(
            _check(
                "browser_artifact_production_proof",
                "FAIL",
                f"verdict={verdict}, checks={check_counts['passed']}/{check_counts['total']}",
                _first_browser_artifact_proof_action(payload) or next_action,
            )
        )
    return _browser_artifact_proof_summary(payload, path=path)


def _browser_artifact_proof_check_counts(payload: dict[str, Any]) -> dict[str, int]:
    checks = payload.get("checks")
    items = [item for item in checks if isinstance(item, dict)] if isinstance(checks, list) else []
    passed = sum(1 for item in items if str(item.get("status") or "") == "PASS")
    return {"passed": passed, "total": len(items)}


def _first_browser_artifact_proof_action(payload: dict[str, Any]) -> str | None:
    actions = payload.get("next_actions")
    if not isinstance(actions, list):
        return None
    for action in actions:
        text = str(action).strip()
        if text:
            return text
    return None


def _browser_artifact_proof_summary(payload: dict[str, Any], *, path: Path) -> dict[str, Any]:
    counts = _browser_artifact_proof_check_counts(payload)
    return {
        "schema_version": str(payload.get("schema_version") or ""),
        "path": str(path),
        "generated_at": payload.get("generated_at"),
        "verdict": str(payload.get("verdict") or "unknown"),
        "checks": counts,
        "next_actions": [
            str(action)
            for action in payload.get("next_actions", [])
            if str(action).strip()
        ]
        if isinstance(payload.get("next_actions"), list)
        else [],
    }


def _status_detail(payload: Any) -> str:
    """Format a compact health-check detail."""
    if isinstance(payload, dict):
        status = payload.get("status") or payload.get("state") or "unknown"
        db = payload.get("db")
        redis = payload.get("redis")
        dependencies = ", ".join(str(item) for item in (db, redis) if item)
        return f"status={status}" + (f", {dependencies}" if dependencies else "")
    return str(payload)


def _append_project_checks(
    client: Any,
    checks: list[dict[str, str]],
    *,
    project_ref: str,
    user: str | None,
    fail_on_launch_blocked: bool,
) -> dict[str, Any] | None:
    """Append read-only project agent-cloud readiness checks."""
    try:
        project_payload = resolve_project(client, project_ref)
    except CLIError as exc:
        checks.append(
            _check(
                "project",
                "FAIL",
                str(exc),
                "Create the project with genaug init/deploy or pass a valid --project.",
            )
        )
        return None

    project_id = str(project_payload.get("id") or project_ref)
    checks.append(
        _check(
            "project",
            "PASS",
            _project_detail(project_payload),
            "No action needed.",
        )
    )
    launch_readiness = _append_launch_readiness_check(
        client,
        checks,
        project_id=project_id,
        fail_on_launch_blocked=fail_on_launch_blocked,
    )
    _append_api_check(
        checks,
        "runtime_policy",
        lambda: client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/runtime-policy",
        ),
        _runtime_policy_detail,
        "Review project runtime policy in the dashboard or run genaug tools discovery.",
    )
    _append_api_check(
        checks,
        "tool_catalog",
        lambda: client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/tools/catalog",
        ),
        _catalog_detail,
        "Run genaug tools catalog --project <project> or add tools with genaug integrate/mcp.",
    )
    _append_api_check(
        checks,
        "delegated_providers",
        lambda: client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/coding-providers",
        ),
        _delegated_provider_detail,
        "Run genaug providers readiness --project <project> --json.",
    )
    _append_api_check(
        checks,
        "approvals_inbox",
        lambda: client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/approvals",
            params={"status": "pending"},
        ),
        _items_detail("pending"),
        (
            "Run "
            f"genaug approvals list --project {_project_command_ref(project_payload)} "
            "--status pending --json."
        ),
    )
    _append_run_timeline_check(client, checks, project_id=project_id)
    if user:
        _append_api_check(
            checks,
            "memory_profile",
            lambda: client.app(
                "GET",
                f"/api/v1/agent/memory/profile/{encode_path_segment(user)}",
                headers={"X-Project-ID": project_id},
            ),
            _memory_profile_detail,
            "Run genaug memory store/search/export to verify the tenant memory workflow.",
        )
    _append_governance_proof_check(checks, project_payload=project_payload, user=user)
    return launch_readiness


def _append_launch_readiness_check(
    client: Any,
    checks: list[dict[str, str]],
    *,
    project_id: str,
    fail_on_launch_blocked: bool,
) -> dict[str, Any] | None:
    """Append the canonical server launch-readiness artifact as a doctor check."""

    next_action = "Run genaug projects launch-readiness --project <project> --json."
    try:
        payload = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/launch-readiness",
        )
    except CLIError as exc:
        checks.append(_check("launch_readiness", "WARN", str(exc), next_action))
        return None
    if not isinstance(payload, dict):
        checks.append(
            _check(
                "launch_readiness",
                "WARN",
                "launch readiness payload was not an object",
                next_action,
            )
        )
        return None
    verdict = str(payload.get("verdict") or "unknown")
    summary = payload.get("summary")
    required_ready = summary.get("required_ready", 0) if isinstance(summary, dict) else 0
    required_total = summary.get("required_total", 0) if isinstance(summary, dict) else 0
    required_open = summary.get("required_open", 0) if isinstance(summary, dict) else 0
    recommended_open = summary.get("recommended_open", 0) if isinstance(summary, dict) else 0
    status = "PASS"
    if verdict == "blocked":
        status = "FAIL" if fail_on_launch_blocked else "WARN"
    elif verdict != "ready":
        status = "WARN"
    checks.append(
        _check(
            "launch_readiness",
            status,
            (
                f"verdict={verdict}, required={required_ready}/{required_total}, "
                f"required_open={required_open}, recommended_open={recommended_open}"
            ),
            _first_launch_readiness_action(payload) or next_action,
        )
    )
    return payload


def _append_api_check(
    checks: list[dict[str, str]],
    name: str,
    request: Callable[[], Any],
    detail: Callable[[Any], str],
    next_action: str,
) -> None:
    """Append one read-only platform API check."""
    try:
        payload = request()
    except CLIError as exc:
        checks.append(_check(name, "FAIL", str(exc), next_action))
        return
    checks.append(_check(name, "PASS", detail(payload), "No action needed."))


def _launch_readiness_action_rows(payload: dict[str, Any]) -> list[list[object]]:
    """Return launch-readiness next actions for human doctor output."""

    actions = payload.get("next_actions")
    if not isinstance(actions, list):
        return []
    rows: list[list[object]] = []
    for item in actions[:5]:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                str(item.get("label") or "Next action"),
                str(item.get("command") or item.get("href") or ""),
            ]
        )
    return rows


def _first_launch_readiness_action(payload: dict[str, Any]) -> str | None:
    """Return the first useful launch-readiness command or URL."""

    rows = _launch_readiness_action_rows(payload)
    if not rows:
        return None
    action, command = rows[0]
    return f"{action}: {command}" if command else str(action)


def _append_run_timeline_check(
    client: Any,
    checks: list[dict[str, str]],
    *,
    project_id: str,
) -> None:
    """Append a read-only durable run timeline inspection check."""
    next_action = "Run genaug runs background-turn or genaug smoke to create the first timeline."
    try:
        payload = client.app(
            "GET",
            "/v1/agent-runs",
            params={"limit": 1},
            headers={"X-Project-ID": project_id},
        )
    except CLIError as exc:
        checks.append(_check("run_timeline", "FAIL", str(exc), next_action))
        return
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list) or not items:
        checks.append(_check("run_timeline", "WARN", "recent_runs=0", next_action))
        return
    latest = items[0]
    if not isinstance(latest, dict):
        checks.append(
            _check(
                "run_timeline",
                "FAIL",
                "latest run payload was not an object",
                "Run genaug runs list --project <project> --json and inspect the API response.",
            )
        )
        return
    run_id = str(latest.get("id") or "").strip()
    if not run_id:
        checks.append(
            _check(
                "run_timeline",
                "FAIL",
                "latest run was missing an id",
                "Run genaug runs list --project <project> --json and inspect the API response.",
            )
        )
        return
    try:
        run = client.app(
            "GET",
            f"/v1/agent-runs/{encode_path_segment(run_id)}",
            headers={"X-Project-ID": project_id},
        )
    except CLIError as exc:
        checks.append(
            _check(
                "run_timeline",
                "FAIL",
                str(exc),
                f"Run genaug runs inspect {run_id} --project <project> --json.",
            )
        )
        return
    events = run.get("run_events") if isinstance(run, dict) else None
    event_count = len(events) if isinstance(events, list) else 0
    if event_count < 1:
        checks.append(
            _check(
                "run_timeline",
                "FAIL",
                f"run_id={run_id}, events=0",
                (
                    f"Run genaug runs inspect {run_id} --project <project> --json "
                    "and check event persistence."
                ),
            )
        )
        return
    status = (
        str(run.get("status") or latest.get("status") or "unknown")
        if isinstance(run, dict)
        else "unknown"
    )
    checks.append(
        _check(
            "run_timeline",
            "PASS",
            f"recent_runs={len(items)}, run_id={run_id}, status={status}, events={event_count}",
            "No action needed.",
        )
    )


def _project_detail(payload: dict[str, Any]) -> str:
    """Return compact project identity detail."""
    bits = [
        f"id={payload.get('id', '')}",
        f"slug={payload.get('slug', '')}",
        f"status={payload.get('status', 'unknown')}",
    ]
    return ", ".join(bit for bit in bits if not bit.endswith("="))


def _project_command_ref(payload: dict[str, Any]) -> str:
    """Return the best stable project reference for copy-pasteable CLI commands."""
    return str(payload.get("slug") or payload.get("id") or "<project>")


def _append_governance_proof_check(
    checks: list[dict[str, str]],
    *,
    project_payload: dict[str, Any],
    user: str | None,
) -> None:
    """Append exact CLI commands for memory and approval proof."""
    project_ref = _project_command_ref(project_payload)
    user_ref = user or "<app-user-id>"
    commands = [
        f"genaug memory profile --project {project_ref} --user {user_ref} --json",
        (
            f"genaug memory export --project {project_ref} --user {user_ref} "
            "--output genaug-memory-export.json"
        ),
        f"genaug approvals list --project {project_ref} --status pending --json",
    ]
    checks.append(
        _check(
            "governance_proof",
            "PASS",
            f"memory_user={user_ref}, commands=3",
            "Run " + "; ".join(commands) + ".",
        )
    )


def _runtime_policy_detail(payload: Any) -> str:
    """Return compact runtime-policy detail."""
    if not isinstance(payload, dict):
        return "runtime policy reachable"
    tool_discovery = payload.get("tool_discovery")
    model_routing = payload.get("model_routing")
    parts = []
    if isinstance(tool_discovery, dict):
        parts.append(f"tool_discovery={tool_discovery.get('mode', 'unknown')}")
    if isinstance(model_routing, dict):
        parts.append(f"model_policy={model_routing.get('policy', 'configured')}")
    return ", ".join(parts) if parts else "runtime policy reachable"


def _catalog_detail(payload: Any) -> str:
    """Return compact tool-catalog detail."""
    items = payload.get("items", []) if isinstance(payload, dict) else []
    available = sum(
        1
        for item in items
        if isinstance(item, dict) and str(item.get("status", "")).lower() == "available"
    )
    return f"tools={len(items) if isinstance(items, list) else 0}, available={available}"


def _delegated_provider_detail(payload: Any) -> str:
    """Return compact delegated-provider readiness detail."""
    items = payload.get("items", []) if isinstance(payload, dict) else []
    providers = [item for item in items if isinstance(item, dict)]
    ready = sum(1 for item in providers if str(item.get("readiness") or "") == "ready")
    productized = _workflow_summary(
        workflow
        for item in providers
        for workflow in _workflow_values(item.get("delegated_workflows"))
    )
    planned = _workflow_summary(
        workflow
        for item in providers
        for workflow in _workflow_values(item.get("planned_workflows"))
    )
    hosted_screenshot = _hosted_screenshot_storage_summary(providers)
    return (
        f"providers={len(providers)}, ready={ready}, "
        f"productized={productized}, planned={planned}, "
        f"hosted_screenshots={hosted_screenshot}"
    )


def _hosted_screenshot_storage_summary(providers: list[dict[str, Any]]) -> str:
    """Return browser hosted-screenshot artifact storage readiness."""
    for item in providers:
        if str(item.get("provider") or "") != "browserbase":
            continue
        details = item.get("readiness_details")
        if not isinstance(details, dict):
            return "unknown"
        state = str(details.get("hosted_screenshot_storage") or "unknown").strip()
        backend = str(details.get("browser_artifact_storage_backend") or "").strip()
        if backend:
            return f"{state}:{backend}"
        return state or "unknown"
    return "not_configured"


def _workflow_values(value: object) -> list[str]:
    """Return workflow names from a readiness payload field."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _workflow_summary(values: Any) -> str:
    """Return a stable compact workflow summary."""
    workflows = sorted({str(value).strip() for value in values if str(value).strip()})
    return ",".join(workflows) if workflows else "none"


def _items_detail(label: str) -> Any:
    """Return a check detail formatter for list-style API responses."""

    def _detail(payload: Any) -> str:
        items = payload.get("items", []) if isinstance(payload, dict) else []
        count = len(items) if isinstance(items, list) else 0
        return f"{label}={count}"

    return _detail


def _memory_profile_detail(payload: Any) -> str:
    """Return compact memory-profile detail."""
    if not isinstance(payload, dict):
        return "memory profile reachable"
    facts = payload.get("facts", [])
    profile = payload.get("profile", {})
    fact_count = len(facts) if isinstance(facts, list) else 0
    profile_keys = len(profile) if isinstance(profile, dict) else 0
    return f"facts={fact_count}, profile_keys={profile_keys}"


def _check(name: str, status: str, detail: str, next_action: str) -> dict[str, str]:
    """Return one doctor check row."""
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "next_action": next_action,
    }


def _verdict(checks: list[dict[str, str]]) -> str:
    """Return the aggregate doctor verdict."""
    if any(item["status"] == "FAIL" for item in checks):
        return "FAIL"
    if any(item["status"] == "WARN" for item in checks):
        return "WARN"
    return "PASS"
