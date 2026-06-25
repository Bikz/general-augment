"""Shared General Augment readiness checklist helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONTRACT_PATH = Path(__file__).with_name("readiness_contract.json")
_READINESS_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

READINESS_SCHEMA_VERSION = str(_READINESS_CONTRACT["schema_version"])
READINESS_CHECKS: tuple[dict[str, str], ...] = tuple(
    {
        "key": str(item["key"]),
        "label": str(item["label"]),
        "description": str(item["description"]),
    }
    for item in _READINESS_CONTRACT["checks"]
)

CHECK_MAPPING: dict[str, tuple[str, ...]] = {
    "api_ready": ("api_ready",),
    "project_created": ("project_resolved",),
    "project_key_created": ("project_api_key",),
    "project_key_execution": ("project_key_execution",),
    "first_response_passed": ("agent_test",),
    "run_timeline_visible": ("run_timeline_inspect",),
    "tools_configured": ("tool_registry",),
    "runtime_policy_visible": ("runtime_policy_model_routing",),
    "tenant_behavior_configured": (),
    "memory_tested": ("memory_store", "memory_search", "memory_profile", "memory_delete"),
    "memory_response_recall": ("memory_response_recall",),
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
    if key == "tenant_behavior_configured":
        prompt = str(project.get("system_prompt") or "").strip()
        skills = project.get("skill_contents") or project.get("skills") or []
        skill_count = len(skills) if isinstance(skills, list) else 0
        soul_check = checks.get("soul_visible")
        skills_check = checks.get("skills_visible")
        prompt_specific = len(prompt) >= 80 and " ".join(prompt.lower().split()) not in {
            "you are a helpful assistant.",
            "you are a helpful assistant for this project.",
            "you are a helpful agent.",
        }
        soul_visible = soul_check is not None and soul_check.get("status") == "PASS"
        skills_visible = skills_check is not None and skills_check.get("status") == "PASS"
        status = (
            "PASS"
            if prompt_specific or skill_count > 0 or soul_visible or skills_visible
            else "SKIP"
        )
        detail = (
            f"system_prompt_chars={len(prompt)}; project_skill_count={skill_count}"
            if status == "PASS"
            else "configure SOUL.md or add a project skill"
        )
        return _item(
            definition,
            status,
            detail,
            source_checks=["soul_visible", "skills_visible"],
        )

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
