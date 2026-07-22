"""Focused tests for candidate preview and durable release authority."""

from __future__ import annotations

from typing import Any

from platform_cli.config import CLIConfig
from platform_cli.launch_release import finalize_verified_release, provision_release_preview


class FakeClient:
    """Ordered installer client that retains non-sensitive request metadata."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def installer(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json: dict[str, Any] | None = None,
    ) -> object:
        self.requests.append({"method": method, "path": path, "token": token, "json": json})
        return self.responses.pop(0)


def preview_key(*, key_id: str = "key-preview", binding_id: str = "binding-1") -> dict[str, Any]:
    return {
        "id": key_id,
        "name": "General Augment launch preview",
        "masked_key": "ga_test_...",
        "project_id": "project-1",
        "runtime_mode": "test",
        "scopes": ["responses:create"],
        "preview_binding_id": binding_id,
        "expires_at": "2099-07-16T12:00:00Z",
    }


def test_preview_rerun_reuses_exact_bound_key_without_returning_secret() -> None:
    row = preview_key()
    client = FakeClient([{"items": [row]}])
    config = CLIConfig(
        runtime_api_key="ga_test_local_only",
        runtime_key_id="key-preview",
        runtime_key_project_id="project-1",
        runtime_key_scopes=["responses:create"],
        runtime_key_mode="test",
        release_preview_binding_id="binding-1",
        release_preview_release_id="release-1",
        release_preview_fingerprint="e" * 64,
    )

    result = provision_release_preview(
        client,
        token="installer-token",
        config=config,
        project_id="project-1",
        launch_session_id="launch-session",
        release={"id": "release-1", "fingerprint": "e" * 64, "status": "candidate"},
        rotate=False,
    )

    assert result.action == "reused"
    assert result.config.runtime_api_key == "ga_test_local_only"
    assert len(client.requests) == 1
    assert "ga_test_local_only" not in repr(result)


def test_preview_without_local_secret_revokes_orphan_before_replacement() -> None:
    old = preview_key()
    new = preview_key(key_id="key-new", binding_id="binding-new")
    client = FakeClient(
        [
            {"items": [old]},
            {"status": "revoked"},
            {
                "binding_id": "binding-new",
                "runtime_key_id": "key-new",
                "runtime_api_key": "ga_test_replacement",
                "expires_at": "2026-07-16T13:00:00Z",
            },
            {"items": [new]},
        ]
    )

    result = provision_release_preview(
        client,
        token="installer-token",
        config=CLIConfig(),
        project_id="project-1",
        launch_session_id="launch-session",
        release={"id": "release-1", "fingerprint": "e" * 64, "status": "candidate"},
        rotate=False,
    )

    assert result.action == "created"
    assert result.config.runtime_api_key == "ga_test_replacement"
    assert [row["method"] for row in client.requests] == ["GET", "DELETE", "POST", "GET"]
    assert client.requests[1]["path"].endswith("/release-previews/binding-1")
    assert "ga_test_replacement" not in repr(result)


def test_preview_near_expiry_rotates_before_verification_window() -> None:
    """A locally held preview key must not expire during application verification."""
    old = {**preview_key(), "expires_at": "2026-07-16T21:10:00Z"}
    new = preview_key(key_id="key-new", binding_id="binding-new")
    client = FakeClient(
        [
            {"items": [old]},
            {"status": "revoked"},
            {
                "binding_id": "binding-new",
                "runtime_key_id": "key-new",
                "runtime_api_key": "ga_test_replacement",
                "expires_at": "2099-07-16T13:00:00Z",
            },
            {"items": [new]},
        ]
    )
    config = CLIConfig(
        runtime_api_key="ga_test_local_only",
        runtime_key_id="key-preview",
        runtime_key_project_id="project-1",
        runtime_key_scopes=["responses:create"],
        runtime_key_mode="test",
        release_preview_binding_id="binding-1",
        release_preview_release_id="release-1",
        release_preview_fingerprint="e" * 64,
    )

    result = provision_release_preview(
        client,
        token="installer-token",
        config=config,
        project_id="project-1",
        launch_session_id="launch-session",
        release={"id": "release-1", "fingerprint": "e" * 64, "status": "candidate"},
        rotate=False,
    )

    assert result.action == "created"
    assert result.config.runtime_key_id == "key-new"
    assert [row["method"] for row in client.requests] == ["GET", "DELETE", "POST", "GET"]


def test_finalize_verifies_promotes_and_replaces_preview_with_durable_key() -> None:
    durable = {
        "id": "key-durable",
        "name": "One-prompt launch app backend",
        "masked_key": "ga_test_...",
        "project_id": "project-1",
        "runtime_mode": "test",
        "scopes": ["responses:create"],
        "preview_binding_id": None,
    }
    release = {"id": "release-1", "fingerprint": "e" * 64, "status": "candidate"}
    verified = {**release, "status": "verified"}
    client = FakeClient(
        [
            [release],
            verified,
            {"active_release_id": "release-1", "runtime_mode": "test"},
            {"items": []},
            {**durable, "api_key": "ga_test_durable"},
            {"items": [durable]},
        ]
    )
    config = CLIConfig(
        runtime_api_key="ga_test_preview",
        runtime_key_id="key-preview",
        runtime_key_project_id="project-1",
        runtime_key_scopes=["responses:create"],
        runtime_key_mode="test",
        release_preview_binding_id="binding-1",
        release_preview_release_id="release-1",
        release_preview_fingerprint="e" * 64,
    )

    result = finalize_verified_release(
        client,
        token="installer-token",
        config=config,
        project_id="project-1",
        release_id="release-1",
        release_fingerprint="e" * 64,
        checks=[{"name": "check", "status": "PASS"}],
    )

    assert result.action == "created"
    assert result.config.runtime_api_key == "ga_test_durable"
    assert result.config.release_preview_binding_id is None
    assert result.key == durable
    assert [row["method"] for row in client.requests] == [
        "GET",
        "POST",
        "POST",
        "GET",
        "POST",
        "GET",
    ]
    assert "ga_test_durable" not in repr(result)
