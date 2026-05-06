"""Shared General Augment readiness checklist helpers."""

from __future__ import annotations

from typing import Any

READINESS_SCHEMA_VERSION = "general-augment-readiness/v1"

READINESS_CHECKS: tuple[dict[str, str], ...] = (
    {
        "key": "api_ready",
        "label": "API ready",
        "description": "Hosted API readiness returned healthy dependencies.",
    },
    {
        "key": "project_created",
        "label": "Project created",
        "description": "The tenant project exists and can be resolved by the CLI.",
    },
    {
        "key": "project_key_created",
        "label": "Project key created",
        "description": "At least one project-scoped server API key exists.",
    },
    {
        "key": "project_key_execution",
        "label": "Project key execution",
        "description": "The configured project key can call /v1/responses.",
    },
    {
        "key": "first_response_passed",
        "label": "First response passed",
        "description": "A hosted test turn completed successfully.",
    },
    {
        "key": "tools_configured",
        "label": "Tools configured",
        "description": "The tool registry is reachable for this project.",
    },
    {
        "key": "runtime_policy_visible",
        "label": "Runtime policy visible",
        "description": "Tenant model routing and Hermes-facing policy are visible.",
    },
    {
        "key": "memory_tested",
        "label": "Memory tested",
        "description": "Memory store, search, profile, and delete checks passed.",
    },
    {
        "key": "trace_visible",
        "label": "Trace visible",
        "description": "Observability returned trace rows for follow-up debugging.",
    },
    {
        "key": "usage_limits_visible",
        "label": "Usage limits visible",
        "description": "Usage totals and plan limits are visible.",
    },
    {
        "key": "channel_status_known",
        "label": "Channel status known",
        "description": "Channel readiness state is visible for the project.",
    },
    {
        "key": "billing_state_known",
        "label": "Billing state known",
        "description": "The project exposes a billing or plan state.",
    },
)

CHECK_MAPPING: dict[str, tuple[str, ...]] = {
    "api_ready": ("api_ready",),
    "project_created": ("project_resolved",),
    "project_key_created": ("project_api_key",),
    "project_key_execution": ("project_key_execution",),
    "first_response_passed": ("agent_test",),
    "tools_configured": ("tool_registry",),
    "runtime_policy_visible": ("runtime_policy_model_routing",),
    "memory_tested": ("memory_store", "memory_search", "memory_profile", "memory_delete"),
    "trace_visible": ("observability",),
    "usage_limits_visible": ("usage", "usage_limits"),
    "channel_status_known": ("channel_status",),
}


def build_readiness_checklist(
    checks: list[dict[str, Any]],
    *,
    project: dict[str, Any],
) -> dict[str, Any]:
    """Build the canonical readiness checklist from raw verification checks."""

    by_name = {str(item.get("name")): item for item in checks}
    items = [
        _readiness_item(definition, by_name, project=project) for definition in READINESS_CHECKS
    ]
    return {
        "version": READINESS_SCHEMA_VERSION,
        "status": _checklist_status(items),
        "items": items,
    }


def _readiness_item(
    definition: dict[str, str],
    checks: dict[str, dict[str, Any]],
    *,
    project: dict[str, Any],
) -> dict[str, Any]:
    """Build one readiness item."""

    key = definition["key"]
    if key == "billing_state_known":
        plan = project.get("plan") or project.get("pricing_tier")
        status = "PASS" if plan else "SKIP"
        detail = f"plan={plan}" if plan else "project plan or pricing tier is not exposed"
        return _item(definition, status, detail, source_checks=[])

    source_names = CHECK_MAPPING[key]
    source_checks = [checks.get(name) for name in source_names]
    missing = [
        name for name, check in zip(source_names, source_checks, strict=True) if check is None
    ]
    present = [check for check in source_checks if check is not None]
    if missing:
        return _item(
            definition,
            "SKIP",
            f"not checked: {', '.join(missing)}",
            source_checks=list(source_names),
        )

    statuses = {str(check.get("status")) for check in present}
    if "FAIL" in statuses:
        status = "FAIL"
    elif statuses == {"PASS"}:
        status = "PASS"
    elif "SKIP" in statuses:
        status = "SKIP"
    else:
        status = "FAIL"
    detail = "; ".join(
        f"{check.get('name')}: {check.get('detail')}" for check in present if check.get("detail")
    )
    return _item(definition, status, detail, source_checks=list(source_names))


def _item(
    definition: dict[str, str],
    status: str,
    detail: str,
    *,
    source_checks: list[str],
) -> dict[str, Any]:
    """Return a single readiness item."""

    return {
        "key": definition["key"],
        "label": definition["label"],
        "description": definition["description"],
        "status": status,
        "detail": detail,
        "source_checks": source_checks,
    }


def _checklist_status(items: list[dict[str, Any]]) -> str:
    """Roll up readiness status."""

    statuses = {str(item.get("status")) for item in items}
    if "FAIL" in statuses:
        return "FAIL"
    if "SKIP" in statuses:
        return "PARTIAL"
    return "PASS"
