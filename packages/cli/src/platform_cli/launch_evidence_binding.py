"""Bind launch verification to one user-visible application response.

The generic runtime probes prove that a credential and the hosted runtime work. They
must not be used to certify dashboard trace or usage visibility, because a customer
needs those links for the response they actually saw in the application.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from platform_cli.self_serve import (
    dashboard_launch_url,
    dashboard_observability_url,
    dashboard_project_section_url,
)

MAX_BINDING_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class ApplicationObservabilityBinding:
    """Secret-free result of correlating one app response to control-plane evidence."""

    trace_passed: bool
    usage_passed: bool
    trace_reason_code: str
    usage_reason_code: str
    trace_evidence: dict[str, Any]
    usage_evidence: dict[str, Any]


def bind_application_observability(
    payload: Mapping[str, Any] | None,
    *,
    response_id: str,
    project_id: str,
    launch_session_id: str,
    dashboard_base_url: str | None = None,
    now: datetime | None = None,
) -> ApplicationObservabilityBinding:
    """Require trace and usage evidence for the exact browser-visible response."""

    current = now or datetime.now(UTC)
    mapping = payload if isinstance(payload, Mapping) else {}
    run_value = mapping.get("latest_run")
    run = run_value if isinstance(run_value, Mapping) else {}
    usage_value = mapping.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}

    run_id = str(run.get("id") or "")
    trace_id = str(run.get("trace_id") or "")
    run_response_id = str(run.get("response_id") or "")
    project_matches = str(mapping.get("project_id") or "") == project_id
    trace_passed = (
        bool(response_id)
        and project_matches
        and run_response_id == response_id
        and bool(run_id)
        and bool(trace_id)
        and run.get("status") in {"completed", "complete"}
        and _fresh(run.get("completed_at") or run.get("created_at"), current)
    )

    usage_run_id = str(usage.get("run_id") or "")
    usage_response_id = str(usage.get("response_id") or "")
    event_count = usage.get("event_count")
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    token_total = (
        input_tokens + output_tokens
        if isinstance(input_tokens, int) and isinstance(output_tokens, int)
        else 0
    )
    usage_passed = (
        trace_passed
        and usage_response_id == response_id
        and usage_run_id == run_id
        and isinstance(event_count, int)
        and event_count > 0
        and token_total > 0
        and _fresh(usage.get("latest_at"), current)
        and _fresh(usage.get("completed_at"), current)
    )

    review_url = dashboard_launch_url(
        project_id,
        launch_session_id,
        base_url=dashboard_base_url,
    )
    trace_url = dashboard_observability_url(
        project=project_id,
        filters={"response_id": response_id, "trace_id": trace_id},
        base_url=dashboard_base_url,
    )
    usage_url = dashboard_project_section_url(
        project_id,
        "usage",
        base_url=dashboard_base_url,
    )
    return ApplicationObservabilityBinding(
        trace_passed=trace_passed,
        usage_passed=usage_passed,
        trace_reason_code=(
            "trace_visibility_application_response_bound"
            if trace_passed
            else "trace_visibility_application_response_binding_missing"
        ),
        usage_reason_code=(
            "usage_visibility_application_response_bound"
            if usage_passed
            else "usage_visibility_application_response_binding_missing"
        ),
        trace_evidence={
            "response_id": response_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "project_id": mapping.get("project_id"),
            "url": trace_url,
            "dashboard_url": review_url,
        },
        usage_evidence={
            "response_id": response_id,
            "run_id": usage_run_id,
            "project_id": mapping.get("project_id"),
            "usage_event_count": event_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "url": usage_url,
            "dashboard_url": review_url,
        },
    )


def _fresh(value: object, now: datetime) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age = now - parsed.astimezone(UTC)
    return timedelta(0) <= age <= MAX_BINDING_AGE
