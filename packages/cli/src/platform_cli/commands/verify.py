"""Project verification command."""

from __future__ import annotations

import os
import uuid
from typing import Any

import typer

from platform_cli import __version__
from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import panel, print_json, table
from platform_cli.readiness import build_readiness_checklist
from platform_cli.runtime import Runtime


def verify(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    message: str = typer.Option(
        "Reply with one short sentence confirming this General Augment project works.",
        help="Message for the hosted agent test.",
    ),
    user: str = typer.Option(
        "genaug-verify-user",
        help="Synthetic app user id for memory and agent checks.",
    ),
    phone_e164: str = typer.Option("+15550000000", help="Synthetic E.164 user identity."),
    channel: str = typer.Option("sms", help="Synthetic channel: sms, whatsapp, ios, or telegram."),
    dashboard_url: str = typer.Option(
        os.getenv("GENAUG_DASHBOARD_URL", "https://app.generalaugment.com"),
        help="Dashboard base URL for follow-up UI checks.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Verify a project through the CLI before checking the dashboard UI."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        payload = build_project_verification_payload(
            client,
            project=project,
            message=message,
            user=user,
            phone_e164=phone_e164,
            channel=channel,
            dashboard_url=dashboard_url,
        )
    if json_output:
        print_json(payload)
    else:
        table(
            f"Project Verify: {payload['project']['slug']}",
            ["Check", "Status", "Detail"],
            [[item["name"], item["status"], item["detail"]] for item in payload["checks"]],
        )
        panel(
            "Dashboard Follow-up",
            "\n".join(f"{key}: {value}" for key, value in payload["dashboard"].items()),
        )
    if payload["verdict"] != "PASS":
        failed = ", ".join(item["name"] for item in payload["checks"] if item["status"] == "FAIL")
        raise CLIError(f"Project verification failed: {failed}")


def build_project_verification_payload(
    client: Any,
    *,
    project: str,
    message: str,
    user: str,
    phone_e164: str,
    channel: str,
    dashboard_url: str,
) -> dict[str, Any]:
    """Run project acceptance checks and return a machine-readable payload."""

    checks: list[dict[str, Any]] = []
    ready = client.public("GET", "/health/ready")
    checks.append(_check("api_ready", _health_ok(ready), _health_detail(ready)))

    project_payload = resolve_project(client, project)
    project_id = str(project_payload["id"])
    project_slug = str(project_payload.get("slug") or project)
    checks.append(_check("project_resolved", True, project_id))
    identity = client.admin("GET", "/me")

    keys = client.admin("GET", "/keys")
    key_items = keys.get("items", []) if isinstance(keys, dict) else []
    project_keys = [
        item
        for item in key_items
        if isinstance(item, dict) and str(item.get("project_id") or "") == project_id
    ]
    checks.append(
        _check(
            "project_api_key",
            bool(project_keys),
            _key_detail(project_keys),
        )
    )
    checks.append(
        _project_key_execution_check(
            client,
            project_id=project_id,
            user=user,
            message=message,
            identity=identity,
        )
    )

    tools_payload = client.admin("GET", "/tools")
    tools = tools_payload if isinstance(tools_payload, list) else tools_payload.get("items", [])
    checks.append(_check("tool_registry", isinstance(tools, list), f"{len(tools)} tools"))

    runtime_policy = client.admin(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/runtime-policy",
    )
    checks.append(
        _check(
            "runtime_policy_model_routing",
            _model_routing_policy_ok(runtime_policy),
            _model_routing_policy_detail(runtime_policy),
        )
    )
    soul = client.admin("GET", f"/projects/{encode_path_segment(project_id)}/soul")
    checks.append(_check("soul_visible", _soul_visible_ok(soul), _soul_detail(soul)))

    skills = client.admin(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/skills",
        params={"limit": 100},
    )
    checks.append(
        _check(
            "skills_visible",
            _skills_visible_ok(skills, runtime_policy),
            _skills_detail(skills, runtime_policy),
        )
    )

    agent_test = client.admin(
        "POST",
        f"/projects/{encode_path_segment(project_id)}/test",
        json={"message": message, "phone_e164": phone_e164, "channel": channel},
    )
    agent_ok = not agent_test.get("error") and bool(
        agent_test.get("response_text") or agent_test.get("response")
    )
    checks.append(
        _check(
            "agent_test",
            agent_ok,
            agent_test.get("error")
            or agent_test.get("details")
            or str(agent_test.get("response_text") or agent_test.get("response") or ""),
        )
    )
    checks.append(_run_timeline_check(client, project_id=project_id, agent_test=agent_test))

    logs = client.admin(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/logs",
        params={"limit": 5},
    )
    log_items = logs.get("items", []) if isinstance(logs, dict) else []
    checks.append(_check("logs", isinstance(log_items, list), f"{len(log_items)} recent rows"))

    usage = client.admin("GET", f"/projects/{encode_path_segment(project_id)}/usage")
    totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
    limits = usage.get("limits", {}) if isinstance(usage, dict) else {}
    checks.append(_check("usage", isinstance(totals, dict), _usage_detail(totals)))
    checks.append(_check("usage_limits", isinstance(limits, dict), _limits_detail(limits)))

    observability = client.admin(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/observability",
        params={"limit": 5},
    )
    traces = observability.get("traces", []) if isinstance(observability, dict) else []
    checks.append(_check("observability", isinstance(traces, list), f"{len(traces)} trace rows"))

    channel_status = client.admin(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/channels/status",
    )
    channel_items = channel_status.get("channels", []) if isinstance(channel_status, dict) else []
    checks.append(
        _check("channel_status", isinstance(channel_items, list), f"{len(channel_items)} channels")
    )

    verification_id = uuid.uuid4().hex[:12]
    memory_checks = _run_memory_lifecycle(
        client,
        project_id=project_id,
        user=user,
        verification_id=verification_id,
    )
    checks.extend(memory_checks)

    audit = client.admin(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/audit/tool-calls",
        params={"limit": 5},
    )
    audit_items = audit.get("items", []) if isinstance(audit, dict) else []
    checks.append(
        _check("tool_call_audit", isinstance(audit_items, list), f"{len(audit_items)} rows")
    )

    dashboard = _dashboard_links(dashboard_url, project_id)
    return {
        "cli": {"version": __version__},
        "api": _api_version_detail(ready),
        "auth": _auth_detail(identity),
        "project": {
            "id": project_id,
            "slug": project_slug,
            "name": project_payload.get("name"),
        },
        "verdict": _verdict(checks),
        "checks": checks,
        "readiness_checklist": build_readiness_checklist(checks, project=project_payload),
        "runtime_policy": _runtime_policy_artifact(runtime_policy),
        "dashboard": dashboard,
    }


def _check(name: str, passed: bool, detail: str) -> dict[str, str]:
    """Build a machine-readable check row."""

    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}


def _skip(name: str, detail: str) -> dict[str, str]:
    """Build a machine-readable skipped check row."""

    return {"name": name, "status": "SKIP", "detail": detail}


def _verdict(checks: list[dict[str, str]]) -> str:
    """Return FAIL only when a required check failed."""

    return "FAIL" if any(item["status"] == "FAIL" for item in checks) else "PASS"


def _health_ok(payload: object) -> bool:
    """Return whether a health payload is ready."""

    return isinstance(payload, dict) and payload.get("status") == "ok"


def _health_detail(payload: object) -> str:
    """Return compact health detail."""

    if not isinstance(payload, dict):
        return str(payload)
    dependencies = [
        f"{key}={payload[key]}"
        for key in ("db", "redis")
        if key in payload and payload[key] is not None
    ]
    return ", ".join(dependencies) or str(payload.get("status"))


def _api_version_detail(payload: object) -> dict[str, str]:
    """Return API build/version metadata for automation artifacts."""

    if not isinstance(payload, dict):
        return {}
    return {
        key: str(payload[key])
        for key in ("version", "build_sha", "status")
        if key in payload and payload[key] is not None
    }


def _auth_detail(payload: object) -> dict[str, str]:
    """Return safe auth metadata for automation artifacts."""

    if not isinstance(payload, dict):
        return {}
    detail = {
        key: str(payload[key])
        for key in ("auth_method", "project_id")
        if key in payload and payload[key] is not None
    }
    project_ids = _identity_project_ids(payload)
    if project_ids:
        detail["project_ids"] = ",".join(project_ids)
    return detail


def _project_key_execution_check(
    client: Any,
    *,
    project_id: str,
    user: str,
    message: str,
    identity: object,
) -> dict[str, str]:
    """Exercise `/v1/responses` when the configured CLI key is project-scoped."""

    if not _identity_is_project_scoped_to(identity, project_id):
        return _skip(
            "project_key_execution",
            (
                "configured CLI key is not project-scoped to this project; "
                "project key existence was checked, but project-key execution was not"
            ),
        )
    response = client.app(
        "POST",
        "/v1/responses",
        json={
            "model": "balanced",
            "user": user,
            "input": message,
            "metadata": {
                "source": "genaug-cli-verify",
                "feature": "project_key_execution",
            },
        },
    )
    if not isinstance(response, dict):
        return _check("project_key_execution", False, str(response))
    response_id = str(response.get("id") or "")
    status = str(response.get("status") or "")
    text = _response_text(response)
    passed = bool(response_id) and status in {"completed", "complete", ""}
    return _check(
        "project_key_execution",
        passed,
        response_id or text or status or "missing response id",
    )


def _run_timeline_check(
    client: Any,
    *,
    project_id: str,
    agent_test: object,
) -> dict[str, str]:
    """Inspect the durable run produced by the dashboard/admin test turn."""
    if not isinstance(agent_test, dict):
        return _check("run_timeline_inspect", False, "agent test response was not an object")
    metadata = agent_test.get("metadata")
    if not isinstance(metadata, dict):
        return _check("run_timeline_inspect", False, "agent test metadata missing")
    run_id = str(metadata.get("agent_run_id") or "").strip()
    if not run_id:
        return _check("run_timeline_inspect", False, "agent test did not return agent_run_id")
    try:
        run = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/runs/{encode_path_segment(run_id)}",
        )
    except CLIError as exc:
        return _check("run_timeline_inspect", False, str(exc))
    if not isinstance(run, dict):
        return _check("run_timeline_inspect", False, f"run {run_id} response was not an object")
    events = run.get("run_events")
    status = str(run.get("status") or "unknown")
    event_count = len(events) if isinstance(events, list) else 0
    return _check(
        "run_timeline_inspect",
        bool(run.get("id")) and isinstance(events, list) and event_count > 0,
        f"run_id={run.get('id') or run_id}, status={status}, events={event_count}",
    )


def _identity_is_project_scoped_to(identity: object, project_id: str) -> bool:
    """Return whether the configured credential itself is scoped to this project."""

    if not isinstance(identity, dict):
        return False
    return str(identity.get("project_id") or "") == project_id


def _identity_project_ids(identity: dict[str, Any]) -> list[str]:
    """Return safe project id strings from an identity payload."""

    raw_project_ids = identity.get("project_ids")
    if isinstance(raw_project_ids, list) and raw_project_ids:
        return [str(project_id) for project_id in raw_project_ids]
    if identity.get("project_id"):
        return [str(identity["project_id"])]
    return []


def _response_text(response: dict[str, Any]) -> str:
    """Return compact text from a Responses-compatible payload."""

    if response.get("output_text"):
        return str(response["output_text"])
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                texts.append(str(block.get("text") or ""))
    return "\n".join(texts)


def _usage_detail(totals: dict[str, Any]) -> str:
    """Return compact usage detail."""

    turns = totals.get("agent_turns_count", 0)
    cost = totals.get("total_cost_usd", 0)
    return f"agent_turns={turns}, cost_usd={cost}"


def _key_detail(items: list[dict[str, Any]]) -> str:
    """Return a compact project-key readiness summary."""

    if not items:
        return "no project-scoped API keys found"
    names = ", ".join(str(item.get("name") or item.get("id") or "key") for item in items[:3])
    return f"{len(items)} project key(s): {names}"


def _limits_detail(limits: dict[str, Any]) -> str:
    """Return compact usage-limit detail."""

    turns = limits.get("agent_turns_per_day", "unknown")
    tokens = limits.get("tokens_per_day", "unknown")
    over_limit = limits.get("over_limit", False)
    return f"agent_turns_per_day={turns}, tokens_per_day={tokens}, over_limit={over_limit}"


def _model_routing_policy_ok(payload: object) -> bool:
    """Return whether runtime policy exposes usable tenant model routing."""

    if not isinstance(payload, dict):
        return False
    routing = payload.get("model_routing")
    if not isinstance(routing, dict):
        return False
    tiers = routing.get("tiers")
    required_tiers = ("simple", "balanced", "complex")
    return (
        routing.get("mode") == "tiered_complexity"
        and routing.get("default_tier") == "balanced"
        and routing.get("channel_parity") is True
        and isinstance(tiers, dict)
        and all(str(tiers.get(tier) or "") for tier in required_tiers)
    )


def _model_routing_policy_detail(payload: object) -> str:
    """Return a compact model-routing summary for verify output."""

    if not isinstance(payload, dict):
        return "runtime policy response was not an object"
    routing = payload.get("model_routing")
    if not isinstance(routing, dict):
        return "runtime policy did not include model_routing"
    tiers = routing.get("tiers")
    if not isinstance(tiers, dict):
        return "model_routing.tiers missing"
    simple = str(tiers.get("simple") or "missing")
    balanced = str(tiers.get("balanced") or "missing")
    complex_model = str(tiers.get("complex") or "missing")
    mode = str(routing.get("mode") or "missing")
    default_tier = str(routing.get("default_tier") or "missing")
    parity = routing.get("channel_parity")
    return (
        f"mode={mode}, simple={simple}, balanced={balanced}, "
        f"complex={complex_model}, default_tier={default_tier}, channel_parity={parity}"
    )


def _soul_visible_ok(payload: object) -> bool:
    """Return whether SOUL content is visible through the admin API."""

    return isinstance(payload, dict) and bool(str(payload.get("content") or "").strip())


def _soul_detail(payload: object) -> str:
    """Return compact SOUL visibility detail."""

    if not isinstance(payload, dict):
        return "SOUL response was not an object"
    content = str(payload.get("content") or "")
    if not content.strip():
        return "SOUL content missing"
    return f"{len(content)} chars"


def _skills_visible_ok(payload: object, runtime_policy: object) -> bool:
    """Return whether tenant skills are visible and match runtime-policy names."""

    items = _skill_items(payload)
    if items is None:
        return False
    listed_names = _skill_names_from_items(items)
    runtime_names = set(_runtime_policy_skill_names(runtime_policy))
    return runtime_names.issubset(listed_names)


def _skills_detail(payload: object, runtime_policy: object) -> str:
    """Return compact skill visibility detail."""

    items = _skill_items(payload)
    if items is None:
        return "skills response did not include an items list"
    listed_names = _skill_names_from_items(items)
    runtime_names = set(_runtime_policy_skill_names(runtime_policy))
    missing = sorted(runtime_names - listed_names)
    if missing:
        return f"{len(listed_names)} listed, missing runtime policy skills: {', '.join(missing)}"
    return f"{len(listed_names)} listed"


def _skill_items(payload: object) -> list[object] | None:
    """Return the skill list from an admin API response."""

    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    return items if isinstance(items, list) else None


def _skill_names_from_items(items: list[object]) -> set[str]:
    """Return skill names from list response rows."""

    names: set[str] = set()
    for item in items:
        if isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]))
    return names


def _runtime_policy_skill_names(payload: object) -> list[str]:
    """Return runtime-policy skill names."""

    if not isinstance(payload, dict):
        return []
    skills = payload.get("skills")
    if not isinstance(skills, dict):
        return []
    names = skills.get("names")
    if not isinstance(names, list):
        return []
    return [str(name) for name in names if str(name)]


def _runtime_policy_artifact(payload: object) -> dict[str, Any]:
    """Return the secret-free runtime policy fields useful in verify artifacts."""

    if not isinstance(payload, dict):
        return {}
    artifact: dict[str, Any] = {}
    for key in (
        "project_id",
        "model_routing",
        "tool_discovery",
        "hermes_exposure",
        "platform_tools",
        "mcp",
        "skills",
    ):
        value = payload.get(key)
        if value is not None:
            artifact[key] = value
    return artifact


def _dashboard_links(dashboard_url: str, project_id: str) -> dict[str, str]:
    """Build dashboard URLs for UI follow-up checks."""

    base = dashboard_url.rstrip("/")
    encoded = encode_path_segment(project_id)
    project_root = f"{base}/dashboard/projects/{encoded}"
    return {
        "project": project_root,
        "integrate": f"{project_root}/integrate",
        "tools": f"{project_root}/tools",
        "observability": f"{project_root}/observability",
        "analytics": f"{project_root}/analytics",
    }


def _run_memory_lifecycle(
    client: Any,
    *,
    project_id: str,
    user: str,
    verification_id: str,
) -> list[dict[str, str]]:
    """Store, search, profile, and delete one synthetic memory fact."""

    headers = {"X-Project-ID": project_id}
    recall_marker = f"genaug-memory-verify-{verification_id}"
    fact = (
        "CLI verification user prefers concise onboarding notes. "
        f"The user's private verification marker is {recall_marker}."
    )
    checks: list[dict[str, str]] = []

    stored = client.app(
        "POST",
        "/api/v1/agent/memory/store",
        json={
            "user_id": user,
            "fact": fact,
            "fact_type": "preference",
            "importance_score": 0.8,
            "source": "genaug-cli-verify",
            "metadata": {"scenario": "project-verify", "verification_id": verification_id},
            "idempotency_key": f"genaug-verify-{project_id}-{user}-{verification_id}",
        },
        headers=headers,
    )
    memory_id = str(stored.get("memory_id") or stored.get("id") or "")
    checks.append(_check("memory_store", bool(memory_id), memory_id or "missing memory_id"))

    search = client.app(
        "POST",
        "/api/v1/agent/memory/search",
        json={
            "user_id": user,
            "query": "concise onboarding notes",
            "limit": 5,
            "min_similarity": 0,
            "fact_type": "preference",
            "min_importance": 0.5,
            "source": "genaug-cli-verify",
        },
        headers=headers,
    )
    facts = search.get("facts", []) if isinstance(search, dict) else []
    found_memory = _memory_hit_found(facts, memory_id)
    checks.append(_check("memory_search", found_memory, f"{len(facts)} facts"))

    profile = client.app(
        "GET",
        f"/api/v1/agent/memory/profile/{encode_path_segment(user)}",
        headers=headers,
    )
    total_facts = profile.get("total_facts") if isinstance(profile, dict) else None
    checks.append(
        _check("memory_profile", isinstance(total_facts, int), f"total_facts={total_facts}")
    )
    checks.append(
        _run_memory_response_recall(
            client,
            project_id=project_id,
            user=user,
            recall_marker=recall_marker,
        )
        if memory_id
        else _skip("memory_response_recall", "skipped because memory_store failed")
    )

    if memory_id:
        deleted = client.app(
            "DELETE",
            f"/api/v1/agent/memory/{encode_path_segment(memory_id)}",
            params={"user_id": user},
            headers=headers,
        )
        deleted_count = deleted.get("deleted_count") if isinstance(deleted, dict) else None
        checks.append(
            _check(
                "memory_delete",
                isinstance(deleted_count, int) and deleted_count >= 1,
                f"deleted_count={deleted_count}",
            )
        )
    else:
        checks.append(_check("memory_delete", False, "skipped because memory_store failed"))
    return checks


def _run_memory_response_recall(
    client: Any,
    *,
    project_id: str,
    user: str,
    recall_marker: str,
) -> dict[str, str]:
    """Ask the normal responses path to recall the synthetic memory marker."""

    try:
        response = client.app(
            "POST",
            "/v1/responses",
            json={
                "model": "balanced",
                "user": user,
                "input": (
                    "What is my stored CLI verification memory marker? "
                    "Reply with only the marker."
                ),
                "metadata": {
                    "source": "genaug-cli-verify",
                    "feature": "memory_response_recall",
                },
            },
            headers={"X-Project-ID": project_id},
        )
    except CLIError as exc:
        return _check("memory_response_recall", False, str(exc))
    if not isinstance(response, dict):
        return _check("memory_response_recall", False, str(response))
    response_id = str(response.get("id") or "")
    status = str(response.get("status") or "")
    text = _response_text(response)
    if recall_marker.lower() in text.lower():
        return _check("memory_response_recall", True, response_id or "marker recalled")
    return _skip(
        "memory_response_recall",
        response_id or status or "response completed but marker was not found",
    )


def _memory_hit_found(facts: object, memory_id: str) -> bool:
    """Return whether a memory search result includes the stored memory."""

    if not isinstance(facts, list) or not facts:
        return False
    if not memory_id:
        return True
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if str(fact.get("memory_id") or fact.get("id") or "") == memory_id:
            return True
    return False
