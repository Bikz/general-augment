from __future__ import annotations

import copy
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from platform_cli.errors import CLIError
from platform_cli.hosted_certification import (
    build_hosted_certification_receipt,
    validate_hosted_certification_receipt,
    write_hosted_certification_receipt,
)
from platform_cli.launch_verification import REQUIRED_BETA_CHECKS


def _receipt() -> dict[str, Any]:
    checked_at = "2026-07-16T12:00:00+00:00"
    identity = {
        "project_id": "project_123",
        "agent_ids": ["agent_primary", "agent_specialist"],
        "runtime_key_id": "key_123",
    }
    counts = {"projects": 1, "agents": 2, "active_runtime_keys": 1}
    return build_hosted_certification_receipt(
        generated_at=datetime(2026, 7, 16, 12, tzinfo=UTC),
        source={"commit": "a" * 40, "branch": "feat/hosted-certification", "clean": True},
        artifacts={
            "cli_version": "0.3.0",
            "cli_wheel_sha256": "b" * 64,
            "skill_version": "1.1.2",
            "skill_sha256": "c" * 64,
            "manifest_sha256": "d" * 64,
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
                "deployment_id": "dpl_fixture",
                "build_sha": "a" * 40,
                "url": "https://dashboard.example.test",
            },
            "fixture_image": None,
        },
        evidence_artifacts={
            name: {
                "schema_version": f"evidence/{name}",
                "sha256": value * 64,
                "checked_at": checked_at,
            }
            for name, value in zip(
                (
                    "verification",
                    "provision_first",
                    "provision_second",
                    "browser",
                    "deployment",
                    "management_denial",
                ),
                "123456",
                strict=True,
            )
        },
        deployment={
            "class": "isolated_hosted",
            "namespace": "general-augment-certification",
            "url_mode": "port_forward",
            "api_url": "http://127.0.0.1:18080",
            "dashboard_url": "http://127.0.0.1:13200",
            "fixture_url": None,
        },
        identifiers={
            "workspace_id": "workspace_123",
            "project_id": "project_123",
            "launch_session_id": "launch_123",
            "release_id": "release_123",
            "runtime_key_id": "key_123",
            "response_id": "resp_123",
            "run_id": "run_123",
            "trace_id": "trace_123",
        },
        links={
            "review_url": (
                "https://dashboard.example.test/dashboard/projects/project_123/launch/launch_123"
            ),
            "trace_url": (
                "https://dashboard.example.test/dashboard/observability"
                "?project_id=project_123&response_id=resp_123&trace_id=trace_123"
            ),
            "usage_url": "https://dashboard.example.test/dashboard/projects/project_123/usage",
        },
        checks=[
            {
                "name": name,
                "required": True,
                "status": "PASS",
                "reason_code": f"{name}_passed",
                "checked_at": checked_at,
                "evidence_ids": [f"evidence/{name}"],
            }
            for name in REQUIRED_BETA_CHECKS
        ],
        security={
            "management_route_status": 403,
            "management_route_checked_at": checked_at,
            "cli_config_mode": "0600",
            "application_env_mode": "0600",
            "secret_match_count": 0,
        },
        idempotency={
            "first": identity,
            "second": copy.deepcopy(identity),
            "counts_before": counts,
            "counts_after": copy.deepcopy(counts),
        },
        cleanup={
            "state": "pending",
            "cleaned_at": None,
            "runtime_key_revoked": False,
            "application_env_removed": False,
            "certification_stack_removed": False,
        },
    )


def test_receipt_requires_each_contractual_check_to_pass_once() -> None:
    receipt = _receipt()
    receipt["checks"][0]["status"] = "SKIP"
    with pytest.raises(CLIError, match="'PASS' was expected"):
        validate_hosted_certification_receipt(receipt)

    receipt = _receipt()
    receipt["checks"][0] = copy.deepcopy(receipt["checks"][1])
    with pytest.raises(CLIError, match="exactly once"):
        validate_hosted_certification_receipt(receipt)


def test_receipt_requires_clean_source_and_security_proofs() -> None:
    receipt = _receipt()
    receipt["source"]["clean"] = False
    with pytest.raises(CLIError, match="True was expected"):
        validate_hosted_certification_receipt(receipt)

    receipt = _receipt()
    receipt["security"]["management_route_status"] = 200
    with pytest.raises(CLIError, match="403 was expected"):
        validate_hosted_certification_receipt(receipt)


def test_receipt_requires_durable_finalization_evidence_as_a_pair() -> None:
    receipt = _receipt()
    receipt["evidence_artifacts"]["finalization_first"] = {
        "schema_version": "general-augment-launch-finalization/v1",
        "sha256": "7" * 64,
        "checked_at": receipt["generated_at"],
    }

    with pytest.raises(CLIError, match="both durable finalization receipts"):
        validate_hosted_certification_receipt(receipt)


def test_receipt_rejects_non_idempotent_rerun() -> None:
    receipt = _receipt()
    receipt["idempotency"]["second"]["runtime_key_id"] = "key_duplicate"
    with pytest.raises(CLIError, match="changed project, agent, or runtime-key"):
        validate_hosted_certification_receipt(receipt)

    receipt = _receipt()
    receipt["idempotency"]["counts_after"]["agents"] = 3
    with pytest.raises(CLIError, match="changed certification resource counts"):
        validate_hosted_certification_receipt(receipt)


@pytest.mark.parametrize(
    "value,error",
    [
        ("founder@example.com", "contains PII"),
        ("Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature", "secret-like value"),
        ("sk_live_not-a-real-secret-but-forbidden", "secret-like value"),
    ],
)
def test_receipt_rejects_pii_and_secret_like_values(value: str, error: str) -> None:
    receipt = _receipt()
    receipt["artifacts"]["skill_version"] = value
    with pytest.raises(CLIError, match=error):
        validate_hosted_certification_receipt(receipt)


def test_receipt_rejects_unsafe_urls_and_unknown_fields() -> None:
    receipt = _receipt()
    receipt["deployment"]["dashboard_url"] = (
        "https://user:password@example.test/path?token=redacted"
    )
    with pytest.raises(CLIError, match="URL may not contain credentials"):
        validate_hosted_certification_receipt(receipt)

    receipt = _receipt()
    receipt["security"]["api_key"] = "not-recorded"
    with pytest.raises(CLIError, match="Additional properties"):
        validate_hosted_certification_receipt(receipt)


def test_completed_cleanup_requires_all_controls() -> None:
    receipt = _receipt()
    receipt["cleanup"].update({"state": "complete", "cleaned_at": "2026-07-16T13:00:00+00:00"})
    with pytest.raises(CLIError, match="all cleanup controls"):
        validate_hosted_certification_receipt(receipt)


def test_receipt_accepts_oci_dashboard_provenance() -> None:
    receipt = _receipt()
    receipt["artifacts"]["dashboard"] = {
        "provider": "oci",
        "digest": f"sha256:{'f' * 64}",
        "build_sha": "a" * 40,
    }
    validate_hosted_certification_receipt(receipt)


def test_receipt_rejects_stale_evidence() -> None:
    receipt = _receipt()
    receipt["evidence_artifacts"]["browser"]["checked_at"] = "2026-07-14T12:00:00+00:00"
    with pytest.raises(CLIError, match="stale evidence"):
        validate_hosted_certification_receipt(receipt)


def test_receipt_write_is_owner_only(tmp_path: Path) -> None:
    target = write_hosted_certification_receipt(
        tmp_path / ".genaug" / "hosted-certification.json",
        _receipt(),
    )
    assert target.is_file()
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
