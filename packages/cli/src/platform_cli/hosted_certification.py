"""Build and validate a secret-free hosted golden-path certification receipt."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from platform_cli.errors import CLIError
from platform_cli.launch_verification import REQUIRED_BETA_CHECKS
from platform_cli.secure_filesystem import atomic_write_text_no_follow, confined_path

HOSTED_CERTIFICATION_SCHEMA_VERSION = "general-augment-hosted-certification/v2"
_SHA256 = "^[a-f0-9]{64}$"
_BUILD_SHA = "^[a-f0-9]{7,64}$"
_SAFE_IDENTIFIER = "^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$"
_SAFE_REASON = "^[a-z0-9][a-z0-9_]{0,127}$"
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ga|gacode|ghp|github_pat|xox[baprs]|AKIA)[_-][A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
)
_EMAIL_PATTERN = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _object(properties: dict[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
    }


_IMAGE_SCHEMA = _object(
    {
        "provider": {"const": "oci"},
        "digest": {"type": "string", "pattern": f"^sha256:{_SHA256[1:-1]}$"},
        "build_sha": {"type": "string", "pattern": _BUILD_SHA},
    },
    ("provider", "digest", "build_sha"),
)
_VERCEL_SCHEMA = _object(
    {
        "provider": {"const": "vercel"},
        "deployment_id": {"type": "string", "pattern": _SAFE_IDENTIFIER},
        "build_sha": {"type": "string", "pattern": _BUILD_SHA},
        "url": {"type": "string", "format": "uri"},
    },
    ("provider", "deployment_id", "build_sha", "url"),
)
_EVIDENCE_BINDING_SCHEMA = _object(
    {
        "schema_version": {"type": "string", "pattern": _SAFE_IDENTIFIER},
        "sha256": {"type": "string", "pattern": _SHA256},
        "checked_at": {"type": "string", "format": "date-time"},
    },
    ("schema_version", "sha256", "checked_at"),
)
_IDENTITY_SCHEMA = _object(
    {
        "project_id": {"type": "string", "pattern": _SAFE_IDENTIFIER},
        "agent_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": _SAFE_IDENTIFIER},
        },
        "runtime_key_id": {"type": "string", "pattern": _SAFE_IDENTIFIER},
    },
    ("project_id", "agent_ids", "runtime_key_id"),
)
_COUNT_SCHEMA = _object(
    {
        "projects": {"type": "integer", "minimum": 1},
        "agents": {"type": "integer", "minimum": 1},
        "active_runtime_keys": {"type": "integer", "minimum": 1},
    },
    ("projects", "agents", "active_runtime_keys"),
)

HOSTED_CERTIFICATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://generalaugment.com/schemas/hosted-certification-v2.json",
    **_object(
        {
            "schema_version": {"const": HOSTED_CERTIFICATION_SCHEMA_VERSION},
            "verdict": {"const": "READY"},
            "generated_at": {"type": "string", "format": "date-time"},
            "source": _object(
                {
                    "commit": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
                    "branch": {"type": "string", "pattern": _SAFE_IDENTIFIER},
                    "clean": {"const": True},
                },
                ("commit", "branch", "clean"),
            ),
            "artifacts": _object(
                {
                    "cli_version": {
                        "type": "string",
                        "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$",
                    },
                    "cli_wheel_sha256": {"type": "string", "pattern": _SHA256},
                    "skill_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "skill_sha256": {"type": "string", "pattern": _SHA256},
                    "manifest_sha256": {"type": "string", "pattern": _SHA256},
                    "api_image": _IMAGE_SCHEMA,
                    "worker_image": _IMAGE_SCHEMA,
                    "dashboard": {"oneOf": [_IMAGE_SCHEMA, _VERCEL_SCHEMA]},
                    "fixture_image": {"anyOf": [_IMAGE_SCHEMA, {"type": "null"}]},
                },
                (
                    "cli_version",
                    "cli_wheel_sha256",
                    "skill_version",
                    "skill_sha256",
                    "manifest_sha256",
                    "api_image",
                    "worker_image",
                    "dashboard",
                    "fixture_image",
                ),
            ),
            "evidence_artifacts": _object(
                dict.fromkeys(
                    (
                        "verification",
                        "provision_first",
                        "provision_second",
                        "finalization_first",
                        "finalization_second",
                        "browser",
                        "deployment",
                        "management_denial",
                    ),
                    _EVIDENCE_BINDING_SCHEMA,
                ),
                (
                    "verification",
                    "provision_first",
                    "provision_second",
                    "browser",
                    "deployment",
                    "management_denial",
                ),
            ),
            "deployment": _object(
                {
                    "class": {"enum": ["isolated_hosted", "staging", "production"]},
                    "namespace": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,62}$"},
                    "url_mode": {
                        "enum": [
                            "port_forward",
                            "private_ingress",
                            "public_preview",
                            "public_production",
                        ]
                    },
                    "api_url": {"type": "string", "format": "uri"},
                    "dashboard_url": {"type": "string", "format": "uri"},
                    "fixture_url": {"type": ["string", "null"], "format": "uri"},
                },
                ("class", "namespace", "url_mode", "api_url", "dashboard_url", "fixture_url"),
            ),
            "identifiers": _object(
                {
                    name: {"type": "string", "pattern": _SAFE_IDENTIFIER}
                    for name in (
                        "workspace_id",
                        "project_id",
                        "launch_session_id",
                        "release_id",
                        "runtime_key_id",
                        "response_id",
                        "run_id",
                        "trace_id",
                    )
                },
                (
                    "workspace_id",
                    "project_id",
                    "launch_session_id",
                    "release_id",
                    "runtime_key_id",
                    "response_id",
                    "run_id",
                    "trace_id",
                ),
            ),
            "links": _object(
                {
                    "review_url": {"type": "string", "format": "uri"},
                    "trace_url": {"type": "string", "format": "uri"},
                    "usage_url": {"type": "string", "format": "uri"},
                },
                ("review_url", "trace_url", "usage_url"),
            ),
            "checks": {
                "type": "array",
                "minItems": len(REQUIRED_BETA_CHECKS),
                "maxItems": len(REQUIRED_BETA_CHECKS),
                "items": _object(
                    {
                        "name": {"enum": list(REQUIRED_BETA_CHECKS)},
                        "required": {"const": True},
                        "status": {"const": "PASS"},
                        "reason_code": {"type": "string", "pattern": _SAFE_REASON},
                        "checked_at": {"type": "string", "format": "date-time"},
                        "evidence_ids": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "pattern": _SAFE_IDENTIFIER},
                        },
                    },
                    ("name", "required", "status", "reason_code", "checked_at", "evidence_ids"),
                ),
            },
            "security": _object(
                {
                    "management_route_status": {"const": 403},
                    "management_route_checked_at": {"type": "string", "format": "date-time"},
                    "cli_config_mode": {"const": "0600"},
                    "application_env_mode": {"const": "0600"},
                    "secret_match_count": {"const": 0},
                },
                (
                    "management_route_status",
                    "management_route_checked_at",
                    "cli_config_mode",
                    "application_env_mode",
                    "secret_match_count",
                ),
            ),
            "idempotency": _object(
                {
                    "first": _IDENTITY_SCHEMA,
                    "second": _IDENTITY_SCHEMA,
                    "counts_before": _COUNT_SCHEMA,
                    "counts_after": _COUNT_SCHEMA,
                },
                ("first", "second", "counts_before", "counts_after"),
            ),
            "cleanup": _object(
                {
                    "state": {"enum": ["pending", "complete"]},
                    "cleaned_at": {"type": ["string", "null"], "format": "date-time"},
                    "runtime_key_revoked": {"type": "boolean"},
                    "application_env_removed": {"type": "boolean"},
                    "certification_stack_removed": {"type": "boolean"},
                },
                (
                    "state",
                    "cleaned_at",
                    "runtime_key_revoked",
                    "application_env_removed",
                    "certification_stack_removed",
                ),
            ),
        },
        (
            "schema_version",
            "verdict",
            "generated_at",
            "source",
            "artifacts",
            "evidence_artifacts",
            "deployment",
            "identifiers",
            "links",
            "checks",
            "security",
            "idempotency",
            "cleanup",
        ),
    ),
}


def validate_hosted_certification_receipt(payload: Mapping[str, Any]) -> None:
    """Reject incomplete, non-idempotent, sensitive, or personally identifying receipts."""

    errors = sorted(
        Draft202012Validator(
            HOSTED_CERTIFICATION_SCHEMA,
            format_checker=FormatChecker(),
        ).iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "receipt"
        raise CLIError(f"Invalid hosted certification receipt at {location}: {first.message}")

    checks = payload["checks"]
    names = [row["name"] for row in checks]
    if set(names) != set(REQUIRED_BETA_CHECKS) or len(names) != len(set(names)):
        raise CLIError(
            "Hosted certification receipt must contain each required beta check exactly once."
        )

    generated_at = _parse_datetime(payload["generated_at"])
    if generated_at is None:
        raise CLIError("Hosted certification receipt generated_at is invalid.")
    timestamps = [row["checked_at"] for row in checks]
    timestamps.extend(row["checked_at"] for row in payload["evidence_artifacts"].values())
    timestamps.append(payload["security"]["management_route_checked_at"])
    if any(not _fresh_at_generation(value, generated_at) for value in timestamps):
        raise CLIError("Hosted certification receipt contains missing, future, or stale evidence.")

    evidence_artifacts = payload["evidence_artifacts"]
    if ("finalization_first" in evidence_artifacts) != (
        "finalization_second" in evidence_artifacts
    ):
        raise CLIError(
            "Hosted certification receipt must bind both durable finalization receipts."
        )

    idempotency = payload["idempotency"]
    if idempotency["first"] != idempotency["second"]:
        raise CLIError("Provisioning rerun changed project, agent, or runtime-key identities.")
    if idempotency["counts_before"] != idempotency["counts_after"]:
        raise CLIError("Provisioning rerun changed certification resource counts.")

    cleanup = payload["cleanup"]
    cleanup_flags = (
        cleanup["runtime_key_revoked"],
        cleanup["application_env_removed"],
        cleanup["certification_stack_removed"],
    )
    if cleanup["state"] == "complete" and (cleanup["cleaned_at"] is None or not all(cleanup_flags)):
        raise CLIError("Completed cleanup requires a timestamp and all cleanup controls to pass.")
    if cleanup["state"] == "pending" and cleanup["cleaned_at"] is not None:
        raise CLIError("Pending cleanup must not claim a cleanup timestamp.")

    for path, value in _string_values(payload):
        if path.endswith("_url") and value:
            _validate_url(path, value)
        if _EMAIL_PATTERN.search(value):
            raise CLIError(f"Hosted certification receipt contains PII at {path}.")
        if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
            raise CLIError(f"Hosted certification receipt contains a secret-like value at {path}.")


def build_hosted_certification_receipt(
    *,
    source: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    evidence_artifacts: Mapping[str, Any],
    deployment: Mapping[str, Any],
    identifiers: Mapping[str, Any],
    links: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
    security: Mapping[str, Any],
    idempotency: Mapping[str, Any],
    cleanup: Mapping[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Create the canonical READY receipt and validate it before returning it."""

    timestamp = generated_at or datetime.now(UTC)
    payload: dict[str, Any] = {
        "schema_version": HOSTED_CERTIFICATION_SCHEMA_VERSION,
        "verdict": "READY",
        "generated_at": timestamp.astimezone(UTC).isoformat(),
        "source": dict(source),
        "artifacts": dict(artifacts),
        "evidence_artifacts": dict(evidence_artifacts),
        "deployment": dict(deployment),
        "identifiers": dict(identifiers),
        "links": dict(links),
        "checks": [dict(row) for row in checks],
        "security": dict(security),
        "idempotency": dict(idempotency),
        "cleanup": dict(cleanup),
    }
    validate_hosted_certification_receipt(payload)
    return payload


def write_hosted_certification_receipt(
    path: Path,
    payload: Mapping[str, Any],
    *,
    workspace: Path | None = None,
) -> Path:
    """Validate and atomically persist the receipt with owner-only permissions."""

    validate_hosted_certification_receipt(payload)
    root = (workspace or path.expanduser().absolute().parent.parent).expanduser().resolve()
    resolved = confined_path(root, path, description="hosted certification receipt")
    atomic_write_text_no_follow(
        root,
        resolved,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        description="hosted certification receipt",
        mode=0o600,
    )
    return resolved


def _string_values(value: Any, path: str = "receipt") -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        rows: list[tuple[str, str]] = []
        for key, item in value.items():
            rows.extend(_string_values(item, f"{path}.{key}"))
        return rows
    if isinstance(value, list):
        rows = []
        for index, item in enumerate(value):
            rows.extend(_string_values(item, f"{path}[{index}]"))
        return rows
    return [(path, value)] if isinstance(value, str) else []


def _validate_url(path: str, value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CLIError(f"Hosted certification receipt contains an unsupported URL at {path}.")
    if parsed.username or parsed.password or parsed.fragment:
        raise CLIError(
            f"Hosted certification receipt URL may not contain credentials or fragment at {path}."
        )
    if parsed.query:
        if not path.startswith("receipt.links."):
            raise CLIError(f"Hosted certification receipt URL may not contain a query at {path}.")
        query_keys = {part.partition("=")[0] for part in parsed.query.split("&")}
        if not query_keys <= {"project_id", "response_id", "run_id", "trace_id"}:
            raise CLIError(
                f"Hosted certification receipt URL contains unsafe query keys at {path}."
            )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _fresh_at_generation(value: object, generated_at: datetime) -> bool:
    observed = _parse_datetime(value)
    if observed is None:
        return False
    age = generated_at - observed
    return 0 <= age.total_seconds() <= 24 * 60 * 60
