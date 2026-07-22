from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from platform_cli.commands.certification import (
    EvidenceDocument,
    assemble_certification_receipt,
)
from platform_cli.errors import CLIError
from platform_cli.launch_verification import REQUIRED_BETA_CHECKS
from platform_cli.main import app

NOW = datetime(2026, 7, 16, 16, tzinfo=UTC)


def _payloads(*, checked_at: datetime = NOW) -> dict[str, dict[str, Any]]:
    timestamp = checked_at.isoformat()
    project_id = "project_123"
    session_id = "launch_123"
    key_id = "key_123"
    manifest_hash = "d" * 64
    manifest_fingerprint = "f" * 64
    session_fingerprint = "9" * 64
    verification_attempt_id = "attempt-123"
    identity = {
        "project_id": project_id,
        "agent_ids": ["agent_primary", "agent_specialist"],
        "runtime_key_id": key_id,
    }
    counts = {"projects": 1, "agents": 2, "active_runtime_keys": 1}
    provision = {
        "schema_version": "general-augment-provisioning-receipt/v1",
        "checked_at": timestamp,
        "session_id": session_id,
        "project_id": project_id,
        "manifest_sha256": manifest_hash,
        "runtime_key": {
            "id": key_id,
            "action": "created",
            "active_matching_count": 1,
        },
        "release": {"id": "release_123", "fingerprint": "release-fingerprint-123"},
    }
    dashboard = "https://app.example.test"
    return {
        "verification": {
            "schema_version": "general-augment-launch-verification/v1",
            "verdict": "READY",
            "verified_at": timestamp,
            "manifest_fingerprint": manifest_fingerprint,
            "dashboard_review_url": (
                f"{dashboard}/dashboard/projects/{project_id}/launch/{session_id}"
            ),
            "checks": [
                {
                    "name": name,
                    "required": True,
                    "status": "PASS",
                    "reason_code": f"{name}_passed",
                    "checked_at": timestamp,
                    "evidence": [
                        {
                            "response_id": "resp_123",
                            "run_id": "run_123",
                            "trace_id": "trace_123",
                            "url": (
                                f"{dashboard}/dashboard/observability?project_id={project_id}"
                                "&response_id=resp_123&trace_id=trace_123"
                            ),
                        }
                        if name == "trace_visibility"
                        else {
                            "response_id": "resp_123",
                            "run_id": "run_123",
                            "url": f"{dashboard}/dashboard/projects/{project_id}/usage",
                        }
                        if name == "usage_visibility"
                        else {
                            "verification_attempt_id": verification_attempt_id,
                        }
                        if name == "application_browser_smoke"
                        else {
                            "launch_session_id": session_id,
                            "manifest_fingerprint": session_fingerprint,
                            "project_id": project_id,
                        }
                        if name == "launch_session_approved"
                        else {"evidence_id": f"evidence-{name}"}
                    ],
                }
                for name in REQUIRED_BETA_CHECKS
            ],
        },
        "provision_first": provision,
        "provision_second": {
            **copy.deepcopy(provision),
            "runtime_key": {
                "id": key_id,
                "action": "reused",
                "active_matching_count": 1,
            },
        },
        "browser": {
            "schema_version": "general-augment-application-evidence/v1",
            "generated_at": timestamp,
            "verification_attempt_id": verification_attempt_id,
            "fixture_url": "https://fixture.example.test",
            "authentication_mode": "two_real_clerk_test_users",
            "approved_session_fingerprint": session_fingerprint,
            "manifest_fingerprint": manifest_fingerprint,
            "runtime_key_fingerprint": "a" * 64,
            "checks": [
                {
                    "name": name,
                    "required": True,
                    "status": "PASS",
                    "reason_code": f"{name}_verified",
                    "evidence": (
                        {
                            "response_id": "resp_123",
                            "trace_id": "trace_123",
                            "observed_event_sequence": [
                                "response.created",
                                "response.output_text.delta",
                                "response.completed",
                            ],
                        }
                        if name == "application_browser_smoke"
                        else {
                            "capability": "habit_list_context",
                            "classification": "read_only",
                            "execution_owner": "application",
                            "identity_binding": "authenticated_server_user",
                            "response_id": "resp_123",
                            "trace_id": "trace_123",
                        }
                        if name == "read_only_application_capability"
                        else {
                            "response_id": "resp_memory",
                            "trace_id": "trace_memory",
                            "marker_digest": "b" * 64,
                        }
                        if name == "memory_write_recall"
                        else {
                            "users_exercised": 2,
                            "marker_absent": True,
                            "response_id": "resp_isolation",
                            "trace_id": "trace_isolation",
                        }
                        if name == "cross_user_memory_isolation"
                        else {
                            "locations_scanned": [
                                "browser_requests",
                                "browser_responses",
                                "console",
                                "html",
                                "javascript_assets",
                                "local_storage",
                                "session_storage",
                                "script_visible_cookies",
                                "cache_storage",
                            ],
                            "runtime_key_fingerprint": "a" * 64,
                            "matches": 0,
                        }
                        if name == "secret_not_browser_visible"
                        else {
                            "unconfirmed_status": 409,
                            "confirmed_status": 200,
                            "persisted_across_requests": True,
                        }
                    ),
                }
                for name in (
                    "application_browser_smoke",
                    "read_only_application_capability",
                    "memory_write_recall",
                    "cross_user_memory_isolation",
                    "secret_not_browser_visible",
                    "app_owned_write_confirmation",
                )
            ],
        },
        "deployment": {
            "schema_version": "general-augment-hosted-deployment/v1",
            "checked_at": timestamp,
            "source": {"commit": "a" * 40, "branch": "main", "clean": True},
            "artifacts": {
                "cli_version": "0.3.0",
                "cli_wheel_sha256": "b" * 64,
                "skill_version": "1.1.2",
                "skill_sha256": "c" * 64,
                "manifest_sha256": manifest_hash,
                "api_image": {
                    "provider": "oci",
                    "digest": f"sha256:{'e' * 64}",
                    "build_sha": "a" * 40,
                },
                "worker_image": {
                    "provider": "oci",
                    "digest": f"sha256:{'e' * 64}",
                    "build_sha": "a" * 40,
                },
                "dashboard": {
                    "provider": "vercel",
                    "deployment_id": "dpl_certified",
                    "build_sha": "a" * 40,
                    "url": dashboard,
                },
                "fixture_image": None,
            },
            "deployment": {
                "class": "production",
                "namespace": "general-augment",
                "url_mode": "public_production",
                "api_url": "https://api.example.test",
                "dashboard_url": dashboard,
                "fixture_url": "https://fixture.example.test",
            },
            "identifiers": {
                "workspace_id": "workspace_123",
                "project_id": project_id,
                "launch_session_id": session_id,
                "release_id": "release_123",
                "runtime_key_id": key_id,
                "response_id": "resp_123",
                "run_id": "run_123",
                "trace_id": "trace_123",
            },
            "links": {
                "review_url": (f"{dashboard}/dashboard/projects/{project_id}/launch/{session_id}"),
                "trace_url": (
                    f"{dashboard}/dashboard/observability?project_id={project_id}"
                    "&response_id=resp_123&trace_id=trace_123"
                ),
                "usage_url": f"{dashboard}/dashboard/projects/{project_id}/usage",
            },
            "security": {
                "cli_config_mode": "0600",
                "application_env_mode": "0600",
                "secret_match_count": 0,
            },
            "idempotency": {
                "first": identity,
                "second": copy.deepcopy(identity),
                "counts_before": counts,
                "counts_after": copy.deepcopy(counts),
            },
            "cleanup": {
                "state": "pending",
                "cleaned_at": None,
                "runtime_key_revoked": False,
                "application_env_removed": False,
                "certification_stack_removed": False,
            },
        },
        "management_denial": {
            "schema_version": "general-augment-runtime-management-denial/v1",
            "checked_at": timestamp,
            "status": 403,
        },
    }


def _documents(payloads: dict[str, dict[str, Any]]) -> dict[str, EvidenceDocument]:
    timestamp_fields = {
        "verification": "verified_at",
        "provision_first": "checked_at",
        "provision_second": "checked_at",
        "finalization_first": "finalized_at",
        "finalization_second": "finalized_at",
        "browser": "generated_at",
        "deployment": "checked_at",
        "management_denial": "checked_at",
    }
    return {
        name: EvidenceDocument(
            payload=payload,
            sha256=hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
            schema_version=str(payload["schema_version"]),
            checked_at=str(payload[timestamp_fields[name]]),
        )
        for name, payload in payloads.items()
    }


def _with_finalization(
    payloads: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    provision = payloads["provision_first"]
    release = provision["release"]
    finalization = {
        "schema_version": "general-augment-launch-finalization/v1",
        "session_id": provision["session_id"],
        "project_id": provision["project_id"],
        "release_id": release["id"],
        "release_fingerprint": release["fingerprint"],
        "runtime_mode": "test",
        "runtime_key_id": "key_durable",
        "runtime_key_action": "created",
        "environment": {
            "status": "configured",
            "permission_mode": "0600",
            "variables": ["GENAUG_API_KEY", "GENAUG_PROJECT_ID", "GENAUG_API_BASE_URL"],
        },
        "finalized_at": payloads["deployment"]["checked_at"],
    }
    payloads["finalization_first"] = finalization
    payloads["finalization_second"] = {
        **copy.deepcopy(finalization),
        "runtime_key_action": "reused",
    }
    payloads["deployment"]["identifiers"]["runtime_key_id"] = "key_durable"
    for phase in ("first", "second"):
        payloads["deployment"]["idempotency"][phase]["runtime_key_id"] = "key_durable"
    return payloads


def _browser_evidence(payloads: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    return next(
        row["evidence"]
        for row in payloads["browser"]["checks"]
        if row["name"] == name
    )


def test_assembler_binds_all_inputs_and_vercel_provenance() -> None:
    receipt = assemble_certification_receipt(_documents(_payloads()), generated_at=NOW)

    assert receipt["verdict"] == "READY"
    assert len(receipt["checks"]) == 18
    assert receipt["artifacts"]["dashboard"]["provider"] == "vercel"
    assert set(receipt["evidence_artifacts"]) == {
        "verification",
        "provision_first",
        "provision_second",
        "browser",
        "deployment",
        "management_denial",
    }
    assert receipt["security"]["management_route_status"] == 403
    assert receipt["links"]["trace_url"].endswith(
        "project_id=project_123&response_id=resp_123&trace_id=trace_123"
    )


def test_assembler_binds_preview_provisioning_to_durable_finalization() -> None:
    receipt = assemble_certification_receipt(
        _documents(_with_finalization(_payloads())),
        generated_at=NOW,
    )

    assert receipt["identifiers"]["runtime_key_id"] == "key_durable"
    assert {"finalization_first", "finalization_second"} <= set(
        receipt["evidence_artifacts"]
    )


def test_assembler_rejects_non_idempotent_durable_finalization() -> None:
    payloads = _with_finalization(_payloads())
    payloads["finalization_second"]["runtime_key_id"] = "key_duplicate"

    with pytest.raises(CLIError, match="did not reuse exactly one runtime key"):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)


def test_assembler_rejects_stale_or_duplicate_required_checks() -> None:
    payloads = _payloads()
    payloads["browser"]["generated_at"] = (NOW - timedelta(hours=25)).isoformat()
    with pytest.raises(CLIError, match="older than 24 hours"):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)

    payloads = _payloads()
    payloads["verification"]["checks"][0] = copy.deepcopy(payloads["verification"]["checks"][1])
    with pytest.raises(CLIError, match="exactly once"):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)


def test_assembler_rejects_duplicate_runtime_key_and_unbound_trace() -> None:
    payloads = _payloads()
    payloads["provision_second"]["runtime_key"]["id"] = "key_duplicate"
    with pytest.raises(CLIError, match="duplicate runtime key"):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)

    payloads = _payloads()
    payloads["deployment"]["links"]["trace_url"] = (
        "https://app.example.test/dashboard/observability"
        "?project_id=project_other&response_id=resp_123&trace_id=trace_123"
    )
    with pytest.raises(CLIError, match="certified application run"):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)


def test_assembler_accepts_preview_rotation_followed_by_exact_reuse() -> None:
    payloads = _payloads()
    payloads["provision_first"]["runtime_key"]["action"] = "rotated"

    receipt = assemble_certification_receipt(_documents(payloads), generated_at=NOW)

    assert receipt["verdict"] == "READY"


@pytest.mark.parametrize(
    ("check_name", "field", "value", "error"),
    [
        (
            "read_only_application_capability",
            "identity_binding",
            "client_supplied_user",
            "capability evidence",
        ),
        ("memory_write_recall", "marker_digest", "", "memory evidence"),
        ("cross_user_memory_isolation", "users_exercised", 1, "cross-user isolation"),
        ("secret_not_browser_visible", "locations_scanned", [], "secret-scan"),
        ("app_owned_write_confirmation", "confirmed_status", 201, "app-owned write"),
    ],
)
def test_assembler_rejects_incomplete_browser_semantics(
    check_name: str,
    field: str,
    value: object,
    error: str,
) -> None:
    payloads = _payloads()
    _browser_evidence(payloads, check_name)[field] = value

    with pytest.raises(CLIError, match=error):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("verification_attempt_id", "attempt-other", "verification attempt"),
        ("manifest_fingerprint", "0" * 64, "verified manifest"),
        ("approved_session_fingerprint", "1" * 64, "approved launch session"),
    ],
)
def test_assembler_rejects_unbound_browser_artifact(
    field: str,
    value: str,
    error: str,
) -> None:
    payloads = _payloads()
    payloads["browser"][field] = value

    with pytest.raises(CLIError, match=error):
        assemble_certification_receipt(_documents(payloads), generated_at=NOW)


def test_certification_command_writes_owner_only_receipt(tmp_path: Path) -> None:
    payloads = _payloads(checked_at=datetime.now(UTC))
    args = ["certification", "create", "--workspace", str(tmp_path)]
    names = {
        "verification": "verification.json",
        "provision_first": "provision-first.json",
        "provision_second": "provision-second.json",
        "browser": "browser.json",
        "deployment": "deployment.json",
        "management_denial": "denial.json",
    }
    option_names = {
        "verification": "--verification",
        "provision_first": "--provision-first",
        "provision_second": "--provision-second",
        "browser": "--browser",
        "deployment": "--deployment",
        "management_denial": "--management-denial-evidence",
    }
    for name, filename in names.items():
        path = tmp_path / filename
        path.write_text(json.dumps(payloads[name]), encoding="utf-8")
        args.extend((option_names[name], str(path)))
    args.append("--json")

    result = CliRunner().invoke(app, args)

    assert result.exit_code == 0, result.output
    receipt_path = tmp_path / ".genaug" / "hosted-certification.json"
    assert receipt_path.is_file()
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert "READY" in result.output
