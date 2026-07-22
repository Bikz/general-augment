"""Assemble a trusted, secret-free hosted golden-path certification receipt."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import typer

from platform_cli.certification_browser import validate_browser_evidence
from platform_cli.errors import CLIError
from platform_cli.hosted_certification import (
    build_hosted_certification_receipt,
    write_hosted_certification_receipt,
)
from platform_cli.launch_verification import (
    APPLICATION_EVIDENCE_SCHEMA_VERSION,
    REQUIRED_BETA_CHECKS,
    evidence_is_fresh,
)
from platform_cli.output import print_json, print_success
from platform_cli.runtime import Runtime
from platform_cli.secure_filesystem import read_text_no_follow

app = typer.Typer(help="Create evidence-bound hosted certification receipts.")

_VERIFICATION_SCHEMA = "general-augment-launch-verification/v1"
_PROVISION_SCHEMA = "general-augment-provisioning-receipt/v1"
_BROWSER_SCHEMA = APPLICATION_EVIDENCE_SCHEMA_VERSION
_DEPLOYMENT_SCHEMA = "general-augment-hosted-deployment/v1"
_DENIAL_SCHEMA = "general-augment-runtime-management-denial/v1"
_FINALIZATION_SCHEMA = "general-augment-launch-finalization/v1"


@dataclass(frozen=True)
class EvidenceDocument:
    """One parsed evidence file plus its exact byte-content binding."""

    payload: dict[str, Any]
    sha256: str
    schema_version: str
    checked_at: str

    def binding(self) -> dict[str, str]:
        """Return the non-sensitive receipt binding for this evidence artifact."""

        return {
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "checked_at": self.checked_at,
        }


@app.command("create")
def create_certification(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option(
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Workspace containing certification evidence.",
        ),
    ],
    verification: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    provision_first: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    provision_second: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    browser: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    deployment: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    finalization_first: Annotated[
        Path | None,
        typer.Option(
            help=(
                "First durable release-finalization receipt. Required with "
                "--finalization-second after preview authority is replaced."
            )
        ),
    ] = None,
    finalization_second: Annotated[
        Path | None,
        typer.Option(
            help=(
                "Idempotent durable release-finalization rerun. Required with "
                "--finalization-first."
            )
        ),
    ] = None,
    output: Annotated[
        Path,
        typer.Option(help="Owner-only receipt path inside the workspace."),
    ] = Path(".genaug/hosted-certification.json"),
    management_denial_evidence: Annotated[
        Path | None,
        typer.Option(
            help="Previously captured status-only runtime-key management denial evidence."
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Require fresh hosted evidence and emit one cryptographically bound READY receipt."""

    runtime: Runtime = ctx.obj
    root = workspace.expanduser().resolve()
    documents = {
        "verification": _read_document(root, verification, _VERIFICATION_SCHEMA, "verified_at"),
        "provision_first": _read_document(root, provision_first, _PROVISION_SCHEMA, "checked_at"),
        "provision_second": _read_document(root, provision_second, _PROVISION_SCHEMA, "checked_at"),
        "browser": _read_document(root, browser, _BROWSER_SCHEMA, "generated_at"),
        "deployment": _read_document(root, deployment, _DEPLOYMENT_SCHEMA, "checked_at"),
    }
    if (finalization_first is None) != (finalization_second is None):
        raise CLIError(
            "Provide both --finalization-first and --finalization-second, or neither."
        )
    if finalization_first is not None and finalization_second is not None:
        documents["finalization_first"] = _read_document(
            root,
            finalization_first,
            _FINALIZATION_SCHEMA,
            "finalized_at",
        )
        documents["finalization_second"] = _read_document(
            root,
            finalization_second,
            _FINALIZATION_SCHEMA,
            "finalized_at",
        )
    if management_denial_evidence is not None:
        denial = _read_document(
            root,
            management_denial_evidence,
            _DENIAL_SCHEMA,
            "checked_at",
        )
        if denial.payload.get("status") != 403:
            raise CLIError("Runtime-key management denial evidence must record HTTP 403.")
    else:
        denial = _probe_runtime_management_denial(runtime)
    documents["management_denial"] = denial

    receipt = assemble_certification_receipt(documents)
    target = output if output.is_absolute() else root / output
    written = write_hosted_certification_receipt(target, receipt, workspace=root)
    result = {
        "status": "READY",
        "receipt_path": str(written),
        "receipt_sha256": hashlib.sha256(
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        ).hexdigest(),
        "review_url": receipt["links"]["review_url"],
        "trace_url": receipt["links"]["trace_url"],
        "usage_url": receipt["links"]["usage_url"],
    }
    if json_output:
        print_json(result)
    else:
        print_success(f"Hosted certification READY. Receipt: {written}")


def assemble_certification_receipt(
    documents: Mapping[str, EvidenceDocument],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate exact evidence documents and construct one canonical receipt."""

    required = {
        "verification",
        "provision_first",
        "provision_second",
        "browser",
        "deployment",
        "management_denial",
    }
    supplied = set(documents)
    finalization = {"finalization_first", "finalization_second"}
    if supplied not in (required, required | finalization):
        raise CLIError(
            "Certification assembly requires the exact base evidence and either both "
            "durable finalization receipts or neither."
        )
    now = generated_at or datetime.now(UTC)
    if any(not evidence_is_fresh(doc.checked_at, now=now) for doc in documents.values()):
        raise CLIError("Certification evidence is missing, future-dated, or older than 24 hours.")

    verification = documents["verification"].payload
    first = documents["provision_first"].payload
    second = documents["provision_second"].payload
    browser = documents["browser"].payload
    deployment = documents["deployment"].payload
    denial = documents["management_denial"].payload

    checks = _validated_checks(verification, now)
    _validate_provisioning(
        first,
        second,
        deployment,
        finalization_first=(
            documents["finalization_first"].payload
            if "finalization_first" in documents
            else None
        ),
        finalization_second=(
            documents["finalization_second"].payload
            if "finalization_second" in documents
            else None
        ),
    )
    validate_browser_evidence(browser, deployment, verification)
    _validate_links(verification, deployment)
    if denial.get("status") != 403:
        raise CLIError("The application runtime key was not denied management access.")

    security = _mapping(deployment, "security")
    security["management_route_status"] = 403
    security["management_route_checked_at"] = documents["management_denial"].checked_at
    return build_hosted_certification_receipt(
        generated_at=now,
        source=_mapping(deployment, "source"),
        artifacts=_mapping(deployment, "artifacts"),
        evidence_artifacts={name: documents[name].binding() for name in sorted(documents)},
        deployment=_mapping(deployment, "deployment"),
        identifiers=_mapping(deployment, "identifiers"),
        links=_mapping(deployment, "links"),
        checks=checks,
        security=security,
        idempotency=_mapping(deployment, "idempotency"),
        cleanup=_mapping(deployment, "cleanup"),
    )


def _read_document(
    root: Path,
    path: Path,
    expected_schema: str,
    timestamp_field: str,
) -> EvidenceDocument:
    candidate = path if path.is_absolute() else root / path
    content = read_text_no_follow(root, candidate, description="certification evidence")
    if content is None:
        raise CLIError(f"Certification evidence is unavailable: {candidate.name}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CLIError(f"Certification evidence is not valid JSON: {candidate.name}") from exc
    if not isinstance(parsed, dict) or parsed.get("schema_version") != expected_schema:
        raise CLIError(f"Certification evidence has the wrong schema: {candidate.name}")
    checked_at = parsed.get(timestamp_field)
    if not isinstance(checked_at, str):
        raise CLIError(f"Certification evidence has no timestamp: {candidate.name}")
    return EvidenceDocument(
        payload=dict(parsed),
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        schema_version=expected_schema,
        checked_at=checked_at,
    )


def _validated_checks(payload: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    if payload.get("verdict") != "READY":
        raise CLIError("Launch verification must have a READY verdict.")
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or len(raw_checks) != len(REQUIRED_BETA_CHECKS):
        raise CLIError("Launch verification must contain all 18 required checks.")
    names = [row.get("name") for row in raw_checks if isinstance(row, dict)]
    if len(names) != len(set(names)) or set(names) != set(REQUIRED_BETA_CHECKS):
        raise CLIError("Launch verification must contain each required check exactly once.")

    checks: list[dict[str, Any]] = []
    for raw in raw_checks:
        if (
            not isinstance(raw, dict)
            or raw.get("required") is not True
            or raw.get("status") != "PASS"
        ):
            raise CLIError("Every required launch verification check must PASS.")
        checked_at = raw.get("checked_at")
        evidence = raw.get("evidence")
        if not evidence_is_fresh(checked_at, now=now):
            raise CLIError("Launch verification contains stale required-check evidence.")
        if not isinstance(evidence, list) or not evidence:
            raise CLIError("Every required check must include non-empty evidence.")
        checks.append(
            {
                "name": raw["name"],
                "required": True,
                "status": "PASS",
                "reason_code": raw.get("reason_code"),
                "checked_at": checked_at,
                "evidence_ids": [
                    f"sha256:{hashlib.sha256(_canonical(item)).hexdigest()}" for item in evidence
                ],
            }
        )
    return checks


def _validate_provisioning(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    deployment: Mapping[str, Any],
    *,
    finalization_first: Mapping[str, Any] | None = None,
    finalization_second: Mapping[str, Any] | None = None,
) -> None:
    fields = ("session_id", "project_id", "manifest_sha256")
    if any(first.get(field) != second.get(field) for field in fields):
        raise CLIError("Provisioning rerun changed its launch, project, or manifest binding.")
    first_key = _mapping(first, "runtime_key")
    second_key = _mapping(second, "runtime_key")
    if (
        first_key.get("action") not in {"created", "rotated", "reused"}
        or first_key.get("active_matching_count") != 1
    ):
        raise CLIError("First provisioning run must record exactly one active runtime key.")
    if first_key.get("id") != second_key.get("id"):
        raise CLIError("Provisioning rerun created a duplicate runtime key.")
    if second_key.get("action") != "reused" or second_key.get("active_matching_count") != 1:
        raise CLIError("Second provisioning run must reuse exactly one active runtime key.")
    identifiers = _mapping(deployment, "identifiers")
    expected = {
        "project_id": first.get("project_id"),
        "launch_session_id": first.get("session_id"),
    }
    if any(identifiers.get(key) != value for key, value in expected.items()):
        raise CLIError("Deployment identifiers do not match provisioning evidence.")
    first_release = _mapping(first, "release")
    second_release = _mapping(second, "release")
    if (
        first_release.get("id") != second_release.get("id")
        or first_release.get("fingerprint") != second_release.get("fingerprint")
        or identifiers.get("release_id") != first_release.get("id")
    ):
        raise CLIError("Provisioning rerun changed its immutable release binding.")
    artifacts = _mapping(deployment, "artifacts")
    if artifacts.get("manifest_sha256") != first.get("manifest_sha256"):
        raise CLIError("Deployment manifest hash does not match provisioning evidence.")
    if (finalization_first is None) != (finalization_second is None):
        raise CLIError("Durable finalization evidence must be supplied as an exact pair.")
    if finalization_first is None or finalization_second is None:
        if identifiers.get("runtime_key_id") != first_key.get("id"):
            raise CLIError("Deployment runtime key does not match provisioning evidence.")
        return
    _validate_finalization(
        first,
        deployment,
        finalization_first,
        finalization_second,
    )


def _validate_finalization(
    provisioning: Mapping[str, Any],
    deployment: Mapping[str, Any],
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> None:
    """Bind preview provisioning to one idempotently finalized durable authority."""

    release = _mapping(provisioning, "release")
    expected = {
        "session_id": provisioning.get("session_id"),
        "project_id": provisioning.get("project_id"),
        "release_id": release.get("id"),
        "release_fingerprint": release.get("fingerprint"),
        "runtime_mode": "test",
    }
    if any(
        first.get(field) != value or second.get(field) != value
        for field, value in expected.items()
    ):
        raise CLIError(
            "Durable finalization changed its launch, project, release, or runtime-mode binding."
        )
    first_key_id = str(first.get("runtime_key_id") or "")
    if (
        not first_key_id
        or second.get("runtime_key_id") != first_key_id
        or first.get("runtime_key_action") not in {"created", "reused"}
        or second.get("runtime_key_action") != "reused"
    ):
        raise CLIError("Durable finalization rerun did not reuse exactly one runtime key.")
    for receipt in (first, second):
        environment = _mapping(receipt, "environment")
        if (
            environment.get("status") != "configured"
            or environment.get("permission_mode") != "0600"
        ):
            raise CLIError(
                "Durable finalization did not install the application environment safely."
            )
    identifiers = _mapping(deployment, "identifiers")
    if (
        identifiers.get("runtime_key_id") != first_key_id
        or identifiers.get("release_id") != release.get("id")
    ):
        raise CLIError(
            "Deployment identifiers do not match durable finalization evidence."
        )


def _validate_links(verification: Mapping[str, Any], deployment: Mapping[str, Any]) -> None:
    links = _mapping(deployment, "links")
    identifiers = _mapping(deployment, "identifiers")
    if links.get("review_url") != verification.get("dashboard_review_url"):
        raise CLIError("Review URL is not bound to the exact verified launch session.")
    review_path = urlsplit(str(links.get("review_url") or "")).path.rstrip("/")
    suffix = (
        f"/dashboard/projects/{identifiers['project_id']}/launch/{identifiers['launch_session_id']}"
    )
    if not review_path.endswith(suffix):
        raise CLIError("Review URL does not identify the certified project and launch session.")
    trace_query = parse_qs(urlsplit(str(links.get("trace_url") or "")).query)
    for key in ("project_id", "response_id", "trace_id"):
        if trace_query.get(key) != [str(identifiers[key])]:
            raise CLIError("Trace URL does not identify the certified application run.")
    if "run_id" in trace_query and trace_query["run_id"] != [str(identifiers["run_id"])]:
        raise CLIError("Trace URL run_id does not identify the certified application run.")
    usage_path = urlsplit(str(links.get("usage_url") or "")).path.rstrip("/")
    if not usage_path.endswith(f"/dashboard/projects/{identifiers['project_id']}/usage"):
        raise CLIError("Usage URL does not identify the certified project.")

    trace_evidence = _check_evidence(verification, "trace_visibility")
    usage_evidence = _check_evidence(verification, "usage_visibility")
    for name in ("response_id", "run_id", "trace_id"):
        if trace_evidence.get(name) != identifiers.get(name):
            raise CLIError("Trace verification is not bound to the certified application run.")
    for name in ("response_id", "run_id"):
        if usage_evidence.get(name) != identifiers.get(name):
            raise CLIError("Usage verification is not bound to the certified application run.")
    if trace_evidence.get("url") != links.get("trace_url"):
        raise CLIError("Trace verification URL differs from the certified trace link.")
    if usage_evidence.get("url") != links.get("usage_url"):
        raise CLIError("Usage verification URL differs from the certified usage link.")


def _check_evidence(payload: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise CLIError("Launch verification checks are unavailable.")
    for row in checks:
        if not isinstance(row, Mapping) or row.get("name") != name:
            continue
        evidence = row.get("evidence")
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, Mapping):
                    return item
        break
    raise CLIError(f"Launch verification has no {name} evidence.")


def _probe_runtime_management_denial(runtime: Runtime) -> EvidenceDocument:
    key = runtime.config.runtime_api_key
    if not key:
        raise CLIError(
            "No runtime key is configured; provide --management-denial-evidence or provision first."
        )
    checked_at = datetime.now(UTC).isoformat()
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                f"{runtime.config.base_url.rstrip('/')}/api/v1/admin/me",
                headers={"X-Admin-Key": key},
            )
    except httpx.HTTPError as exc:
        raise CLIError("Could not complete the runtime-key management denial probe.") from exc
    payload = {
        "schema_version": _DENIAL_SCHEMA,
        "checked_at": checked_at,
        "status": response.status_code,
    }
    return EvidenceDocument(
        payload=payload,
        sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
        schema_version=_DENIAL_SCHEMA,
        checked_at=checked_at,
    )


def _mapping(payload: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, Mapping):
        raise CLIError(f"Certification evidence is missing the {name} object.")
    return dict(value)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
