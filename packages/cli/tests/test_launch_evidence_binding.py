from __future__ import annotations

from datetime import UTC, datetime

from platform_cli.launch_evidence_binding import bind_application_observability
from platform_cli.launch_verification import (
    check_result,
    correlate_application_checks,
)


def _control_plane_evidence(
    *,
    project_id: str = "project-fixture-1",
    response_id: str = "resp-visible-1",
) -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "project_id": project_id,
        "latest_run": {
            "id": "run-visible-1",
            "response_id": response_id,
            "trace_id": "trace-visible-1",
            "status": "completed",
            "completed_at": now,
        },
        "usage": {
            "event_count": 2,
            "run_id": "run-visible-1",
            "response_id": response_id,
            "input_tokens": 12,
            "output_tokens": 8,
            "latest_at": now,
            "completed_at": now,
        },
    }


def test_binding_returns_exact_links_for_visible_application_response() -> None:
    binding = bind_application_observability(
        _control_plane_evidence(),
        response_id="resp-visible-1",
        project_id="project-fixture-1",
        launch_session_id="launch-fixture-1",
        dashboard_base_url="https://app.example.test",
    )

    assert binding.trace_passed is True
    assert binding.usage_passed is True
    assert binding.trace_reason_code == "trace_visibility_application_response_bound"
    assert binding.usage_reason_code == "usage_visibility_application_response_bound"
    assert binding.trace_evidence["url"] == (
        "https://app.example.test/dashboard/observability?response_id=resp-visible-1"
        "&trace_id=trace-visible-1&project_id=project-fixture-1"
    )
    assert binding.trace_evidence["dashboard_url"] == (
        "https://app.example.test/dashboard/projects/project-fixture-1/launch/launch-fixture-1"
    )
    assert binding.usage_evidence["url"] == (
        "https://app.example.test/dashboard/projects/project-fixture-1/usage"
    )


def test_binding_fails_when_usage_points_to_a_different_response() -> None:
    payload = _control_plane_evidence()
    usage = payload["usage"]
    assert isinstance(usage, dict)
    usage["response_id"] = "resp-verifier-probe"

    binding = bind_application_observability(
        payload,
        response_id="resp-visible-1",
        project_id="project-fixture-1",
        launch_session_id="launch-fixture-1",
    )

    assert binding.trace_passed is True
    assert binding.usage_passed is False
    assert binding.usage_reason_code == (
        "usage_visibility_application_response_binding_missing"
    )


def test_correlator_replaces_probe_results_with_visible_application_evidence() -> None:
    project_id = "project-fixture-1"
    payload = _control_plane_evidence(project_id=project_id)

    class Client:
        def installer(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            return payload

    manifest = {
        "x-general-augment-launch": {
            "capabilities": [
                {
                    "name": "habit_list_context",
                    "classification": "read_only",
                    "execution_owner": "application",
                    "source": {"kind": "app_owned_context"},
                }
            ]
        }
    }
    checks = [
        check_result(
            "trace_visibility",
            "FAIL",
            "trace_visibility_application_response_binding_missing",
            "A generic verifier probe is insufficient.",
            producer="hosted",
        ),
        check_result(
            "usage_visibility",
            "FAIL",
            "usage_visibility_application_response_binding_missing",
            "A generic verifier probe is insufficient.",
            producer="hosted",
        ),
        check_result(
            "read_only_application_capability",
            "PASS",
            "read_only_application_capability_passed",
            "Browser proof awaiting hosted correlation.",
            evidence=[
                {
                    "artifact_sha256": "a" * 64,
                    "capability": "habit_list_context",
                    "classification": "read_only",
                    "execution_owner": "application",
                    "identity_binding": "authenticated_server_user",
                    "response_id": "resp-visible-1",
                    "verification_attempt_id": "attempt-fixture-1",
                }
            ],
            producer="repository_claim",
        ),
    ]

    correlated = correlate_application_checks(
        Client(),
        checks,
        installer_token="installer-synthetic",
        project_id=project_id,
        artifact={"session_id": "launch-fixture-1"},
        manifest=manifest,
        dashboard_base_url="https://app.example.test",
    )
    by_name = {row["name"]: row for row in correlated}

    assert by_name["read_only_application_capability"]["status"] == "PASS"
    assert by_name["trace_visibility"]["status"] == "PASS"
    assert by_name["usage_visibility"]["status"] == "PASS"
    assert by_name["trace_visibility"]["producer"] == "hosted_correlated"
    assert by_name["trace_visibility"]["evidence"][0]["response_id"] == "resp-visible-1"


def test_correlator_cannot_pass_observability_without_browser_response_id() -> None:
    class Client:
        def installer(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise AssertionError("installer evidence must not be queried")

    checks = [
        check_result(
            "trace_visibility",
            "FAIL",
            "trace_visibility_application_response_binding_missing",
            "Missing browser binding.",
            producer="hosted",
        ),
        check_result(
            "usage_visibility",
            "FAIL",
            "usage_visibility_application_response_binding_missing",
            "Missing browser binding.",
            producer="hosted",
        ),
        check_result(
            "read_only_application_capability",
            "PASS",
            "read_only_application_capability_passed",
            "Incomplete browser proof.",
            evidence=[{"capability": "habit_list_context"}],
            producer="repository_claim",
        ),
    ]

    correlated = correlate_application_checks(
        Client(),
        checks,
        installer_token="installer-synthetic",
        project_id="project-fixture-1",
        artifact={"session_id": "launch-fixture-1"},
        manifest={"x-general-augment-launch": {"capabilities": []}},
    )
    by_name = {row["name"]: row for row in correlated}

    assert by_name["trace_visibility"]["status"] == "FAIL"
    assert by_name["usage_visibility"]["status"] == "FAIL"
