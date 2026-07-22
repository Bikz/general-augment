"""CLI lifecycle for an approved candidate's expiring Test preview key."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from platform_cli.client import encode_path_segment
from platform_cli.config import CLIConfig
from platform_cli.errors import CLIError

RUNTIME_KEY_SCOPES = ("responses:create",)
MINIMUM_PREVIEW_REMAINING = timedelta(minutes=15)


@dataclass(frozen=True)
class ReleasePreviewProvisioning:
    """Secret-bearing local result; callers must redact it from command output."""

    action: str
    config: CLIConfig = field(repr=False)
    key: dict[str, Any]
    active_matching_count: int


@dataclass(frozen=True)
class ReleaseFinalization:
    """Verified Test deployment and its durable application credential."""

    action: str
    config: CLIConfig = field(repr=False)
    key: dict[str, Any]
    release: dict[str, Any]
    deployment: dict[str, Any]


def provision_release_preview(
    client: Any,
    *,
    token: str,
    config: CLIConfig,
    project_id: str,
    launch_session_id: str,
    release: dict[str, Any],
    rotate: bool,
) -> ReleasePreviewProvisioning:
    """Create or reuse one local-secret-backed candidate preview authority."""
    release_id = _required(release, "id")
    fingerprint = _required(release, "fingerprint")
    if str(release.get("status") or "") != "candidate":
        raise CLIError("Launch provisioning requires an immutable candidate release.")

    key_path = f"/projects/{encode_path_segment(project_id)}/runtime-keys"
    listed = client.installer("GET", key_path, token=token)
    preview_keys = _preview_keys(listed)
    reusable = _reusable_preview(
        config,
        preview_keys,
        project_id=project_id,
        release_id=release_id,
        fingerprint=fingerprint,
    )
    if reusable is not None and not rotate:
        return ReleasePreviewProvisioning(
            action="reused",
            config=config.model_copy(update={"active_project": project_id}),
            key=reusable,
            active_matching_count=1,
        )

    for key in preview_keys:
        binding_id = str(key.get("preview_binding_id") or "")
        if not binding_id:
            raise CLIError("An active preview key is missing its revocation binding.")
        client.installer(
            "DELETE",
            (
                f"/projects/{encode_path_segment(project_id)}/release-previews/"
                f"{encode_path_segment(binding_id)}"
            ),
            token=token,
        )

    created = client.installer(
        "POST",
        (
            f"/projects/{encode_path_segment(project_id)}/releases/"
            f"{encode_path_segment(release_id)}/preview"
        ),
        token=token,
        json={
            "launch_session_id": launch_session_id,
            "expected_release_fingerprint": fingerprint,
            "idempotency_key": f"cli-preview-{fingerprint[:16]}-{uuid4()}",
            "expires_in_seconds": 3600,
        },
    )
    if not isinstance(created, dict):
        raise CLIError("Release preview creation returned an invalid response.")
    raw_key = created.get("runtime_api_key")
    key_id = str(created.get("runtime_key_id") or "")
    binding_id = str(created.get("binding_id") or "")
    if not isinstance(raw_key, str) or not raw_key or not key_id or not binding_id:
        raise CLIError("Release preview creation did not return usable runtime authority.")
    updated = config.model_copy(
        update={
            "active_project": project_id,
            "runtime_api_key": raw_key,
            "runtime_key_id": key_id,
            "runtime_key_project_id": project_id,
            "runtime_key_scopes": list(RUNTIME_KEY_SCOPES),
            "runtime_key_mode": "test",
            "release_preview_binding_id": binding_id,
            "release_preview_release_id": release_id,
            "release_preview_fingerprint": fingerprint,
            "release_preview_expires_at": str(created.get("expires_at") or ""),
        }
    )
    confirmed = _preview_keys(client.installer("GET", key_path, token=token))
    matching = [
        row
        for row in confirmed
        if str(row.get("id") or "") == key_id
        and str(row.get("preview_binding_id") or "") == binding_id
    ]
    if len(matching) != 1:
        _best_effort_revoke(client, token, project_id, binding_id)
        raise CLIError("Preview provisioning did not leave exactly one bound runtime key.")
    return ReleasePreviewProvisioning(
        action="rotated" if rotate else "created",
        config=updated,
        key=matching[0],
        active_matching_count=1,
    )


def finalize_verified_release(
    client: Any,
    *,
    token: str,
    config: CLIConfig,
    project_id: str,
    release_id: str,
    release_fingerprint: str,
    checks: list[dict[str, Any]],
) -> ReleaseFinalization:
    """Verify/promote exact evidence and atomically select durable Test authority."""
    releases = client.installer(
        "GET",
        f"/projects/{encode_path_segment(project_id)}/releases",
        token=token,
    )
    release = _release_row(releases, release_id, release_fingerprint)
    if release.get("status") == "candidate":
        verified = client.installer(
            "POST",
            (
                f"/projects/{encode_path_segment(project_id)}/releases/"
                f"{encode_path_segment(release_id)}/verify"
            ),
            token=token,
            json={"checks": checks},
        )
        if not isinstance(verified, dict) or verified.get("status") != "verified":
            raise CLIError("Release verification did not produce a verified release.")
        release = dict(verified)
    elif release.get("status") != "verified":
        raise CLIError("Only the exact candidate or verified release may be finalized.")

    deployment = client.installer(
        "POST",
        (
            f"/projects/{encode_path_segment(project_id)}/releases/"
            f"{encode_path_segment(release_id)}/promote"
        ),
        token=token,
        json={
            "runtime_mode": "test",
            "idempotency_key": f"cli-finalize-{project_id}-{release_id}-test",
        },
    )
    if (
        not isinstance(deployment, dict)
        or str(deployment.get("active_release_id") or "") != release_id
    ):
        raise CLIError("Test promotion did not bind the exact verified release.")

    key_path = f"/projects/{encode_path_segment(project_id)}/runtime-keys"
    runtime_keys = _normal_launch_keys(client.installer("GET", key_path, token=token))
    reusable = _reusable_normal_key(config, runtime_keys, project_id=project_id)
    if reusable is not None:
        return ReleaseFinalization(
            action="reused",
            config=config.model_copy(update=_durable_key_config(config, project_id, reusable)),
            key=reusable,
            release=release,
            deployment=dict(deployment),
        )
    if runtime_keys:
        raise CLIError(
            "A durable launch runtime key exists but its raw value is not in this CLI profile. "
            "Rotate it explicitly before finalizing."
        )
    created = client.installer(
        "POST",
        key_path,
        token=token,
        json={
            "name": "One-prompt launch app backend",
            "scopes": list(RUNTIME_KEY_SCOPES),
            "runtime_mode": "test",
        },
    )
    if not isinstance(created, dict) or not isinstance(created.get("api_key"), str):
        raise CLIError("Durable Test runtime-key creation returned an invalid response.")
    updated = config.model_copy(
        update={
            **_durable_key_config(config, project_id, created),
            "runtime_api_key": str(created["api_key"]),
        }
    )
    confirmed = _normal_launch_keys(client.installer("GET", key_path, token=token))
    matching = [row for row in confirmed if str(row.get("id") or "") == updated.runtime_key_id]
    if len(matching) != 1:
        key_id = str(created.get("id") or "")
        if key_id:
            try:
                client.installer("DELETE", f"{key_path}/{encode_path_segment(key_id)}", token=token)
            except Exception:
                pass
        raise CLIError("Finalization did not leave exactly one durable Test runtime key.")
    return ReleaseFinalization(
        action="created",
        config=updated,
        key=matching[0],
        release=release,
        deployment=dict(deployment),
    )


def _preview_keys(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [
        dict(row)
        for row in payload["items"]
        if isinstance(row, dict) and row.get("preview_binding_id")
    ]


def _normal_launch_keys(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return []
    return [
        dict(row)
        for row in payload["items"]
        if isinstance(row, dict)
        and row.get("name") == "One-prompt launch app backend"
        and row.get("runtime_mode") == "test"
        and not row.get("preview_binding_id")
    ]


def _release_row(payload: object, release_id: str, fingerprint: str) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise CLIError("Project release listing returned an invalid response.")
    matches = [
        dict(row)
        for row in payload
        if isinstance(row, dict)
        and str(row.get("id") or "") == release_id
        and str(row.get("fingerprint") or "") == fingerprint
    ]
    if len(matches) != 1:
        raise CLIError("The provisioned release fingerprint no longer matches server state.")
    return matches[0]


def _reusable_normal_key(
    config: CLIConfig,
    keys: list[dict[str, Any]],
    *,
    project_id: str,
) -> dict[str, Any] | None:
    if any(
        (
            not config.runtime_api_key,
            config.runtime_key_project_id != project_id,
            config.runtime_key_mode != "test",
            set(config.runtime_key_scopes) != set(RUNTIME_KEY_SCOPES),
            not config.runtime_key_id,
            bool(config.release_preview_binding_id),
        )
    ):
        return None
    matches = [row for row in keys if str(row.get("id") or "") == config.runtime_key_id]
    return matches[0] if len(matches) == 1 else None


def _durable_key_config(
    config: CLIConfig,
    project_id: str,
    key: dict[str, Any],
) -> dict[str, Any]:
    return {
        "active_project": project_id,
        "runtime_api_key": config.runtime_api_key,
        "runtime_key_id": str(key.get("id") or ""),
        "runtime_key_project_id": project_id,
        "runtime_key_scopes": list(RUNTIME_KEY_SCOPES),
        "runtime_key_mode": "test",
        "release_preview_binding_id": None,
        "release_preview_release_id": None,
        "release_preview_fingerprint": None,
        "release_preview_expires_at": None,
    }


def _reusable_preview(
    config: CLIConfig,
    keys: list[dict[str, Any]],
    *,
    project_id: str,
    release_id: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    if any(
        (
            not config.runtime_api_key,
            config.runtime_key_project_id != project_id,
            config.runtime_key_mode != "test",
            set(config.runtime_key_scopes) != set(RUNTIME_KEY_SCOPES),
            config.release_preview_release_id != release_id,
            config.release_preview_fingerprint != fingerprint,
            not config.release_preview_binding_id,
            not config.runtime_key_id,
        )
    ):
        return None
    matches = [
        row
        for row in keys
        if str(row.get("id") or "") == config.runtime_key_id
        and str(row.get("preview_binding_id") or "") == config.release_preview_binding_id
    ]
    if len(matches) != 1:
        return None
    expires_at = _expiry(matches[0].get("expires_at"))
    if expires_at is None or expires_at <= datetime.now(UTC) + MINIMUM_PREVIEW_REMAINING:
        return None
    return matches[0]


def _expiry(value: object) -> datetime | None:
    """Parse one server expiration without trusting malformed key metadata."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _best_effort_revoke(
    client: Any,
    token: str,
    project_id: str,
    binding_id: str,
) -> None:
    try:
        client.installer(
            "DELETE",
            (
                f"/projects/{encode_path_segment(project_id)}/release-previews/"
                f"{encode_path_segment(binding_id)}"
            ),
            token=token,
        )
    except Exception:
        pass


def _required(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "")
    if not value:
        raise CLIError(f"Project release is missing {key}.")
    return value
