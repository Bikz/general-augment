"""Contractual verification for the one-prompt launch beta."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml

from platform_cli.errors import APIError, CLIError
from platform_cli.launch_evidence_binding import bind_application_observability
from platform_cli.secure_filesystem import (
    assert_no_symlink_components,
    atomic_write_text_no_follow,
    confined_path,
    read_text_no_follow,
)

CheckStatus = Literal["PASS", "FAIL", "SKIP"]

LAUNCH_VERIFICATION_SCHEMA_VERSION = "general-augment-launch-verification/v1"
APPLICATION_EVIDENCE_SCHEMA_VERSION = "general-augment-application-evidence/v1"
REQUIRED_BETA_CHECKS: tuple[str, ...] = (
    "cli_api_skill_manifest_compatibility",
    "launch_session_approved",
    "provisioning_idempotent",
    "runtime_key_scope",
    "runtime_key_execution",
    "non_streaming_response",
    "streaming_event_sequence",
    "stable_user_continuity",
    "memory_write_recall",
    "cross_user_memory_isolation",
    "read_only_application_capability",
    "trace_visibility",
    "usage_visibility",
    "application_typecheck_or_equivalent",
    "application_build",
    "application_browser_smoke",
    "secret_not_browser_visible",
    "rollback_documented",
)
MAX_EVIDENCE_AGE = timedelta(hours=24)
APPLICATION_COMMAND_TIMEOUT_SECONDS = 15 * 60
MAX_APPLICATION_EVIDENCE_BYTES = 256 * 1024
SUPPORTED_API_MINIMUM = (0, 1, 0)
SUPPORTED_API_MAXIMUM_EXCLUSIVE = (0, 2, 0)
_PASS_EVIDENCE_KEYS = frozenset(
    {
        "api_build",
        "api_version",
        "artifact_sha256",
        "browser_artifact_sha256",
        "capability",
        "checked_files",
        "cli_version",
        "command_sha256",
        "event_types",
        "exit_code",
        "identity_binding",
        "launch_session_id",
        "manifest_fingerprint",
        "manifest_schema_version",
        "memory_id",
        "project_id",
        "provisioning_receipt_sha256",
        "request_id",
        "response_id",
        "run_id",
        "input_tokens",
        "output_tokens",
        "runtime_key_id",
        "scope",
        "scopes",
        "skill_version",
        "trace_id",
        "usage_event_count",
        "url",
    }
)
_SENSITIVE_EVIDENCE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
)

EXPECTED_EVIDENCE_PRODUCERS: dict[str, str] = {
    "cli_api_skill_manifest_compatibility": "hosted",
    "launch_session_approved": "hosted",
    "provisioning_idempotent": "hosted",
    "runtime_key_scope": "hosted",
    "runtime_key_execution": "hosted",
    "non_streaming_response": "hosted",
    "streaming_event_sequence": "hosted",
    "stable_user_continuity": "hosted",
    "memory_write_recall": "hosted",
    "cross_user_memory_isolation": "hosted",
    "read_only_application_capability": "hosted_correlated",
    "trace_visibility": "hosted_correlated",
    "usage_visibility": "hosted_correlated",
    "application_typecheck_or_equivalent": "verifier_local",
    "application_build": "verifier_local",
    "application_browser_smoke": "verifier_local",
    "secret_not_browser_visible": "verifier_local",
    "rollback_documented": "verifier_local",
}


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Return a stable fingerprint for a parsed manifest without serializing secrets."""

    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def launch_session_fingerprint(artifact: Mapping[str, Any]) -> str:
    """Mirror the control-plane launch-session artifact fingerprint."""

    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def bind_application_command_contract(
    artifact: Mapping[str, Any],
    workspace: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind review approval to the declared verification commands.

    The coding agent applies the reviewed plan after approval, so application files and
    package scripts are intentionally not snapshot-bound here. Execution still resolves
    each current script through the strict canonical-command allowlist before invoking a
    trusted local tool entrypoint.
    """

    root = workspace.expanduser().resolve()
    contract = _application_command_contract(root, manifest)
    bound = dict(artifact)
    plan_value = bound.get("plan")
    plan = dict(plan_value) if isinstance(plan_value, Mapping) else {}
    plan["application_command_contract"] = {
        "sha256": hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "command_count": len(contract["commands"]),
        "file_count": len(contract["files"]),
    }
    bound["plan"] = plan
    session_identity = {key: value for key, value in bound.items() if key != "session_id"}
    digest = hashlib.sha256(
        json.dumps(
            session_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    bound["session_id"] = f"launch_{digest[:20]}"
    return bound


def application_command_contract_sha(
    workspace: Path,
    manifest: Mapping[str, Any],
) -> str:
    """Return the exact reviewed command contract for a workspace snapshot."""

    contract = _application_command_contract(workspace.expanduser().resolve(), manifest)
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _application_command_contract(
    workspace: Path,
    manifest: Mapping[str, Any],
) -> dict[str, list[dict[str, str]]]:
    """Describe the stable command intent that must remain identical after approval."""

    command_rows: list[dict[str, str]] = []
    for command in _application_commands(manifest):
        argv = shlex.split(command)
        command_rows.append(
            {
                "category": _declared_command_category(argv),
                "command_sha256": hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest(),
            }
        )
    return {"commands": command_rows, "files": []}


def _declared_command_category(argv: list[str]) -> str:
    """Classify manifest command intent without depending on pre-apply repository files."""

    normalized = [item.casefold() for item in argv]
    if len(normalized) == 3 and normalized[0] in {"npm", "pnpm", "bun"}:
        if normalized[1] != "run":
            return "unsupported"
        return {
            "typecheck": "typecheck",
            "build": "build",
            "test:e2e": "browser",
            "e2e": "browser",
        }.get(normalized[2], "unsupported")
    if len(normalized) == 2 and normalized[0] == "yarn":
        return {
            "typecheck": "typecheck",
            "build": "build",
            "test:e2e": "browser",
            "e2e": "browser",
        }.get(normalized[1], "unsupported")
    if _is_typescript_check(normalized):
        return "typecheck"
    if _is_next_build(normalized):
        return "build"
    if _is_playwright_command(normalized):
        return "browser"
    return "unsupported"


def check_result(
    name: str,
    status: CheckStatus,
    reason_code: str,
    detail: str,
    *,
    evidence: Iterable[Mapping[str, Any]] = (),
    checked_at: datetime | None = None,
    required: bool = True,
    producer: str = "unspecified",
) -> dict[str, Any]:
    """Build one secret-safe verification result."""

    rows = [_safe_evidence(dict(row)) for row in evidence]
    return {
        "name": name,
        "required": required,
        "status": status,
        "reason_code": reason_code,
        "detail": detail,
        "producer": producer,
        "evidence": rows,
        "checked_at": _isoformat(checked_at or datetime.now(UTC)),
    }


def _tag_producer(row: Mapping[str, Any], producer: str) -> dict[str, Any]:
    tagged = dict(row)
    tagged["producer"] = producer
    return tagged


def evaluate_launch_verification(
    manifest: Mapping[str, Any],
    provided_checks: Iterable[Mapping[str, Any]],
    *,
    optional_warnings: Iterable[str] = (),
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    """Normalize evidence into exactly one result per required beta check."""

    now = verified_at or datetime.now(UTC)
    grouped: dict[str, list[dict[str, Any]]] = {}
    optional: list[dict[str, Any]] = []
    for raw in provided_checks:
        row = dict(raw)
        name = str(row.get("name") or "")
        if name in REQUIRED_BETA_CHECKS:
            grouped.setdefault(name, []).append(row)
        elif name:
            optional.append(_normalize_optional(row, now))

    normalized: list[dict[str, Any]] = []
    for name in REQUIRED_BETA_CHECKS:
        candidates = grouped.get(name, [])
        if not candidates:
            normalized.append(
                check_result(
                    name,
                    "SKIP",
                    f"{name}_evidence_missing",
                    "Required beta evidence was not collected.",
                    checked_at=now,
                )
            )
            continue
        if len(candidates) > 1:
            normalized.append(
                check_result(
                    name,
                    "FAIL",
                    f"{name}_duplicate_results",
                    "The verifier produced more than one result for this required check.",
                    checked_at=now,
                )
            )
            continue
        normalized.append(_normalize_required(name, candidates[0], now))

    required_names = _manifest_required_checks(manifest)
    if required_names != list(REQUIRED_BETA_CHECKS):
        index = REQUIRED_BETA_CHECKS.index("cli_api_skill_manifest_compatibility")
        normalized[index] = check_result(
            "cli_api_skill_manifest_compatibility",
            "FAIL",
            "manifest_required_check_contract_mismatch",
            "The manifest required-check list does not match the beta contract.",
            evidence=[{"manifest_fingerprint": manifest_fingerprint(manifest)}],
            checked_at=now,
        )

    warning_codes = sorted({str(code) for code in optional_warnings if str(code)})
    warning_codes.extend(
        str(row["reason_code"])
        for row in optional
        if row["status"] in {"FAIL", "SKIP"}
    )
    blocking = [row for row in normalized if row["status"] != "PASS"]
    verdict = "BLOCKED" if blocking else ("READY_WITH_WARNINGS" if warning_codes else "READY")
    reason_codes = sorted({str(row["reason_code"]) for row in blocking} | set(warning_codes))
    return {
        "schema_version": LAUNCH_VERIFICATION_SCHEMA_VERSION,
        "verdict": verdict,
        "reason_codes": reason_codes,
        "verified_at": _isoformat(now),
        "manifest_fingerprint": manifest_fingerprint(manifest),
        "checks": normalized,
        "optional_checks": optional,
    }


def collect_application_checks(
    workspace: Path,
    manifest: Mapping[str, Any],
    *,
    runtime_api_key: str | None,
    runtime_api_base_url: str | None = None,
    project_id: str | None = None,
    approved_command_contract_sha: str,
    launch_fingerprint_value: str,
    manifest_fingerprint_value: str,
    timeout_seconds: int = APPLICATION_COMMAND_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run declared local commands and collect browser/capability/secret evidence."""

    root = workspace.expanduser().resolve()
    commands = _application_commands(manifest)
    verification_attempt_id = uuid.uuid4().hex
    current_contract_sha = application_command_contract_sha(root, manifest)
    if not approved_command_contract_sha or current_contract_sha != approved_command_contract_sha:
        checks = _command_contract_mismatch_checks(manifest, root, runtime_api_key)
        return checks, {
            "schema_version": "general-augment-application-command-receipts/v1",
            "workspace": str(root),
            "verification_attempt_id": verification_attempt_id,
            "command_contract_matches": False,
            "receipts": [],
        }
    receipts = [
        _run_application_command(
            root,
            command,
            manifest=manifest,
            approved_command_contract_sha=approved_command_contract_sha,
            timeout_seconds=timeout_seconds,
            verification_attempt_id=verification_attempt_id,
            launch_fingerprint_value=launch_fingerprint_value,
            manifest_fingerprint_value=manifest_fingerprint_value,
            runtime_api_key=runtime_api_key,
            runtime_api_base_url=runtime_api_base_url,
            project_id=project_id,
        )
        for command in commands
    ]
    checks = [
        _tag_producer(
            _command_check(
            "application_typecheck_or_equivalent",
            receipts,
            categories=("typecheck",),
            label="typecheck, test, or lint",
            ),
            "verifier_local",
        ),
        _tag_producer(
            _command_check(
            "application_build",
            receipts,
            categories=("build",),
            label="build",
            ),
            "verifier_local",
        ),
        _tag_producer(
            _command_check(
            "application_browser_smoke",
            receipts,
            categories=("browser",),
            label="browser",
            ),
            "verifier_local",
        ),
    ]
    browser_receipts = [item for item in receipts if item["category"] == "browser"]
    checks.append(
        _tag_producer(
            _capability_check_from_application_evidence(
            root,
            manifest,
            browser_receipts=browser_receipts,
            ),
            "repository_claim",
        )
    )
    checks.append(
        _tag_producer(
            _browser_secret_check(root, runtime_api_key, browser_receipts=browser_receipts),
            "verifier_local",
        )
    )
    checks.append(_tag_producer(_rollback_check(manifest), "verifier_local"))
    return checks, {
        "schema_version": "general-augment-application-command-receipts/v1",
        "workspace": str(root),
        "verification_attempt_id": verification_attempt_id,
        "command_contract_matches": True,
        "receipts": receipts,
    }


def _command_contract_mismatch_checks(
    manifest: Mapping[str, Any],
    workspace: Path,
    runtime_api_key: str | None,
) -> list[dict[str, Any]]:
    """Fail local execution checks without running repository-controlled code."""

    checks = [
        _tag_producer(
            check_result(
                name,
                "FAIL",
                "application_command_contract_mismatch",
                "Application commands or their reviewed configuration changed after approval.",
            ),
            "verifier_local",
        )
        for name in (
            "application_typecheck_or_equivalent",
            "application_build",
            "application_browser_smoke",
        )
    ]
    checks.append(
        _tag_producer(
            check_result(
                "read_only_application_capability",
                "SKIP",
                "read_only_application_capability_blocked_by_command_contract",
                (
                    "Capability verification did not run because the approved command "
                    "contract changed."
                ),
            ),
            "repository_claim",
        )
    )
    checks.append(
        _tag_producer(
            _browser_secret_check(workspace, runtime_api_key),
            "verifier_local",
        )
    )
    checks.append(_tag_producer(_rollback_check(manifest), "verifier_local"))
    return checks


def collect_hosted_preflight_checks(
    client: Any,
    *,
    installer_token: str | None,
    project_id: str,
    artifact: Mapping[str, Any],
    compatible: bool,
    compatibility_reasons: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prove compatibility and exact dashboard approval before repository execution."""

    ready = _safe_call(lambda: client.public("GET", "/health/ready"))
    compatibility_check = _compatibility_check(
        compatible=compatible,
        compatibility_reasons=compatibility_reasons,
        artifact=artifact,
        ready=ready,
    )
    ready_mapping = ready if isinstance(ready, dict) else {}
    safe_artifact: dict[str, Any] = {
        "api": {
            "status": ready_mapping.get("status"),
            "version": ready_mapping.get("version"),
            "build_sha": ready_mapping.get("build_sha"),
        }
    }
    if compatibility_check["status"] != "PASS":
        return [_tag_producer(compatibility_check, "hosted")], safe_artifact

    session: dict[str, Any] | None = None
    if installer_token:
        session_payload = _safe_call(
            lambda: client.installer(
                "GET",
                (
                    f"/projects/{_path_segment(project_id)}/launch-sessions/"
                    f"{_path_segment(str(artifact.get('session_id') or ''))}"
                ),
                token=installer_token,
            )
        )
        session = session_payload if isinstance(session_payload, dict) else None
    approval = _launch_session_check(session, artifact, installer_token=installer_token)
    safe_artifact["launch_session"] = {
        "session_id": session.get("session_id") if session else None,
        "project_id": session.get("project_id") if session else None,
        "status": session.get("status") if session else None,
        "fingerprint_matches": (
            str(session.get("fingerprint") or "") == launch_session_fingerprint(artifact)
            if session
            else False
        ),
    }
    return [
        _tag_producer(compatibility_check, "hosted"),
        _tag_producer(approval, "hosted"),
    ], safe_artifact


def collect_hosted_checks(
    client: Any,
    *,
    installer_token: str | None,
    runtime_api_key: str | None,
    runtime_key_id: str | None = None,
    runtime_key_scopes: Iterable[str] = (),
    project_id: str,
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: Path,
    provisioning_receipt_path: Path,
    compatible: bool,
    compatibility_reasons: Iterable[str],
    preflight_checks: Iterable[Mapping[str, Any]] | None = None,
    preflight_artifact: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Exercise hosted installer and runtime boundaries and return secret-free evidence."""

    checks: list[dict[str, Any]] = []
    safe_artifact: dict[str, Any] = {
        "schema_version": "general-augment-hosted-verification-evidence/v1",
        "project_id": project_id,
    }
    if preflight_checks is None:
        resolved_preflight, resolved_artifact = collect_hosted_preflight_checks(
            client,
            installer_token=installer_token,
            project_id=project_id,
            artifact=artifact,
            compatible=compatible,
            compatibility_reasons=compatibility_reasons,
        )
    else:
        resolved_preflight = [dict(row) for row in preflight_checks]
        resolved_artifact = dict(preflight_artifact or {})
    checks.extend(resolved_preflight)
    safe_artifact.update(resolved_artifact)
    if any(row.get("status") != "PASS" for row in resolved_preflight):
        existing_names = {str(row.get("name") or "") for row in checks}
        checks.extend(
            _tag_producer(
                check_result(
                    name,
                    "SKIP",
                    f"{name}_blocked_by_launch_preflight",
                    "Hosted verification did not run before compatibility and approval passed.",
                ),
                "hosted",
            )
            for name in (
                "launch_session_approved",
                "provisioning_idempotent",
                "runtime_key_scope",
                "runtime_key_execution",
                "non_streaming_response",
                "streaming_event_sequence",
                "stable_user_continuity",
                "memory_write_recall",
                "cross_user_memory_isolation",
                "trace_visibility",
                "usage_visibility",
            )
            if name not in existing_names
        )
        return checks, safe_artifact

    session_payload = _safe_call(
        lambda: client.installer(
            "GET",
            (
                f"/projects/{_path_segment(project_id)}/launch-sessions/"
                f"{_path_segment(str(artifact.get('session_id') or ''))}"
            ),
            token=installer_token,
        )
    )
    session = session_payload if isinstance(session_payload, dict) else None

    control_before: dict[str, Any] | None = None
    if installer_token:
        payload = _safe_call(
            lambda: client.installer(
                "GET",
                f"/projects/{_path_segment(project_id)}/verification-evidence",
                token=installer_token,
            )
        )
        control_before = payload if isinstance(payload, dict) else None
    provisioning = _load_provisioning_receipt(provisioning_receipt_path)
    checks.append(
        _provisioning_idempotence_check(
            provisioning,
            session=session,
            control=control_before,
            runtime_key_id=runtime_key_id,
            artifact=artifact,
            manifest=manifest,
            manifest_path=manifest_path,
            project_id=project_id,
            receipt_path=provisioning_receipt_path,
        )
    )
    checks.append(
        _runtime_scope_check(
            provisioning,
            control_before,
            runtime_api_key_present=bool(runtime_api_key),
            runtime_key_id=runtime_key_id,
            runtime_key_scopes=runtime_key_scopes,
        )
    )

    runtime_checks, runtime_artifact = _runtime_execution_checks(
        client,
        project_id=project_id,
        runtime_api_key=runtime_api_key,
    )
    checks.extend(runtime_checks)
    safe_artifact["runtime"] = runtime_artifact

    # Runtime probes establish API health, but they are not the response a customer
    # saw in their app. Observability remains blocked until the browser evidence is
    # correlated below through installer-authenticated control-plane evidence.
    checks.extend(_unbound_application_observability_checks())
    return [_tag_producer(row, "hosted") for row in checks], safe_artifact


def correlate_application_checks(
    client: Any,
    checks: Iterable[Mapping[str, Any]],
    *,
    installer_token: str | None,
    project_id: str,
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    dashboard_base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Bind browser claims, trace, and usage to one installer-authenticated app run."""

    resolved = [dict(row) for row in checks]
    for index, row in enumerate(resolved):
        if row.get("name") != "read_only_application_capability" or row.get("status") != "PASS":
            continue
        evidence_rows = row.get("evidence")
        first = evidence_rows[0] if isinstance(evidence_rows, list) and evidence_rows else {}
        response_id = str(first.get("response_id") or "") if isinstance(first, dict) else ""
        if not installer_token or not response_id:
            resolved[index] = check_result(
                "read_only_application_capability",
                "FAIL",
                "read_only_application_capability_hosted_correlation_missing",
                "Capability evidence requires installer-authenticated hosted correlation.",
                producer="hosted_correlated",
            )
            continue
        payload = _safe_call(
            lambda response_id_=response_id: client.installer(
                "GET",
                f"/projects/{_path_segment(project_id)}/verification-evidence",
                params={"response_id": response_id_},
                token=installer_token,
            )
        )
        mapping = payload if isinstance(payload, dict) else {}
        latest = mapping.get("latest_run")
        run = latest if isinstance(latest, dict) else {}
        capability_value = mapping.get("application_capability")
        capability_proof = capability_value if isinstance(capability_value, dict) else {}
        capability_name = str(first.get("capability") or "") if isinstance(first, dict) else ""
        launch = manifest.get("x-general-augment-launch")
        reviewed_capabilities = launch.get("capabilities") if isinstance(launch, dict) else None
        reviewed = (
            next(
                (
                    item
                    for item in reviewed_capabilities
                    if isinstance(item, dict) and str(item.get("name") or "") == capability_name
                ),
                None,
            )
            if isinstance(reviewed_capabilities, list)
            else None
        )
        reviewed_source = reviewed.get("source") if isinstance(reviewed, dict) else None
        run_correlated = (
            str(mapping.get("project_id") or "") == project_id
            and str(run.get("response_id") or "") == response_id
            and bool(run.get("id"))
            and bool(run.get("trace_id"))
            and run.get("status") in {"complete", "completed"}
            and evidence_is_fresh(run.get("completed_at") or run.get("created_at"))
        )
        platform_tool_correlated = (
            run_correlated
            and str(capability_proof.get("response_id") or "") == response_id
            and str(capability_proof.get("run_id") or "") == str(run.get("id") or "")
            and str(capability_proof.get("capability") or "") == capability_name
            and capability_proof.get("classification") == "read_only"
            and capability_proof.get("identity_binding") == "authenticated_server_user"
            and capability_proof.get("evidence_source") == "platform_tool_execution"
            and bool(capability_proof.get("step_id"))
        )
        app_context_correlated = (
            run_correlated
            and isinstance(reviewed, dict)
            and reviewed.get("classification") == "read_only"
            and reviewed.get("execution_owner") == "application"
            and isinstance(reviewed_source, dict)
            and reviewed_source.get("kind") == "app_owned_context"
            and isinstance(first, dict)
            and first.get("classification") == "read_only"
            and first.get("execution_owner") == "application"
            and first.get("identity_binding") == "authenticated_server_user"
            and bool(first.get("artifact_sha256"))
            and bool(first.get("verification_attempt_id"))
        )
        correlated = platform_tool_correlated or app_context_correlated
        evidence_source = (
            "platform_tool_execution"
            if platform_tool_correlated
            else "cli_verified_application_context"
            if app_context_correlated
            else None
        )
        resolved[index] = check_result(
            "read_only_application_capability",
            "PASS" if correlated else "FAIL",
            (
                "read_only_application_capability_passed"
                if correlated
                else "read_only_application_capability_hosted_correlation_failed"
            ),
            (
                "Installer evidence links the application capability to a current project run."
                if correlated
                else "The claimed capability response was not found in current project evidence."
            ),
            evidence=[
                {
                    "capability": first.get("capability") if isinstance(first, dict) else None,
                    "identity_binding": (
                        first.get("identity_binding") if isinstance(first, dict) else None
                    ),
                    "response_id": response_id,
                    "run_id": run.get("id"),
                    "step_id": capability_proof.get("step_id"),
                    "trace_id": run.get("trace_id"),
                    "project_id": mapping.get("project_id"),
                    "evidence_source": evidence_source,
                }
            ]
            if response_id
            else [],
            producer="hosted_correlated",
        )
        resolved = _replace_application_observability_checks(
            resolved,
            mapping,
            response_id=response_id,
            project_id=project_id,
            launch_session_id=str(artifact.get("session_id") or ""),
            dashboard_base_url=dashboard_base_url,
        )
    return resolved


def _replace_application_observability_checks(
    checks: list[dict[str, Any]],
    payload: Mapping[str, Any] | None,
    *,
    response_id: str,
    project_id: str,
    launch_session_id: str,
    dashboard_base_url: str | None,
) -> list[dict[str, Any]]:
    """Replace generic probe results with evidence for the browser-visible response."""

    binding = bind_application_observability(
        payload,
        response_id=response_id,
        project_id=project_id,
        launch_session_id=launch_session_id,
        dashboard_base_url=dashboard_base_url,
    )
    replacements = {
        "trace_visibility": check_result(
            "trace_visibility",
            "PASS" if binding.trace_passed else "FAIL",
            binding.trace_reason_code,
            (
                "The dashboard trace links to the exact response shown by the application."
                if binding.trace_passed
                else "The application response is not bound to a current dashboard trace."
            ),
            evidence=[binding.trace_evidence],
            producer="hosted_correlated",
        ),
        "usage_visibility": check_result(
            "usage_visibility",
            "PASS" if binding.usage_passed else "FAIL",
            binding.usage_reason_code,
            (
                "The project usage page includes metering for the exact application run."
                if binding.usage_passed
                else "The application response is not bound to current metered usage."
            ),
            evidence=[binding.usage_evidence],
            producer="hosted_correlated",
        ),
    }
    return [replacements.get(str(row.get("name") or ""), row) for row in checks]


def _compatibility_check(
    *,
    compatible: bool,
    compatibility_reasons: Iterable[str],
    artifact: Mapping[str, Any],
    ready: object,
) -> dict[str, Any]:
    reasons = sorted({str(item) for item in compatibility_reasons if str(item)})
    ready_mapping = ready if isinstance(ready, dict) else {}
    api_ready = ready_mapping.get("status") in {"ok", "ready"}
    api_version = str(ready_mapping.get("version") or "")
    parsed_api_version = _version_tuple(api_version)
    api_compatible = (
        parsed_api_version is not None
        and SUPPORTED_API_MINIMUM
        <= parsed_api_version
        < SUPPORTED_API_MAXIMUM_EXCLUSIVE
    )
    api_build = str(ready_mapping.get("build_sha") or ready_mapping.get("version") or "")
    passed = compatible and not reasons and api_ready and api_compatible and bool(api_build)
    if not api_ready or not api_build:
        hosted_reason = "hosted_api_compatibility_unproven"
    elif not api_compatible:
        hosted_reason = "hosted_api_version_incompatible"
    else:
        hosted_reason = "cli_api_skill_manifest_compatibility_passed"
    return check_result(
        "cli_api_skill_manifest_compatibility",
        "PASS" if passed else "FAIL",
        (
            "cli_api_skill_manifest_compatibility_passed"
            if passed
            else (reasons[0] if reasons else hosted_reason)
        ),
        (
            "CLI, hosted API, launch skill, and manifest schema are compatible."
            if passed
            else (
                "Version compatibility is unproven. Upgrade the CLI or hosted API to "
                "the documented compatible range before retrying."
            )
        ),
        evidence=[
            {
                "cli_version": artifact.get("cli_version"),
                "skill_version": artifact.get("skill_version"),
                "manifest_schema_version": artifact.get("manifest_schema_version"),
                "api_version": api_version or None,
                "api_build": api_build or None,
            }
        ],
    )


def _launch_session_check(
    session: Mapping[str, Any] | None,
    artifact: Mapping[str, Any],
    *,
    installer_token: str | None,
) -> dict[str, Any]:
    if not installer_token:
        return check_result(
            "launch_session_approved",
            "SKIP",
            "launch_session_installer_auth_missing",
            "Installer auth is required to verify dashboard approval.",
        )
    if not isinstance(session, Mapping):
        return check_result(
            "launch_session_approved",
            "FAIL",
            "launch_session_evidence_unavailable",
            "The installer control plane did not return the launch session.",
        )
    expected_fingerprint = launch_session_fingerprint(artifact)
    actual_fingerprint = str(session.get("fingerprint") or "")
    status = str(session.get("status") or "")
    fresh = evidence_is_fresh(session.get("created_at"))
    passed = status == "approved" and actual_fingerprint == expected_fingerprint and fresh
    if status != "approved":
        reason = "launch_session_not_approved"
    elif actual_fingerprint != expected_fingerprint:
        reason = "approved_plan_fingerprint_mismatch"
    elif not fresh:
        reason = "launch_session_evidence_stale"
    else:
        reason = "launch_session_approved_passed"
    return check_result(
        "launch_session_approved",
        "PASS" if passed else "FAIL",
        reason,
        (
            "Dashboard approval is current and bound to the exact launch plan."
            if passed
            else "Dashboard approval is absent, stale, or bound to another plan."
        ),
        evidence=[
            {
                "launch_session_id": session.get("session_id"),
                "manifest_fingerprint": actual_fingerprint or None,
                "project_id": session.get("project_id"),
            }
        ]
        if actual_fingerprint
        else [],
    )


def _load_provisioning_receipt(path: Path) -> dict[str, Any] | None:
    root = path.expanduser().absolute().parent.parent
    try:
        resolved = confined_path(root, path, description="provisioning receipt")
        content = read_text_no_follow(root, resolved, description="provisioning receipt")
    except (CLIError, OSError, UnicodeDecodeError):
        return None
    if content is None or len(content.encode("utf-8")) > 128 * 1024:
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _provisioning_idempotence_check(
    receipt: Mapping[str, Any] | None,
    *,
    session: Mapping[str, Any] | None,
    control: Mapping[str, Any] | None,
    runtime_key_id: str | None,
    artifact: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_path: Path,
    project_id: str,
    receipt_path: Path,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        return check_result(
            "provisioning_idempotent",
            "SKIP",
            "provisioning_receipt_missing",
            "Rerun provisioning to generate a current idempotence receipt.",
        )
    runtime_key = receipt.get("runtime_key")
    key = runtime_key if isinstance(runtime_key, dict) else {}
    expected_session_fingerprint = launch_session_fingerprint(artifact)
    remote_fingerprint = str(session.get("fingerprint") or "") if session else ""
    root = receipt_path.expanduser().absolute().parent.parent
    manifest_content = read_text_no_follow(root, manifest_path, description="launch manifest path")
    receipt_content = read_text_no_follow(root, receipt_path, description="provisioning receipt")
    if manifest_content is None or receipt_content is None:
        return check_result(
            "provisioning_idempotent",
            "FAIL",
            "provisioning_receipt_path_invalid",
            "Provisioning evidence could not be read safely inside the workspace.",
        )
    expected_manifest_sha = hashlib.sha256(manifest_content.encode("utf-8")).hexdigest()
    authorities = receipt.get("authorities")
    authority_mapping = authorities if isinstance(authorities, dict) else {}
    runtime_keys_value = control.get("runtime_keys") if isinstance(control, Mapping) else None
    runtime_keys = runtime_keys_value if isinstance(runtime_keys_value, list) else []
    current_keys = [
        item
        for item in runtime_keys
        if isinstance(item, dict)
        and str(item.get("id") or "") == str(runtime_key_id or key.get("id") or "")
        and sorted(str(scope) for scope in item.get("scopes", [])) == ["responses:create"]
    ]
    valid = (
        receipt.get("schema_version") == "general-augment-provisioning-receipt/v1"
        and receipt.get("session_id") == artifact.get("session_id")
        and str(receipt.get("project_id") or "") == project_id
        and str(receipt.get("approved_plan_fingerprint") or "")
        == expected_session_fingerprint
        == remote_fingerprint
        and str(receipt.get("manifest_sha256") or "") == expected_manifest_sha
        and key.get("action") == "reused"
        and key.get("active_matching_count") == 1
        and bool(key.get("id"))
        and len(current_keys) == 1
        and bool(authority_mapping)
        and evidence_is_fresh(receipt.get("checked_at"))
    )
    digest = hashlib.sha256(receipt_content.encode("utf-8")).hexdigest()
    return check_result(
        "provisioning_idempotent",
        "PASS" if valid else "FAIL",
        "provisioning_idempotent_passed" if valid else "provisioning_idempotence_unproven",
        (
            "A provisioning rerun recorded one key and current state contains "
            "one configured runtime key."
            if valid
            else "Provisioning must be rerun and reuse exactly one key for the approved plan."
        ),
        evidence=[
            {
                "provisioning_receipt_sha256": digest,
                "runtime_key_id": runtime_key_id or key.get("id"),
                "manifest_fingerprint": manifest_fingerprint(manifest),
                "project_id": project_id,
            }
        ],
    )


def _runtime_scope_check(
    receipt: Mapping[str, Any] | None,
    control: Mapping[str, Any] | None,
    *,
    runtime_api_key_present: bool,
    runtime_key_id: str | None = None,
    runtime_key_scopes: Iterable[str] = (),
) -> dict[str, Any]:
    receipt_key_value = receipt.get("runtime_key") if isinstance(receipt, Mapping) else None
    receipt_key = receipt_key_value if isinstance(receipt_key_value, dict) else {}
    key_id = str(runtime_key_id or receipt_key.get("id") or "")
    configured_scopes = sorted(str(item) for item in runtime_key_scopes)
    if not configured_scopes:
        configured_scopes = sorted(str(item) for item in receipt_key.get("scopes", []))
    runtime_keys_value = control.get("runtime_keys") if isinstance(control, Mapping) else None
    runtime_keys = runtime_keys_value if isinstance(runtime_keys_value, list) else []
    remote = next(
        (
            item
            for item in runtime_keys
            if isinstance(item, dict) and str(item.get("id") or "") == key_id
        ),
        None,
    )
    remote_scopes = sorted(str(item) for item in remote.get("scopes", [])) if remote else []
    passed = (
        runtime_api_key_present
        and bool(key_id)
        and configured_scopes == ["responses:create"]
        and remote_scopes == ["responses:create"]
        and receipt_key.get("active_matching_count") == 1
    )
    return check_result(
        "runtime_key_scope",
        "PASS" if passed else "FAIL",
        "runtime_key_scope_passed" if passed else "runtime_key_scope_invalid",
        (
            "The one active runtime key has only responses:create."
            if passed
            else "Runtime-key presence, identity, or exact minimal scope is unproven."
        ),
        evidence=[{"runtime_key_id": key_id, "scopes": remote_scopes}] if key_id else [],
    )


def _runtime_execution_checks(
    client: Any,
    *,
    project_id: str,
    runtime_api_key: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    names = (
        "runtime_key_execution",
        "non_streaming_response",
        "streaming_event_sequence",
        "stable_user_continuity",
        "memory_write_recall",
        "cross_user_memory_isolation",
    )
    if not runtime_api_key:
        return (
            [
                check_result(
                    name,
                    "SKIP",
                    f"{name}_runtime_key_missing",
                    "The application runtime key is not configured.",
                )
                for name in names
            ],
            {},
        )
    runtime_app = getattr(client, "runtime_app", None)
    runtime_stream = getattr(client, "runtime_response_event_stream", None)
    if not callable(runtime_app) or not callable(runtime_stream):
        return (
            [
                check_result(
                    name,
                    "SKIP",
                    f"{name}_client_incompatible",
                    "This CLI client cannot exercise the separated runtime credential.",
                )
                for name in names
            ],
            {},
        )

    probe_id = uuid.uuid4().hex[:16]
    user_a = f"genaug-launch:{probe_id}:user-a"
    user_b = f"genaug-launch:{probe_id}:user-b"
    marker = f"genaug-launch-memory-{probe_id}"
    headers = {"X-Project-ID": project_id}
    checks: list[dict[str, Any]] = []
    artifact: dict[str, Any] = {"probe_id": probe_id}

    response, response_failure = _runtime_probe(
        lambda: runtime_app(
            "POST",
            "/v1/responses",
            json={
                "model": "balanced",
                "user": user_a,
                "input": "Reply with a short confirmation for launch verification.",
                "metadata": {"source": "genaug-launch-verify", "probe_id": probe_id},
            },
        )
    )
    response_mapping = response if isinstance(response, dict) else {}
    response_id = str(response_mapping.get("id") or "")
    completed = response_mapping.get("status") in {None, "", "complete", "completed"}
    runtime_passed = bool(response_id) and completed
    artifact["non_streaming_response_id"] = response_id
    response_evidence = (
        [{"response_id": response_id, "project_id": project_id}] if response_id else []
    )
    checks.append(
        check_result(
            "runtime_key_execution",
            "PASS" if runtime_passed else "FAIL",
            (
                "runtime_key_execution_passed"
                if runtime_passed
                else _runtime_failure_reason(
                    "runtime_key_execution",
                    response_failure,
                    "runtime_key_execution_failed",
                )
            ),
            (
                "The runtime credential executed an app-facing response."
                if runtime_passed
                else _runtime_failure_detail(
                    response_failure,
                    "The runtime credential did not complete an app-facing response.",
                )
            ),
            evidence=response_evidence,
        )
    )
    checks.append(
        check_result(
            "non_streaming_response",
            "PASS" if runtime_passed and bool(_response_text(response_mapping)) else "FAIL",
            (
                "non_streaming_response_passed"
                if runtime_passed and bool(_response_text(response_mapping))
                else _runtime_failure_reason(
                    "non_streaming_response",
                    response_failure,
                    "non_streaming_response_incomplete",
                )
            ),
            _runtime_failure_detail(
                response_failure,
                "The non-streaming response returned an ID and assistant text.",
            ),
            evidence=response_evidence,
        )
    )

    stream_payload = {
        "model": "balanced",
        "user": user_a,
        "input": "Stream a short confirmation for launch verification.",
        "stream": True,
        "metadata": {"source": "genaug-launch-verify", "probe_id": probe_id},
    }
    stream_result, stream_failure = _runtime_probe(
        lambda: list(runtime_stream(json=stream_payload))
    )
    stream_events = stream_result if isinstance(stream_result, list) else []
    event_names = [_event_name(event) for event in stream_events]
    stream_passed = _valid_response_event_sequence(event_names)
    artifact["stream_event_types"] = event_names
    checks.append(
        check_result(
            "streaming_event_sequence",
            "PASS" if stream_passed else "FAIL",
            (
                "streaming_event_sequence_passed"
                if stream_passed
                else _runtime_failure_reason(
                    "streaming_event_sequence",
                    stream_failure,
                    "streaming_event_sequence_invalid",
                )
            ),
            (
                "The stream emitted created, text delta, and completed events in order."
                if stream_passed
                else _runtime_failure_detail(
                    stream_failure,
                    "The runtime stream did not emit the required semantic event sequence.",
                )
            ),
            evidence=[{"event_types": event_names}] if event_names else [],
        )
    )

    memory_id = ""
    recall_response_id = ""
    memory_recalled = False
    cross_user_isolated = False
    store_failure: str | None = None
    own_search_failure: str | None = None
    other_search_failure: str | None = None
    recall_failure: str | None = None
    other_recall_failure: str | None = None
    try:
        stored, store_failure = _runtime_probe(
            lambda: runtime_app(
                "POST",
                "/api/v1/agent/memory/store",
                json={
                    "user_id": user_a,
                    "fact": f"My onboarding note code is {marker}.",
                    "fact_type": "preference",
                    "importance_score": 0.8,
                    "source": "genaug-launch-verify",
                    "idempotency_key": f"genaug-launch-{project_id}-{probe_id}",
                },
                headers=headers,
            )
        )
        memory_id = (
            str(stored.get("memory_id") or stored.get("id") or "")
            if isinstance(stored, dict)
            else ""
        )
        own_search, own_search_failure = _runtime_probe(
            lambda: runtime_app(
                "POST",
                "/api/v1/agent/memory/search",
                json={"user_id": user_a, "query": marker, "limit": 5, "min_similarity": 0},
                headers=headers,
            )
        )
        own_found = _facts_include_memory(own_search, memory_id, marker)
        recall, recall_failure = _runtime_probe(
            lambda: runtime_app(
                "POST",
                "/v1/responses",
                json={
                    "model": "balanced",
                    "user": user_a,
                    "input": "Return my onboarding note code only.",
                    "metadata": {
                        "source": "genaug-launch-verify",
                        "feature": "stable-user-memory-recall",
                        "probe_id": probe_id,
                    },
                },
            )
        )
        recall_mapping = recall if isinstance(recall, dict) else {}
        recall_response_id = str(recall_mapping.get("id") or "")
        memory_recalled = (
            not any((store_failure, own_search_failure, recall_failure))
            and own_found
            and marker.casefold() in _response_text(recall_mapping).casefold()
        )
        other_recall, other_recall_failure = _runtime_probe(
            lambda: runtime_app(
                "POST",
                "/v1/responses",
                json={
                    "model": "balanced",
                    "user": user_b,
                    "input": "Return any onboarding note code you remember.",
                    "metadata": {
                        "source": "genaug-launch-verify",
                        "feature": "cross-user-memory-isolation",
                        "probe_id": probe_id,
                    },
                },
            )
        )
        other_mapping = other_recall if isinstance(other_recall, dict) else {}
        # The runtime request creates the second stable application user. Search only
        # after that boundary exists; an unknown user is correctly a 404, not evidence
        # that an existing user's memory is isolated.
        other_search, other_search_failure = _runtime_probe(
            lambda: runtime_app(
                "POST",
                "/api/v1/agent/memory/search",
                json={"user_id": user_b, "query": marker, "limit": 5, "min_similarity": 0},
                headers=headers,
            )
        )
        other_found = _facts_include_memory(other_search, memory_id, marker)
        cross_user_isolated = (
            not any((store_failure, other_search_failure, other_recall_failure))
            and not other_found
            and marker.casefold() not in _response_text(other_mapping).casefold()
        )
    finally:
        if memory_id:
            _safe_call(
                lambda: runtime_app(
                    "DELETE",
                    f"/api/v1/agent/memory/{_path_segment(memory_id)}",
                    params={"user_id": user_a},
                    headers=headers,
                )
            )
    memory_evidence = [
        {"memory_id": memory_id, "response_id": recall_response_id, "project_id": project_id}
    ] if memory_id else []
    memory_failure = next(
        (
            failure
            for failure in (store_failure, own_search_failure, recall_failure)
            if failure
        ),
        None,
    )
    isolation_failure = next(
        (
            failure
            for failure in (store_failure, other_search_failure, other_recall_failure)
            if failure
        ),
        None,
    )
    checks.append(
        check_result(
            "stable_user_continuity",
            "PASS" if memory_recalled and bool(recall_response_id) else "FAIL",
            (
                "stable_user_continuity_passed"
                if memory_recalled
                else _runtime_failure_reason(
                    "stable_user_continuity",
                    memory_failure,
                    "stable_user_continuity_failed",
                )
            ),
            (
                "A later response for the same stable app user recalled the stored marker."
                if memory_recalled and recall_response_id
                else _runtime_failure_detail(
                    memory_failure,
                    (
                        "A later response for the same stable app user did not recall "
                        "the stored marker."
                    ),
                )
            ),
            evidence=memory_evidence,
        )
    )
    checks.append(
        check_result(
            "memory_write_recall",
            "PASS" if memory_recalled else "FAIL",
            (
                "memory_write_recall_passed"
                if memory_recalled
                else _runtime_failure_reason(
                    "memory_write_recall",
                    memory_failure,
                    "memory_write_recall_failed",
                )
            ),
            (
                "The runtime stored, searched, and recalled one synthetic user memory."
                if memory_recalled
                else _runtime_failure_detail(
                    memory_failure,
                    (
                        "The runtime did not complete store, search, and response recall "
                        "for one synthetic user memory."
                    ),
                )
            ),
            evidence=memory_evidence,
        )
    )
    checks.append(
        check_result(
            "cross_user_memory_isolation",
            "PASS" if cross_user_isolated else "FAIL",
            (
                "cross_user_memory_isolation_passed"
                if cross_user_isolated
                else _runtime_failure_reason(
                    "cross_user_memory_isolation",
                    isolation_failure,
                    "cross_user_memory_isolation_failed",
                )
            ),
            _runtime_failure_detail(
                isolation_failure,
                "A second synthetic app user could not search or recall the first user's marker.",
            ),
            evidence=[{"memory_id": memory_id, "project_id": project_id}] if memory_id else [],
        )
    )
    artifact["memory_id"] = memory_id
    artifact["recall_response_id"] = recall_response_id
    return checks, artifact


def _unbound_application_observability_checks() -> list[dict[str, Any]]:
    """Fail closed until browser evidence identifies the visible app response."""

    return [
        check_result(
            "trace_visibility",
            "FAIL",
            "trace_visibility_application_response_binding_missing",
            "Trace visibility requires the response ID produced by the application browser run.",
        ),
        check_result(
            "usage_visibility",
            "FAIL",
            "usage_visibility_application_response_binding_missing",
            "Usage visibility requires the response ID produced by the application browser run.",
        ),
    ]


def write_verification_receipt(
    path: Path,
    payload: Mapping[str, Any],
    *,
    workspace: Path | None = None,
) -> Path:
    """Persist one secret-free, owner-readable launch-verification receipt."""

    root = (workspace or path.expanduser().absolute().parent.parent).expanduser().resolve()
    resolved = confined_path(root, path, description="verification receipt")
    atomic_write_text_no_follow(
        root,
        resolved,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        description="verification receipt",
        mode=0o600,
    )
    return resolved


def _normalize_required(
    name: str,
    raw: Mapping[str, Any],
    now: datetime,
) -> dict[str, Any]:
    status = str(raw.get("status") or "SKIP").upper()
    if status not in {"PASS", "FAIL", "SKIP"}:
        return check_result(
            name,
            "FAIL",
            f"{name}_invalid_status",
            "Verification status must be PASS, FAIL, or SKIP.",
            checked_at=now,
        )
    producer = str(raw.get("producer") or "unspecified")
    expected_producer = EXPECTED_EVIDENCE_PRODUCERS.get(name)
    if status == "PASS" and expected_producer and producer != expected_producer:
        return check_result(
            name,
            "FAIL",
            f"{name}_unauthorized_evidence_producer",
            f"Required PASS evidence must come from {expected_producer}.",
            checked_at=now,
            producer="verifier_policy",
        )
    evidence = raw.get("evidence")
    evidence_rows = list(evidence) if isinstance(evidence, list) else []
    checked_at = _parse_datetime(raw.get("checked_at"))
    if status == "PASS" and not evidence_is_fresh(raw.get("checked_at"), now=now):
        return check_result(
            name,
            "FAIL",
            f"{name}_evidence_stale",
            "PASS evidence must have a current verification timestamp.",
            checked_at=now,
        )
    if status == "PASS" and not _has_verifiable_evidence(evidence_rows):
        return check_result(
            name,
            "FAIL",
            f"{name}_unverifiable_pass_evidence",
            "PASS requires a non-sensitive evidence identifier, URL, digest, or command receipt.",
            checked_at=now,
        )
    return check_result(
        name,
        status,  # type: ignore[arg-type]
        str(raw.get("reason_code") or f"{name}_{status.casefold()}"),
        str(raw.get("detail") or "No detail supplied."),
        evidence=(row for row in evidence_rows if isinstance(row, dict)),
        checked_at=checked_at or now,
        producer=producer,
    )


def _normalize_optional(raw: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    name = str(raw.get("name") or "optional")
    normalized = _normalize_required(name, raw, now)
    normalized["required"] = False
    return normalized


def _manifest_required_checks(manifest: Mapping[str, Any]) -> list[str]:
    launch = manifest.get("x-general-augment-launch")
    if not isinstance(launch, dict):
        return []
    verification = launch.get("verification")
    if not isinstance(verification, dict):
        return []
    checks = verification.get("required_checks")
    return [str(item) for item in checks] if isinstance(checks, list) else []


def _has_verifiable_evidence(rows: list[object]) -> bool:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if any(key in row and row[key] not in (None, "", [], {}) for key in _PASS_EVIDENCE_KEYS):
            return True
    return False


def _safe_evidence(value: dict[str, Any]) -> dict[str, Any]:
    sensitive = _first_sensitive_evidence_key(value)
    if sensitive:
        raise ValueError(f"Sensitive verification evidence field is forbidden: {sensitive}")
    return value


def _first_sensitive_evidence_key(value: object, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            current = f"{path}.{key}" if path else str(key)
            if normalized in _SENSITIVE_EVIDENCE_KEYS or normalized.endswith("_api_key"):
                return current
            nested = _first_sensitive_evidence_key(item, current)
            if nested:
                return nested
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = _first_sensitive_evidence_key(item, f"{path}[{index}]")
            if nested:
                return nested
    return None


def _application_commands(manifest: Mapping[str, Any]) -> list[str]:
    launch = manifest.get("x-general-augment-launch")
    verification = launch.get("verification") if isinstance(launch, dict) else None
    commands = verification.get("application_commands") if isinstance(verification, dict) else None
    if not isinstance(commands, list):
        return []
    return [str(command).strip() for command in commands if str(command).strip()]


def _run_application_command(
    workspace: Path,
    command: str,
    *,
    manifest: Mapping[str, Any],
    approved_command_contract_sha: str,
    timeout_seconds: int,
    verification_attempt_id: str,
    launch_fingerprint_value: str,
    manifest_fingerprint_value: str,
    runtime_api_key: str | None = None,
    runtime_api_base_url: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    argv = shlex.split(command)
    digest = hashlib.sha256("\0".join(argv).encode("utf-8")).hexdigest()
    category, canonical_argv = _canonical_command(workspace, argv)
    before_artifacts = _verification_artifact_state(workspace, category)
    started = datetime.now(UTC)
    started_clock = time.monotonic()
    exit_code: int
    stdout = b""
    stderr = b""
    outcome = "completed"
    browser_runtime_evidence: dict[str, Any] = {
        "browser_runtime_evidence_complete": False,
        "browser_secret_match": False,
        "browser_runtime_artifact_sha256": "",
        "browser_trace_count": 0,
    }
    try:
        if not argv:
            raise ValueError("empty command")
        if (
            not approved_command_contract_sha
            or application_command_contract_sha(workspace, manifest)
            != approved_command_contract_sha
        ):
            raise PermissionError("approved command contract changed")
        trusted_argv = _trusted_command_argv(workspace, category, canonical_argv)
        if trusted_argv is None:
            raise PermissionError("unsupported verification command")
        with (
            tempfile.TemporaryDirectory(prefix="genaug-playwright-")
            if category == "browser"
            else nullcontext(None)
        ) as browser_output_value:
            browser_output = Path(browser_output_value) if browser_output_value else None
            if browser_output is not None:
                trusted_argv = [
                    *trusted_argv,
                    f"--output={browser_output}",
                    "--trace=on",
                ]
            command_environment = {
                **os.environ,
                "PATH": _sanitized_path(workspace),
                "GENAUG_VERIFICATION_ATTEMPT_ID": verification_attempt_id,
                "GENAUG_LAUNCH_SESSION_FINGERPRINT": launch_fingerprint_value,
                "GENAUG_MANIFEST_FINGERPRINT": manifest_fingerprint_value,
            }
            if (
                category == "browser"
                and runtime_api_key
                and runtime_api_base_url
                and project_id
            ):
                # The reviewed browser check must exercise the provisioned hosted
                # runtime, not a local mock. These values stay in the child process
                # environment and are never copied into command receipts or output.
                command_environment.update(
                    {
                        "GENAUG_E2E_MODE": "hosted",
                        "GENAUG_API_KEY": runtime_api_key,
                        "GENAUG_API_BASE_URL": runtime_api_base_url.rstrip("/"),
                        "GENAUG_PROJECT_ID": project_id,
                    }
                )
            completed = subprocess.run(
                trusted_argv,
                cwd=workspace,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
                env=command_environment,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            if browser_output is not None:
                browser_runtime_evidence = _browser_runtime_evidence(
                    browser_output,
                    runtime_api_key=runtime_api_key,
                    stdout=stdout,
                    stderr=stderr,
                    command_succeeded=exit_code == 0,
                )
                completed_at = datetime.now(UTC)
                semantic_evidence, evidence_digest = _semantic_command_evidence(
                    workspace,
                    category,
                    before_artifacts,
                    started,
                    completed_at,
                    exit_code=exit_code,
                    outcome=outcome,
                    command_sha256=digest,
                    browser_output=browser_output,
                )
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _bytes(exc.stdout)
        stderr = _bytes(exc.stderr)
        outcome = "timeout"
    except (FileNotFoundError, ValueError):
        exit_code = 127
        outcome = "not_executable"
    except PermissionError:
        exit_code = 126
        outcome = "unsupported"
    completed_at = datetime.now(UTC)
    if category != "browser" or "semantic_evidence" not in locals():
        semantic_evidence, evidence_digest = _semantic_command_evidence(
            workspace,
            category,
            before_artifacts,
            started,
            completed_at,
            exit_code=exit_code,
            outcome=outcome,
            command_sha256=digest,
        )
    return {
        "category": category,
        "executable": Path(argv[0]).name if argv else "missing",
        "command_sha256": digest,
        "exit_code": exit_code,
        "outcome": outcome,
        "semantic_evidence": semantic_evidence,
        "verification_attempt_id": verification_attempt_id,
        "duration_ms": round((time.monotonic() - started_clock) * 1000),
        "started_at": _isoformat(started),
        "completed_at": _isoformat(completed_at),
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "evidence_artifact_sha256": evidence_digest,
        **browser_runtime_evidence,
    }


def _command_category(workspace: Path, argv: list[str]) -> str:
    """Return the category for an exact canonical beta command."""

    return _canonical_command(workspace, argv)[0]


def _canonical_command(workspace: Path, argv: list[str]) -> tuple[str, list[str] | None]:
    """Resolve package-script aliases without executing package-manager lifecycle hooks."""

    normalized = [item.casefold() for item in argv]
    resolved_script = _resolved_package_script(workspace, argv)
    if len(normalized) == 3 and normalized[0] in {"npm", "pnpm", "bun"}:
        if normalized[1] != "run":
            return "unsupported", None
        script = normalized[2]
        resolved_argv = shlex.split(resolved_script) if resolved_script else []
        resolved = [item.casefold() for item in resolved_argv]
        if script in {"test:e2e", "e2e"} and _is_playwright_command(resolved):
            return "browser", resolved_argv
        if script == "typecheck" and _is_typescript_check(resolved):
            return "typecheck", resolved_argv
        if script == "build" and _is_next_build(resolved):
            return "build", resolved_argv
    if len(normalized) == 2 and normalized[0] == "yarn":
        resolved_argv = shlex.split(resolved_script) if resolved_script else []
        resolved = [item.casefold() for item in resolved_argv]
        if normalized[1] in {"test:e2e", "e2e"} and _is_playwright_command(resolved):
            return "browser", resolved_argv
        if normalized[1] == "typecheck" and _is_typescript_check(resolved):
            return "typecheck", resolved_argv
        if normalized[1] == "build" and _is_next_build(resolved):
            return "build", resolved_argv
    if _is_typescript_check(normalized):
        return "typecheck", argv
    if _is_next_build(normalized):
        return "build", argv
    if _is_playwright_command(normalized):
        return "browser", argv
    return "unsupported", None


def _resolved_package_script(workspace: Path, argv: list[str]) -> str | None:
    if not argv:
        return None
    normalized = [item.casefold() for item in argv]
    script_name: str | None = None
    if len(normalized) == 3 and normalized[0] in {"npm", "pnpm", "bun"} and normalized[1] == "run":
        script_name = argv[2]
    elif len(normalized) == 2 and normalized[0] == "yarn":
        script_name = argv[1]
    if script_name is None:
        return None
    package_path = workspace / "package.json"
    try:
        assert_no_symlink_components(workspace, package_path, description="package manifest")
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (CLIError, OSError, json.JSONDecodeError):
        return None
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    value = scripts.get(script_name) if isinstance(scripts, dict) else None
    return value if isinstance(value, str) else None


def _is_typescript_check(argv: list[str]) -> bool:
    return argv == ["tsc", "--noemit"]


def _is_next_build(argv: list[str]) -> bool:
    return argv == ["next", "build"]


def _is_playwright_command(argv: list[str]) -> bool:
    return argv == ["playwright", "test"]


def _trusted_command_argv(
    workspace: Path,
    category: str,
    canonical_argv: list[str] | None,
) -> list[str] | None:
    """Invoke fixed CLI entrypoints through trusted Node, never npm scripts or shims."""

    if canonical_argv is None:
        return None
    node = shutil.which("node", path=_sanitized_path(workspace))
    if not node:
        return None
    node_path = Path(node).expanduser().resolve()
    if _path_is_within(node_path, workspace) or "node_modules" in node_path.parts:
        return None
    candidates = {
        "typecheck": (workspace / "node_modules" / "typescript" / "bin" / "tsc", ["--noEmit"]),
        "build": (workspace / "node_modules" / "next" / "dist" / "bin" / "next", ["build"]),
        "browser": (
            workspace / "node_modules" / "@playwright" / "test" / "cli.js",
            ["test"],
        ),
    }
    selected = candidates.get(category)
    if selected is None:
        return None
    entrypoint, arguments = selected
    if category == "browser" and not entrypoint.is_file():
        entrypoint = workspace / "node_modules" / "playwright" / "cli.js"
    try:
        assert_no_symlink_components(
            workspace,
            entrypoint,
            description="verification command entrypoint",
        )
    except CLIError:
        return None
    if not entrypoint.is_file():
        return None
    return [str(node_path), str(entrypoint), *arguments]


def _sanitized_path(workspace: Path) -> str:
    """Remove repository and node_modules executable directories from subprocess PATH."""

    safe: list[str] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        resolved = Path(raw).expanduser().resolve()
        if _path_is_within(resolved, workspace) or "node_modules" in resolved.parts:
            continue
        safe.append(str(resolved))
    return os.pathsep.join(safe)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.expanduser().resolve())
    except ValueError:
        return False
    return True


def _verification_artifact_state(workspace: Path, category: str) -> dict[str, tuple[int, int]]:
    paths = {
        "build": (workspace / ".next" / "build-manifest.json",),
        "browser": (
            workspace / "test-results" / ".last-run.json",
            workspace / "playwright-report" / "index.html",
        ),
    }.get(category, ())
    return {
        str(path): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in paths
        if path.is_file() and not path.is_symlink()
    }


def _semantic_command_evidence(
    workspace: Path,
    category: str,
    before: Mapping[str, tuple[int, int]],
    started: datetime,
    completed: datetime,
    *,
    exit_code: int,
    outcome: str,
    command_sha256: str,
    browser_output: Path | None = None,
) -> tuple[bool, str]:
    if category == "typecheck":
        if exit_code != 0 or outcome != "completed":
            return False, ""
        return True, command_sha256
    browser_root = browser_output if browser_output is not None else workspace / "test-results"
    paths = {
        "build": (workspace / ".next" / "build-manifest.json",),
        "browser": (
            browser_root / ".last-run.json",
        ),
    }.get(category, ())
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        old = before.get(str(path))
        changed = old != (stat.st_mtime_ns, stat.st_size)
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        within_command_window = (
            started - timedelta(seconds=1) <= modified <= completed + timedelta(seconds=1)
        )
        if changed and within_command_window:
            if category == "build" and not _valid_next_build_manifest(path):
                continue
            if category == "browser" and not _valid_playwright_result(path):
                continue
            return True, hashlib.sha256(path.read_bytes()).hexdigest()
    return False, ""


def _browser_runtime_evidence(
    output_dir: Path,
    *,
    runtime_api_key: str | None,
    stdout: bytes,
    stderr: bytes,
    command_succeeded: bool,
) -> dict[str, Any]:
    """Inspect verifier-owned Playwright traces and process output without retaining values."""

    traces = sorted(
        path
        for path in output_dir.rglob("trace.zip")
        if path.is_file() and not path.is_symlink()
    )
    key_bytes = runtime_api_key.encode("utf-8") if runtime_api_key else b""
    secret_match = bool(key_bytes and (key_bytes in stdout or key_bytes in stderr))
    digest = hashlib.sha256()
    scan_complete = bool(command_succeeded and key_bytes and traces)
    total_uncompressed = 0
    try:
        if len(traces) > 128:
            scan_complete = False
        for trace in traces[:128]:
            if trace.stat().st_size > 64 * 1024 * 1024:
                scan_complete = False
                break
            digest.update(trace.read_bytes())
            with zipfile.ZipFile(trace) as archive:
                entries = archive.infolist()
                if len(entries) > 4096:
                    scan_complete = False
                    continue
                for entry in entries:
                    total_uncompressed += entry.file_size
                    if entry.file_size > 32 * 1024 * 1024 or total_uncompressed > 128 * 1024 * 1024:
                        scan_complete = False
                        break
                    payload = archive.read(entry)
                    if key_bytes and key_bytes in payload:
                        secret_match = True
                if not scan_complete:
                    break
    except (OSError, zipfile.BadZipFile, RuntimeError):
        scan_complete = False
    return {
        "browser_runtime_evidence_complete": scan_complete,
        "browser_secret_match": secret_match,
        "browser_runtime_artifact_sha256": digest.hexdigest() if traces else "",
        "browser_trace_count": len(traces),
    }


def _valid_next_build_manifest(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("pages"), dict)


def _valid_playwright_result(path: Path) -> bool:
    if path.name != ".last-run.json":
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "passed"


def _command_check(
    name: str,
    receipts: list[dict[str, Any]],
    *,
    categories: tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    matches = [item for item in receipts if item["category"] in categories]
    if not matches:
        return check_result(
            name,
            "SKIP",
            f"{name}_command_missing",
            f"No declared {label} command was available.",
        )
    failed = [
        item
        for item in matches
        if item["exit_code"] != 0 or not item.get("semantic_evidence")
    ]
    evidence = [
        {
            "command_sha256": item["command_sha256"],
            "exit_code": item["exit_code"],
            "artifact_sha256": item["stdout_sha256"],
            "browser_artifact_sha256": item.get("evidence_artifact_sha256"),
            "verification_attempt_id": item.get("verification_attempt_id"),
        }
        for item in matches
    ]
    return check_result(
        name,
        "FAIL" if failed else "PASS",
        f"{name}_{'command_failed' if failed else 'passed'}",
        f"Executed {len(matches)} declared {label} command(s); {len(failed)} failed.",
        evidence=evidence,
    )


def _application_evidence_path(workspace: Path, manifest: Mapping[str, Any]) -> Path:
    launch = manifest.get("x-general-augment-launch")
    verification = launch.get("verification") if isinstance(launch, dict) else None
    value = (
        verification.get("application_evidence_path")
        if isinstance(verification, dict)
        else None
    )
    relative = Path(str(value or ".genaug/application-verification.json"))
    return confined_path(
        workspace,
        workspace / relative,
        description="application evidence path",
    )


def _capability_check_from_application_evidence(
    workspace: Path,
    manifest: Mapping[str, Any],
    *,
    browser_receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    if not browser_receipts or any(item["exit_code"] != 0 for item in browser_receipts):
        return check_result(
            "read_only_application_capability",
            "SKIP",
            "read_only_application_capability_browser_evidence_missing",
            "A passing browser command must generate application capability evidence.",
        )
    try:
        path = _application_evidence_path(workspace, manifest)
    except (CLIError, ValueError):
        return check_result(
            "read_only_application_capability",
            "FAIL",
            "read_only_application_capability_path_invalid",
            "The application evidence path escapes the workspace.",
        )
    try:
        content = read_text_no_follow(
            workspace,
            path,
            description="application evidence path",
        )
    except (CLIError, OSError, UnicodeDecodeError):
        content = None
    if content is None:
        return check_result(
            "read_only_application_capability",
            "SKIP",
            "read_only_application_capability_artifact_missing",
            "The browser command did not produce the configured evidence artifact.",
        )
    raw = content.encode("utf-8")
    if len(raw) > MAX_APPLICATION_EVIDENCE_BYTES:
        return check_result(
            "read_only_application_capability",
            "FAIL",
            "read_only_application_capability_artifact_oversized",
            "The application evidence artifact exceeds the size limit.",
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != APPLICATION_EVIDENCE_SCHEMA_VERSION
    ):
        return check_result(
            "read_only_application_capability",
            "FAIL",
            "read_only_application_capability_artifact_invalid",
            "The application evidence artifact has an unsupported schema.",
        )
    generated_at = _parse_datetime(payload.get("generated_at"))
    attempt_ids = {
        str(item.get("verification_attempt_id") or "")
        for item in browser_receipts
        if item.get("verification_attempt_id")
    }
    verification_attempt_id = next(iter(attempt_ids)) if len(attempt_ids) == 1 else ""
    starts = [
        parsed
        for item in browser_receipts
        if (parsed := _parse_datetime(item.get("started_at"))) is not None
    ]
    completions = [
        parsed
        for item in browser_receipts
        if (parsed := _parse_datetime(item.get("completed_at"))) is not None
    ]
    earliest = min(starts) if starts else None
    latest = max(completions) if completions else None
    if (
        generated_at is None
        or earliest is None
        or latest is None
        or not earliest <= generated_at <= latest
        or str(payload.get("verification_attempt_id") or "") != verification_attempt_id
    ):
        return check_result(
            "read_only_application_capability",
            "FAIL",
            "read_only_application_capability_artifact_stale",
            "The application evidence was not generated by the current browser command.",
        )
    checks = payload.get("checks")
    rows = [row for row in checks if isinstance(row, dict)] if isinstance(checks, list) else []
    capability = next(
        (row for row in rows if row.get("name") == "read_only_application_capability"),
        None,
    )
    evidence = capability.get("evidence") if isinstance(capability, dict) else None
    evidence = evidence if isinstance(evidence, dict) else {}
    launch = manifest.get("x-general-augment-launch")
    capabilities = launch.get("capabilities") if isinstance(launch, dict) else None
    reviewed_read_only = {
        str(item.get("name") or "")
        for item in capabilities
        if isinstance(item, dict) and item.get("classification") == "read_only"
    } if isinstance(capabilities, list) else set()
    valid = (
        isinstance(capability, dict)
        and capability.get("status") == "PASS"
        and evidence.get("classification") == "read_only"
        and evidence.get("identity_binding") == "authenticated_server_user"
        and str(evidence.get("capability") or "") in reviewed_read_only
        and bool(
            evidence.get("response_id")
            or evidence.get("trace_id")
            or evidence.get("request_id")
        )
    )
    digest = hashlib.sha256(raw).hexdigest()
    return check_result(
        "read_only_application_capability",
        "PASS" if valid else "FAIL",
        (
            "read_only_application_capability_passed"
            if valid
            else "read_only_application_capability_evidence_incomplete"
        ),
        (
            "The current browser run proved an authenticated read-only capability."
            if valid
            else "Capability evidence must prove read-only classification and server user binding."
        ),
        evidence=[
            {
                "artifact_sha256": digest,
                "capability": evidence.get("capability"),
                "classification": evidence.get("classification"),
                "execution_owner": evidence.get("execution_owner"),
                "identity_binding": evidence.get("identity_binding"),
                "response_id": evidence.get("response_id"),
                "trace_id": evidence.get("trace_id"),
                "request_id": evidence.get("request_id"),
                "verification_attempt_id": verification_attempt_id,
            }
        ],
    )


def _browser_secret_check(
    workspace: Path,
    runtime_api_key: str | None,
    *,
    browser_receipts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not runtime_api_key:
        return check_result(
            "secret_not_browser_visible",
            "SKIP",
            "secret_not_browser_visible_runtime_key_missing",
            "The runtime key was unavailable for an exact browser-artifact scan.",
        )
    key_bytes = runtime_api_key.encode("utf-8")
    roots = [workspace / ".next" / "static", workspace / "public", workspace / "out"]
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(
                path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
            )
    prerendered = workspace / ".next" / "server" / "app"
    if prerendered.is_dir():
        files.extend(
            path
            for path in prerendered.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.casefold() in {".body", ".html", ".rsc", ".txt"}
        )
    client_sources = [
        path
        for path in workspace.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.casefold() in {".js", ".jsx", ".ts", ".tsx"}
        and not any(part in {"node_modules", ".git", ".next"} for part in path.parts)
        and b"use client" in path.read_bytes()[:4096].lower()
    ]
    files.extend(client_sources)
    unique_files = sorted(set(files))
    raw_key_leaked = any(key_bytes in path.read_bytes() for path in unique_files)
    client_secret_reference = any(
        b"GENAUG_API_KEY" in path.read_bytes() for path in client_sources
    )
    runtime_receipts = browser_receipts or []
    runtime_evidence_complete = bool(runtime_receipts) and all(
        item.get("exit_code") == 0
        and item.get("semantic_evidence") is True
        and item.get("browser_runtime_evidence_complete") is True
        and bool(item.get("browser_runtime_artifact_sha256"))
        for item in runtime_receipts
    )
    runtime_secret_match = any(
        item.get("browser_secret_match") is True for item in runtime_receipts
    )
    leaked = raw_key_leaked or client_secret_reference or runtime_secret_match
    no_browser_files = not unique_files
    scan_digest = hashlib.sha256(
        "\n".join(str(path.relative_to(workspace)) for path in unique_files).encode("utf-8")
    ).hexdigest()
    return check_result(
        "secret_not_browser_visible",
        "FAIL" if leaked or no_browser_files or not runtime_evidence_complete else "PASS",
        (
            "secret_browser_artifact_match"
            if leaked
            else (
                "secret_browser_artifacts_missing"
                if no_browser_files
                else (
                    "secret_browser_runtime_evidence_missing"
                    if not runtime_evidence_complete
                    else "secret_not_browser_visible_passed"
                )
            )
        ),
        (
            "A browser-visible artifact contains the runtime credential."
            if leaked
            else (
                "No browser-visible build artifacts were available to scan."
                if no_browser_files
                else (
                    "No fresh verifier-controlled Playwright trace was available to scan."
                    if not runtime_evidence_complete
                    else (
                    f"Scanned {len(unique_files)} browser-visible file(s) without a "
                    "credential match."
                    )
                )
            )
        ),
        evidence=[
            {
                "checked_files": len(unique_files),
                "artifact_sha256": scan_digest,
                "runtime_trace_count": sum(
                    int(item.get("browser_trace_count") or 0) for item in runtime_receipts
                ),
                "runtime_evidence_complete": runtime_evidence_complete,
            }
        ],
    )


def _rollback_check(manifest: Mapping[str, Any]) -> dict[str, Any]:
    launch = manifest.get("x-general-augment-launch")
    rollback = launch.get("rollback") if isinstance(launch, dict) else None
    valid = (
        isinstance(rollback, dict)
        and bool(str(rollback.get("disable") or "").strip())
        and bool(str(rollback.get("data") or "").strip())
    )
    return check_result(
        "rollback_documented",
        "PASS" if valid else "FAIL",
        "rollback_documented_passed" if valid else "rollback_documentation_missing",
        (
            "The manifest includes disable and data rollback guidance."
            if valid
            else "The manifest must include disable and data rollback guidance."
        ),
        evidence=[{"manifest_fingerprint": manifest_fingerprint(manifest)}] if valid else [],
    )


def evidence_is_fresh(value: object, *, now: datetime | None = None) -> bool:
    """Return whether an evidence timestamp is within the launch verification window."""

    observed = _parse_datetime(value)
    current = now or datetime.now(UTC)
    return observed is not None and timedelta(0) <= current - observed <= MAX_EVIDENCE_AGE


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load a YAML mapping for verification helpers."""

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, dict) else {}


def _safe_call(operation: Any) -> Any:
    """Run one evidence probe without copying exception text into receipts."""

    try:
        return operation()
    except Exception:
        return None


def _runtime_probe(operation: Any) -> tuple[Any, str | None]:
    """Run an app-facing probe and preserve only a safe failure category."""

    try:
        return operation(), None
    except APIError as exc:
        if exc.status_code == 402:
            return None, "runtime_limit_reached"
        if exc.status_code == 429:
            return None, "runtime_rate_limited"
        if exc.status_code in {401, 403}:
            return None, "runtime_authorization_failed"
        if exc.status_code >= 500:
            return None, "runtime_dependency_unavailable"
        return None, "runtime_api_error"
    except CLIError:
        return None, "runtime_transport_failed"
    except Exception:
        return None, "runtime_probe_failed"


def _runtime_failure_reason(
    check_name: str,
    failure: str | None,
    semantic_reason: str,
) -> str:
    """Return a stable check reason without exposing provider or API details."""

    return f"{check_name}_{failure}" if failure else semantic_reason


def _runtime_failure_detail(failure: str | None, semantic_detail: str) -> str:
    """Explain operational probe failures separately from semantic failures."""

    if failure is None:
        return semantic_detail
    details = {
        "runtime_limit_reached": "A project usage or budget limit blocked this runtime probe.",
        "runtime_rate_limited": "A runtime provider or platform rate limit blocked this probe.",
        "runtime_authorization_failed": "Runtime authorization blocked this probe.",
        "runtime_dependency_unavailable": "A runtime dependency was unavailable for this probe.",
        "runtime_api_error": "The runtime API rejected this probe.",
        "runtime_transport_failed": "The CLI could not reach the runtime for this probe.",
        "runtime_probe_failed": "The runtime probe failed before evidence was produced.",
    }
    return details.get(failure, semantic_detail)


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def _response_text(response: Mapping[str, Any]) -> str:
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


def _event_name(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    if event.get("event"):
        return str(event["event"])
    data = event.get("data")
    return str(data.get("type") or "") if isinstance(data, dict) else ""


def _valid_response_event_sequence(names: list[str]) -> bool:
    if (
        not names
        or names[0] != "response.created"
        or names[-1] != "response.completed"
        or names.count("response.created") != 1
        or names.count("response.completed") != 1
        or "response.failed" in names
    ):
        return False
    try:
        created = names.index("response.created")
        delta = names.index("response.output_text.delta")
        completed = names.index("response.completed")
    except ValueError:
        return False
    return created < delta < completed


def _facts_include_memory(payload: object, memory_id: str, marker: str) -> bool:
    facts = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        return False
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        if memory_id and str(fact.get("memory_id") or fact.get("id") or "") == memory_id:
            return True
        if marker.casefold() in json.dumps(fact, sort_keys=True).casefold():
            return True
    return False


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    """Parse one strict three-part release version for protocol compatibility."""

    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
