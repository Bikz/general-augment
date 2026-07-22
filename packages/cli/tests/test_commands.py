"""Command tests for the standalone (public) CLI package."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import time
import tomllib
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar, cast

import httpx
import pytest
import typer
import yaml
from typer.testing import CliRunner

from platform_cli.client import PlatformClient
from platform_cli.commands import auth as auth_command
from platform_cli.commands.launch import (
    _launch_artifact,
    _matching_launch_runtime_keys,
    _release_intent,
    _server_release_check,
)
from platform_cli.commands.setup import _select_or_create_project
from platform_cli.config import CLIConfig
from platform_cli.errors import CLIError, helpful_api_error
from platform_cli.launch_contract import build_launch_manifest, write_launch_manifest
from platform_cli.launch_verification import (
    REQUIRED_BETA_CHECKS,
    launch_session_fingerprint,
    manifest_fingerprint,
)
from platform_cli.main import app
from platform_cli.runtime import Runtime
from platform_cli.self_serve import (
    dashboard_observability_url,
    dashboard_project_url,
    installer_access_token,
)
from platform_cli.workspace_inspector import inspect_workspace

ROOT = Path(__file__).resolve().parents[3]
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PLACEHOLDER_MCP_URL = "https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}"
CANARY_SECRETS = (
    "sk-delegated-coding-secret",
    "must-not-leak",
    "sk-research-secret",
    "bb-secret",
    "support-token-secret",
    "support-api-key-secret",
)
RICH_BOX_TRANSLATION = dict.fromkeys(
    map(ord, ("\u2500", "\u2502", "\u256d", "\u256e", "\u2570", "\u256f")),
    " ",
)


class FakeHTTPClient:
    """Fake httpx.Client implementation for command tests."""

    requests: ClassVar[list[dict[str, Any]]] = []
    queue: ClassVar[list[httpx.Response]] = []

    def __init__(self, timeout: float) -> None:
        """Capture timeout."""
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Capture request data and return queued response."""
        self.requests.append(
            {"method": method, "url": url, "headers": headers or {}, "json": json, "params": params}
        )
        response = self.queue.pop(0) if self.queue else json_response({})
        response.request = httpx.Request(method, url)
        return response

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> _FakeStreamResponse:
        """Capture streaming request data and return queued SSE response."""
        self.requests.append(
            {"method": method, "url": url, "headers": headers or {}, "json": json, "params": params}
        )
        response = self.queue.pop(0) if self.queue else json_response({})
        response.request = httpx.Request(method, url)
        return _FakeStreamResponse(response)

    def close(self) -> None:
        """Close fake client."""


class _FakeStreamResponse:
    """Tiny context manager that exposes queued JSON as semantic SSE lines."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.status_code = response.status_code
        self.headers = response.headers

    def __enter__(self) -> _FakeStreamResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._response.read()

    def iter_lines(self) -> Iterator[str]:
        payload = self._response.json()
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            for item in payload["items"]:
                yield "event: agent_run.event"
                yield f"data: {json.dumps(item)}"
                yield ""
            yield "event: agent_run.event_stream.done"
            yield f"data: {json.dumps({'status': payload.get('status', '')})}"
            yield ""
            return
        yield "event: message"
        yield f"data: {json.dumps(payload)}"
        yield ""


class _FakeLocalCallback:
    """Fake loopback callback server for browser auth tests."""

    redirect_uri = "http://127.0.0.1:49231/callback"

    def __init__(self, code: str) -> None:
        self.code = code
        self.wait_timeout: float | None = None
        self.closed = False

    def wait(self, timeout: float) -> str:
        self.wait_timeout = timeout
        return self.code

    def close(self) -> None:
        self.closed = True


def plain_cli_output(result: Any) -> str:
    """Return CLI output without ANSI or Rich box wrapping artifacts."""
    output = ANSI_RE.sub("", str(result.output))
    output = output.translate(RICH_BOX_TRANSLATION)
    return " ".join(output.split())


def compact_cli_output(result: Any) -> str:
    """Return CLI output compacted for option strings that Rich can wrap mid-token."""
    return plain_cli_output(result).replace(" ", "")


def assert_no_canary_secrets(value: object) -> None:
    """Assert serialized output does not contain fake raw secrets."""
    serialized = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    leaked = [secret for secret in CANARY_SECRETS if secret in serialized]
    assert not leaked, "Raw canary secrets leaked into output: " + ", ".join(leaked)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch HTTP calls for every command test."""
    FakeHTTPClient.requests = []
    FakeHTTPClient.queue = []
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)


class _FixedVerifyUUID:
    hex = "abcdef1234567890"


def test_auth_login_logout_whoami(tmp_path: Path) -> None:
    """Auth commands should write config, call /me, and clear config."""
    config_path = tmp_path / "config.yaml"
    runner = CliRunner()
    FakeHTTPClient.queue = [
        json_response({"auth_method": "api_key", "project_id": "p1", "project_ids": []}),
        json_response({"auth_method": "api_key", "project_id": "p1", "project_ids": []}),
        json_response({"auth_method": "api_key", "project_id": "p1", "project_ids": []}),
    ]

    login = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "auth",
            "login",
            "--api-key",
            "secret",
            "--base-url",
            "http://api.test",
        ],
    )
    whoami = runner.invoke(app, ["--config", str(config_path), "auth", "whoami"])
    whoami_json = runner.invoke(app, ["--config", str(config_path), "auth", "whoami", "--json"])
    logout = runner.invoke(app, ["--config", str(config_path), "auth", "logout"])

    assert login.exit_code == 0
    assert "Verified API access" in login.output
    assert "projects: p1" in login.output
    assert whoami.exit_code == 0
    assert "api_key" in whoami.output
    assert "Project IDs: p1" in whoami.output
    assert whoami_json.exit_code == 0
    identity = json.loads(whoami_json.output)
    assert identity["authenticated"] is True
    assert identity["auth_method"] == "api_key"
    assert identity["project_ids"] == ["p1"]
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/admin/me"
    assert FakeHTTPClient.requests[1]["url"] == "http://api.test/api/v1/admin/me"
    assert FakeHTTPClient.requests[2]["url"] == "http://api.test/api/v1/admin/me"
    assert logout.exit_code == 0
    assert not config_path.exists()


def test_launch_provision_separates_authority_reuses_key_and_writes_ignored_env(
    tmp_path: Path,
) -> None:
    """Provision must use installer auth for config and runtime auth only for the app."""
    workspace = tmp_path / "habit-app"
    route = workspace / "app" / "api" / "assistant"
    route.mkdir(parents=True)
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.5.18", "@clerk/nextjs": "7.2.4"}}),
        encoding="utf-8",
    )
    (route / "route.ts").write_text(
        "import { auth } from '@clerk/nextjs/server';\nexport async function POST() { "
        "const { userId } = await auth(); return Response.json({ userId }); }\n",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(".env.local\n.genaug/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": "legacy-management-key",
                "active_project": "project-1",
                "metadata": {"installer": {"access_token": "gainst_installer_secret"}},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    plan = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "launch",
            "--workspace",
            str(workspace),
            "--plan",
            "--json",
        ],
    )
    assert plan.exit_code == 0, plan.output
    session_id = json.loads(plan.output)["session_id"]
    runtime_key_row = {
        "id": "key-1",
        "name": "General Augment launch preview",
        "masked_key": "ga...cret",
        "project_id": "project-1",
        "scopes": ["responses:create"],
        "runtime_mode": "test",
        "preview_binding_id": "binding-1",
        "expires_at": "2099-07-13T13:00:00Z",
        "created_at": "2026-07-13T12:00:00Z",
    }
    approved = {
        "session_id": session_id,
        "status": "approved",
        "fingerprint": "f" * 64,
    }
    release_row = {
        "id": "release-1",
        "project_id": "project-1",
        "status": "candidate",
        "fingerprint": "e" * 64,
    }
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "project-1", "slug": "habit-app", "name": "Habit"}]}),
        json_response(approved),
        json_response({"id": "project-1", "slug": "habit-app", "name": "Habit"}),
        json_response(release_row),
        json_response({"items": []}),
        json_response(
            {
                "binding_id": "binding-1",
                "release_id": "release-1",
                "runtime_key_id": "key-1",
                "runtime_api_key": "ga_runtime_secret_once",
                "expires_at": "2099-07-13T13:00:00Z",
            }
        ),
        json_response({"items": [runtime_key_row]}),
    ]
    args = [
        "--config",
        str(config_path),
        "launch",
        "--workspace",
        str(workspace),
        "--provision",
        "--approve-session",
        session_id,
        "--configure-application-env",
        "--json",
    ]

    first = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert "ga_runtime_secret_once" not in first.output
    first_payload = json.loads(first.output)
    assert first_payload["runtime_key"]["action"] == "created"
    assert first_payload["runtime_key"]["active_matching_count"] == 1
    assert first_payload["control_plane_authority"] == "installer"
    assert first_payload["application_authority"] == "runtime_api_key"
    assert first_payload["release"] == {
        "id": "release-1",
        "fingerprint": "e" * 64,
        "status": "candidate",
        "intent": "test",
    }
    assert all("/api/v1/admin" not in request["url"] for request in FakeHTTPClient.requests)
    config_request = next(
        request for request in FakeHTTPClient.requests if request["url"].endswith("/config")
    )
    assert config_request["headers"] == {"Authorization": "Bearer gainst_installer_secret"}
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["api_key"] == "legacy-management-key"
    assert persisted["runtime_api_key"] == "ga_runtime_secret_once"
    assert persisted["runtime_key_scopes"] == ["responses:create"]
    assert persisted["runtime_key_mode"] == "test"
    assert persisted["release_preview_binding_id"] == "binding-1"
    assert persisted["release_preview_release_id"] == "release-1"
    runtime_key_create = next(
        request
        for request in FakeHTTPClient.requests
        if request["method"] == "POST" and request["url"].endswith("/preview")
    )
    assert runtime_key_create["json"]["launch_session_id"] == session_id
    assert runtime_key_create["json"]["expected_release_fingerprint"] == "e" * 64
    env_content = (workspace / ".env.local").read_text(encoding="utf-8")
    assert "GENAUG_API_KEY=ga_runtime_secret_once" in env_content
    assert (workspace / ".env.local").stat().st_mode & 0o777 == 0o600
    receipt = workspace / ".genaug" / "provisioning-receipt.json"
    assert receipt.exists()
    assert "ga_runtime_secret_once" not in receipt.read_text(encoding="utf-8")
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_payload["runtime_key"] == {
        "action": "created",
        "active_matching_count": 1,
        "authority": "candidate_test_preview",
        "expires_at": "2099-07-13T13:00:00Z",
        "id": "key-1",
        "masked_key": "ga...cret",
        "preview_binding_id": "binding-1",
        "scopes": ["responses:create"],
    }
    assert receipt_payload["authorities"]["runtime_execution"] == "candidate_test_preview"

    FakeHTTPClient.requests = []
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "project-1", "slug": "habit-app", "name": "Habit"}]}),
        json_response(approved),
        json_response({"id": "project-1", "slug": "habit-app", "name": "Habit"}),
        json_response(release_row),
        json_response({"items": [runtime_key_row]}),
    ]

    second = runner.invoke(app, args)

    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["runtime_key"]["action"] == "reused"
    assert not any(
        request["method"] == "POST" and request["url"].endswith("/preview")
        for request in FakeHTTPClient.requests
    )

    rotated_key_row = {
        **runtime_key_row,
        "id": "key-2",
        "masked_key": "ga...ated",
        "preview_binding_id": "binding-2",
    }
    FakeHTTPClient.requests = []
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "project-1", "slug": "habit-app", "name": "Habit"}]}),
        json_response(approved),
        json_response({"id": "project-1", "slug": "habit-app", "name": "Habit"}),
        json_response(release_row),
        json_response({"items": [runtime_key_row]}),
        json_response({"status": "revoked", "id": "binding-1"}),
        json_response(
            {
                "binding_id": "binding-2",
                "release_id": "release-1",
                "runtime_key_id": "key-2",
                "runtime_api_key": "ga_runtime_rotated_once",
                "expires_at": "2026-07-13T14:00:00Z",
            }
        ),
        json_response({"items": [rotated_key_row]}),
    ]

    rotated = runner.invoke(app, [*args, "--rotate-runtime-key"])

    assert rotated.exit_code == 0, rotated.output
    assert "ga_runtime_rotated_once" not in rotated.output
    assert json.loads(rotated.output)["runtime_key"]["action"] == "rotated"
    post_index = next(
        index
        for index, request in enumerate(FakeHTTPClient.requests)
        if request["method"] == "POST" and request["url"].endswith("/preview")
    )
    delete_index = next(
        index
        for index, request in enumerate(FakeHTTPClient.requests)
        if request["method"] == "DELETE" and request["url"].endswith("/binding-1")
    )
    assert delete_index < post_index
    assert "GENAUG_API_KEY=ga_runtime_rotated_once" in (workspace / ".env.local").read_text(
        encoding="utf-8"
    )
    persisted_after_rotation = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted_after_rotation["runtime_key_id"] == "key-2"

    FakeHTTPClient.requests = []
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "project-1", "slug": "habit-app", "name": "Habit"}]}),
        json_response(approved),
        json_response({"id": "project-1", "slug": "habit-app", "name": "Habit"}),
        json_response(release_row),
        json_response({"items": [rotated_key_row]}),
        json_response({"detail": "temporary revoke failure"}, status_code=503),
    ]

    failed_rotation = runner.invoke(app, [*args, "--rotate-runtime-key"])

    assert failed_rotation.exit_code == 1
    assert not any(
        request["method"] == "POST" and request["url"].endswith("/preview")
        for request in FakeHTTPClient.requests
    )
    restored = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert restored["runtime_key_id"] == "key-2"
    restored_env = (workspace / ".env.local").read_text(encoding="utf-8")
    assert "GENAUG_API_KEY=ga_runtime_rotated_once" in restored_env


def test_launch_finalize_requires_ready_evidence_and_installs_durable_key(
    tmp_path: Path,
) -> None:
    """Finalization promotes exact evidence and replaces preview authority without leaking it."""
    workspace = tmp_path / "habit-app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.5.18"}}),
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(".env.local\n.genaug/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    manifest_path = workspace / "genaug-agent.yaml"
    manifest = build_launch_manifest(workspace, inspect_workspace(workspace))
    write_launch_manifest(manifest_path, manifest, workspace=workspace)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    artifact = _launch_artifact(
        workspace,
        manifest_path,
        inspect_workspace(workspace),
        manifest,
    )
    now = datetime.now(UTC).isoformat()
    genaug = workspace / ".genaug"
    genaug.mkdir()
    checks = [
        {
            "name": name,
            "required": True,
            "status": "PASS",
            "reason_code": f"{name}_verified",
            "detail": "Verified by the isolated certification run.",
            "evidence": [{"artifact_sha256": "a" * 64}],
            "checked_at": now,
        }
        for name in REQUIRED_BETA_CHECKS
    ]
    (genaug / "launch-verification.json").write_text(
        json.dumps(
            {
                "schema_version": "general-augment-launch-verification/v1",
                "verdict": "READY",
                "verified_at": now,
                "manifest_fingerprint": manifest_fingerprint(manifest),
                "session_id": artifact["session_id"],
                "checks": checks,
            }
        ),
        encoding="utf-8",
    )
    manifest_content = manifest_path.read_text(encoding="utf-8")
    (genaug / "provisioning-receipt.json").write_text(
        json.dumps(
            {
                "schema_version": "general-augment-provisioning-receipt/v1",
                "session_id": artifact["session_id"],
                "project_id": "project-1",
                "approved_plan_fingerprint": launch_session_fingerprint(artifact),
                "manifest_sha256": hashlib.sha256(manifest_content.encode("utf-8")).hexdigest(),
                "release": {
                    "id": "release-1",
                    "fingerprint": "e" * 64,
                    "status": "candidate",
                },
                "environment": {
                    "status": "action_required",
                    "target": str(workspace / ".env.local"),
                },
                "checked_at": now,
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "active_project": "project-1",
                "runtime_api_key": "ga_preview_secret",
                "runtime_key_id": "preview-key",
                "runtime_key_project_id": "project-1",
                "runtime_key_scopes": ["responses:create"],
                "runtime_key_mode": "test",
                "release_preview_binding_id": "binding-1",
                "release_preview_release_id": "release-1",
                "release_preview_fingerprint": "e" * 64,
                "metadata": {"installer": {"access_token": "installer-secret"}},
            }
        ),
        encoding="utf-8",
    )
    durable = {
        "id": "durable-key",
        "name": "One-prompt launch app backend",
        "masked_key": "ga_test_...",
        "project_id": "project-1",
        "scopes": ["responses:create"],
        "runtime_mode": "test",
        "preview_binding_id": None,
    }
    release = {"id": "release-1", "fingerprint": "e" * 64, "status": "candidate"}
    FakeHTTPClient.queue = [
        json_response([release]),
        json_response({**release, "status": "verified"}),
        json_response({"active_release_id": "release-1", "runtime_mode": "test"}),
        json_response({"items": []}),
        json_response({**durable, "api_key": "ga_durable_secret"}),
        json_response({"items": [durable]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "launch",
            "--workspace",
            str(workspace),
            "--finalize",
            "--configure-application-env",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ga_preview_secret" not in result.output
    assert "ga_durable_secret" not in result.output
    payload = json.loads(result.output)
    assert payload["status"] == "FINALIZED"
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["runtime_key_id"] == "durable-key"
    assert persisted["release_preview_binding_id"] is None
    env = (workspace / ".env.local").read_text(encoding="utf-8")
    assert "GENAUG_API_KEY=ga_durable_secret" in env
    receipt = (workspace / ".genaug" / "launch-finalization.json").read_text(encoding="utf-8")
    assert "ga_durable_secret" not in receipt


def test_launch_review_reuses_project_with_generated_name_and_slug(tmp_path: Path) -> None:
    """Review reruns must reuse the project created before a later phase fails."""
    existing = {"id": "project-1", "slug": "habit-app", "name": "Habit Assistant"}

    selected = _select_or_create_project(
        cast(PlatformClient, object()),
        token="unused",
        workspace=tmp_path,
        projects_payload={"items": [existing]},
        project=None,
        project_name="Habit Assistant",
        project_slug="habit-app",
    )

    assert selected is existing


def test_launch_review_binds_without_regenerating_reviewed_multi_agent_plan(
    tmp_path: Path,
) -> None:
    """Hosted review must receive the exact declared topology and release intent."""
    workspace = tmp_path / "habit-app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.5.18"}}),
        encoding="utf-8",
    )
    manifest = build_launch_manifest(workspace, {"detected": {"frameworks": ["nextjs"]}})
    manifest["tools"] = {"builtin": ["web_search"], "mcp": []}
    manifest["agents"][0]["tools"] = ["web_search"]
    manifest["agents"][0]["skills"] = ["habit-coaching@1.0.0"]
    manifest["agents"].append(
        {
            "name": "triage",
            "display_name": "Triage",
            "entry": False,
            "personality": {"role": "Triage habit questions"},
            "model": dict(manifest["agents"][0]["model"]),
            "tools": [],
            "skills": [],
            "memory": {"user_profile": "read"},
            "delegations": [],
        }
    )
    manifest["agents"][0]["delegations"] = [{"to": "triage", "mode": "as_tool"}]
    manifest["x-general-augment-launch"]["project"] = {
        "create": True,
        "name": "Habit App",
        "slug": "habit-app",
        "workspace": {"ref": "workspace-1"},
    }
    manifest["x-general-augment-launch"]["release"] = {
        "intent": "live",
        "activation_allowed": False,
        "requires_verified_release": True,
    }
    write_launch_manifest(
        workspace / "genaug-agent.yaml",
        manifest,
        workspace=workspace,
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "items": [
                    {
                        "id": "workspace-1",
                        "name": "Personal workspace",
                        "slug": "personal",
                        "kind": "personal",
                        "role": "owner",
                    }
                ]
            }
        ),
        json_response({"items": []}),
        json_response(
            {
                "id": "project-1",
                "workspace_id": "workspace-1",
                "name": "Habit App",
                "slug": "habit-app",
            }
        ),
        json_response({"session_id": "launch_reviewed_exactly"}),
        json_response(
            {
                "session_id": "launch_reviewed_exactly",
                "status": "review_required",
                "approval_source": "none",
                "approval_reason": "launch_review_required",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "launch",
            "--workspace",
            str(workspace),
            "--review",
            "--account-workspace",
            "workspace-1",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    persisted = yaml.safe_load(
        (workspace / "genaug-agent.yaml").read_text(encoding="utf-8")
    )
    assert [agent["name"] for agent in persisted["agents"]] == [
        manifest["agents"][0]["name"],
        "triage",
    ]
    assert persisted["agents"][0]["tools"] == ["web_search"]
    assert persisted["agents"][0]["skills"] == ["habit-coaching@1.0.0"]
    assert persisted["agents"][0]["delegations"] == [
        {"to": "triage", "mode": "as_tool"}
    ]
    assert persisted["x-general-augment-launch"]["release"]["intent"] == "live"
    assert persisted["x-general-augment-launch"]["project"] == {
        "ref": "project-1",
        "link_state": "linked",
        "workspace": {"ref": "workspace-1"},
    }
    launch_request = next(
        request
        for request in FakeHTTPClient.requests
        if request["method"] == "POST" and request["url"].endswith("/launch-sessions")
    )
    assert launch_request["url"].endswith("/projects/project-1/launch-sessions")
    assert launch_request["json"]["manifest_schema_version"] == "genaug/v2"
    assert launch_request["json"]["approval_mode"] == "required"
    uploaded = yaml.safe_load(launch_request["json"]["configuration"]["yaml_content"])
    assert uploaded["agents"] == persisted["agents"]
    assert launch_request["json"]["plan"]["release"]["intent"] == "live"


def test_launch_review_can_request_bounded_server_policy_without_granting_it_locally(
    tmp_path: Path,
) -> None:
    """The CLI may request safe auto-approval, but only reports the server's decision."""
    workspace = tmp_path / "safe-app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.5.18"}}),
        encoding="utf-8",
    )
    manifest = build_launch_manifest(workspace, {"detected": {"frameworks": ["nextjs"]}})
    write_launch_manifest(workspace / "genaug-agent.yaml", manifest, workspace=workspace)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "items": [
                    {
                        "id": "workspace-1",
                        "name": "Personal workspace",
                        "slug": "personal",
                        "kind": "personal",
                        "role": "owner",
                    }
                ]
            }
        ),
        json_response(
            {
                "items": [
                    {
                        "id": "project-1",
                        "workspace_id": "workspace-1",
                        "name": "Safe App",
                        "slug": "safe-app",
                    }
                ]
            }
        ),
        json_response({"session_id": "launch_safe_policy"}),
        json_response(
            {
                "session_id": "launch_safe_policy",
                "status": "approved",
                "approval_source": "policy",
                "approval_reason": "standing_policy_safe_change",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "launch",
            "--workspace",
            str(workspace),
            "--review",
            "--project",
            "project-1",
            "--account-workspace",
            "workspace-1",
            "--auto-approve-safe",
            "--no-browser",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "APPROVED"
    assert payload["approval_source"] == "policy"
    launch_request = next(
        request
        for request in FakeHTTPClient.requests
        if request["method"] == "POST" and request["url"].endswith("/launch-sessions")
    )
    assert launch_request["json"]["approval_mode"] == "safe_auto"


def test_agent_cli_is_read_only_and_points_mutations_to_declarative_launch(
    tmp_path: Path,
) -> None:
    """Installer Agent changes must not bypass the reviewed manifest fingerprint."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "active_project": "project-1",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            [
                {
                    "id": "agent-1",
                    "name": "Habit Assistant",
                    "slug": "habit-assistant",
                    "is_entry": True,
                    "status": "active",
                }
            ]
        )
    ]

    status = CliRunner().invoke(
        app,
        ["--config", str(config_path), "agent", "status", "--json"],
    )
    help_result = CliRunner().invoke(app, ["agent", "--help"])

    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload == {
        "project_id": "project-1",
        "agent_count": 1,
        "entry_agent_ids": ["agent-1"],
        "configuration_source": "genaug-agent.yaml",
        "mutation_mode": "declarative_launch",
        "approval_enforcement": "server_fingerprint",
        "next": "genaug launch --activate --auto-approve-safe --json",
    }
    assert help_result.exit_code == 0
    compact_help = compact_cli_output(help_result)
    assert "list" in compact_help
    assert "show" in compact_help
    assert "status" in compact_help
    assert "create" not in compact_help
    assert "tools" not in compact_help
    assert "skills" not in compact_help
    assert FakeHTTPClient.requests[0]["method"] == "GET"
    assert FakeHTTPClient.requests[0]["url"].endswith("/projects/project-1/agents")


def test_launch_activate_falls_back_to_review_without_applying_configuration(
    tmp_path: Path,
) -> None:
    """A coding agent cannot turn an unapproved launch request into authority."""
    workspace = tmp_path / "review-required-app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "15.5.18"}}),
        encoding="utf-8",
    )
    manifest = build_launch_manifest(workspace, {"detected": {"frameworks": ["nextjs"]}})
    write_launch_manifest(workspace / "genaug-agent.yaml", manifest, workspace=workspace)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "items": [
                    {
                        "id": "workspace-1",
                        "name": "Personal workspace",
                        "slug": "personal",
                        "kind": "personal",
                        "role": "owner",
                    }
                ]
            }
        ),
        json_response(
            {
                "items": [
                    {
                        "id": "project-1",
                        "workspace_id": "workspace-1",
                        "name": "Review Required App",
                        "slug": "review-required-app",
                    }
                ]
            }
        ),
        json_response({"session_id": "launch_review_required"}),
        json_response(
            {
                "session_id": "launch_review_required",
                "status": "review_required",
                "approval_source": "none",
                "approval_reason": "first_launch_requires_review",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "launch",
            "--workspace",
            str(workspace),
            "--activate",
            "--project",
            "project-1",
            "--account-workspace",
            "workspace-1",
            "--no-browser",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "REVIEW_REQUIRED"
    assert payload["approval_source"] == "none"
    assert payload["approval_reason"] == "first_launch_requires_review"
    assert payload["dashboard_review_url"].endswith(
        "/dashboard/projects/project-1/launch/launch_review_required"
    )
    assert all(not request["url"].endswith("/config") for request in FakeHTTPClient.requests)
    assert all("/runtime-keys" not in request["url"] for request in FakeHTTPClient.requests)
    launch_request = FakeHTTPClient.requests[2]
    assert launch_request["json"]["approval_mode"] == "safe_auto"


def test_launch_release_intent_controls_runtime_key_mode() -> None:
    """Test and Live keys remain distinct and follow the exact reviewed release intent."""
    artifact = {"plan": {"release": {"intent": "live"}}}
    keys = [
        {
            "name": "One-prompt launch app backend",
            "runtime_mode": "test",
            "scopes": ["responses:create"],
        },
        {
            "name": "One-prompt launch app backend",
            "runtime_mode": "live",
            "scopes": ["responses:create"],
        },
    ]

    assert _release_intent(artifact) == "live"
    assert _matching_launch_runtime_keys(keys, runtime_mode="live") == [keys[1]]


def test_server_release_check_uses_non_sensitive_evidence_ids() -> None:
    """Release verification forwards identifiers and digests, not captured evidence bodies."""
    row = {
        "name": "streaming_event_sequence",
        "status": "PASS",
        "reason_code": "stream_complete",
        "detail": "Observed the required sequence.",
        "checked_at": "2026-07-14T00:00:00Z",
        "evidence": [{"response_id": "resp_123", "event_types": ["created", "completed"]}],
    }

    projected = _server_release_check(row)

    assert projected["evidence_ids"] == ["resp_123"]
    assert "event_types" not in projected


def test_platform_client_uses_explicit_credentials_for_each_role() -> None:
    """Admin, installer, and runtime requests must not share credential material."""
    config = CLIConfig(
        base_url="http://api.test",
        api_key="management-secret",
        runtime_api_key="runtime-secret",
    )
    FakeHTTPClient.queue = [
        json_response({"id": "project-1"}),
        json_response({"status": "completed"}),
        json_response({"type": "response.completed", "response": {"id": "resp-1"}}),
    ]

    with PlatformClient(config) as client:
        client.admin("GET", "/projects/project-1")
        client.runtime_app("POST", "/v1/responses", json={"input": "hello"})
        list(client.runtime_response_event_stream(json={"input": "hello"}))

    assert FakeHTTPClient.requests[0]["headers"] == {"X-Admin-Key": "management-secret"}
    assert FakeHTTPClient.requests[1]["headers"] == {"Authorization": "Bearer runtime-secret"}
    assert FakeHTTPClient.requests[2]["headers"] == {
        "Authorization": "Bearer runtime-secret",
        "Accept": "text/event-stream",
    }
    assert FakeHTTPClient.requests[2]["json"]["stream"] is True


def test_installer_access_token_refreshes_expired_profile_without_leaking_tokens(
    tmp_path: Path,
) -> None:
    """Expired access auth should rotate both tokens and persist the replacement safely."""
    config_path = tmp_path / "config.yaml"
    config = CLIConfig(
        base_url="http://api.test",
        metadata={
            "installer": {
                "access_token": "expired-access-secret",
                "refresh_token": "old-refresh-secret",
                "expires_at": "2026-01-01T00:00:00Z",
                "scopes": ["projects:read"],
            }
        },
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "access_token": "replacement-access-secret",
                "refresh_token": "replacement-refresh-secret",
                "expires_at": "2027-01-01T00:00:00Z",
                "scopes": ["projects:read"],
                "project_id": None,
            }
        )
    ]
    runtime = Runtime(config=config, config_path=config_path, loaded_config_path=config_path)

    token = installer_access_token(runtime)

    assert token == "replacement-access-secret"
    assert FakeHTTPClient.requests[0]["json"] == {
        "grant_type": "refresh_token",
        "refresh_token": "old-refresh-secret",
    }
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["metadata"]["installer"]["access_token"] == token
    assert persisted["metadata"]["installer"]["refresh_token"] == ("replacement-refresh-secret")
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_auth_whoami_refreshes_expired_installer_access_token(tmp_path: Path) -> None:
    """Whoami should use the same refresh-aware installer authority as other commands."""
    config_path = tmp_path / "config.yaml"
    config = CLIConfig(
        base_url="http://api.test",
        api_key="runtime-key-must-not-be-used-for-whoami",
        metadata={
            "installer": {
                "access_token": "expired-access-secret",
                "refresh_token": "refresh-secret",
                "expires_at": "2026-01-01T00:00:00Z",
                "scopes": ["projects:read"],
            }
        },
    )
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    FakeHTTPClient.queue = [
        json_response(
            {
                "access_token": "replacement-access-secret",
                "refresh_token": "replacement-refresh-secret",
                "expires_at": "2027-01-01T00:00:00Z",
                "scopes": ["projects:read"],
            }
        ),
        json_response({"auth_method": "installer", "project_ids": ["project-1"]}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "auth", "whoami", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["project_ids"] == ["project-1"]
    assert FakeHTTPClient.requests[0]["json"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-secret",
    }
    assert FakeHTTPClient.requests[1]["headers"]["Authorization"] == (
        "Bearer replacement-access-secret"
    )


def test_dashboard_urls_honor_environment_and_use_real_observability_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All generated dashboard links should stay in the selected deployment."""
    monkeypatch.setenv("GENAUG_DASHBOARD_URL", "https://preview.example.test/")

    assert dashboard_project_url("project/id") == (
        "https://preview.example.test/dashboard/projects/project%2Fid"
    )
    assert dashboard_observability_url(
        project="project/id",
        filters={"trace_id": "trace/id"},
    ) == (
        "https://preview.example.test/dashboard/observability?"
        "trace_id=trace%2Fid&project_id=project%2Fid"
    )


def test_auth_whoami_json_reports_unauthenticated(tmp_path: Path) -> None:
    """Machine-readable auth checks should work before login."""
    config_path = tmp_path / "config.yaml"

    result = CliRunner().invoke(app, ["--config", str(config_path), "auth", "whoami", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "authenticated": False,
        "auth_method": None,
        "base_url": "https://api.generalaugment.com",
        "next_action": "genaug auth login",
        "project_ids": [],
        "project_scope": "none",
    }


def test_workspace_and_project_commands_persist_explicit_context(tmp_path: Path) -> None:
    """Workspace and Project selection should be explicit and remain unambiguous."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    FakeHTTPClient.queue = [
        json_response(
            {
                "items": [
                    {
                        "id": "workspace_1",
                        "name": "Acme",
                        "slug": "acme",
                        "kind": "shared",
                        "role": "owner",
                    }
                ]
            }
        ),
        json_response(
            {
                "id": "project_1",
                "workspace_id": "workspace_1",
                "name": "Health App",
                "slug": "health-app",
            }
        ),
        json_response(
            {
                "items": [
                    {
                        "id": "project_1",
                        "workspace_id": "workspace_1",
                        "name": "Health App",
                        "slug": "health-app",
                    }
                ]
            }
        ),
    ]

    select_workspace = runner.invoke(
        app,
        ["--config", str(config_path), "workspace", "use", "acme"],
    )
    create_project = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "project",
            "create",
            "--name",
            "Health App",
            "--slug",
            "health-app",
        ],
    )
    select_project = runner.invoke(
        app,
        ["--config", str(config_path), "project", "use", "health-app"],
    )

    assert select_workspace.exit_code == 0, select_workspace.output
    assert create_project.exit_code == 0, create_project.output
    assert select_project.exit_code == 0, select_project.output
    assert FakeHTTPClient.requests[1]["json"] == {
        "name": "Health App",
        "slug": "health-app",
        "workspace_id": "workspace_1",
    }
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["active_workspace"] == "workspace_1"
    assert persisted["active_project"] == "project_1"


def test_workspace_create_clears_stale_project_context(tmp_path: Path) -> None:
    """Changing Workspace must not retain a Project from another Workspace."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "active_workspace": "old_workspace",
                "active_project": "old_project",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "id": "workspace_2",
                "name": "New Company",
                "slug": "new-company",
                "kind": "shared",
                "role": "owner",
            }
        )
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "workspace",
            "create",
            "--name",
            "New Company",
            "--slug",
            "new-company",
        ],
    )

    assert result.exit_code == 0, result.output
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["active_workspace"] == "workspace_2"
    assert persisted["active_project"] is None


def test_release_commands_use_installer_not_runtime_authority(
    tmp_path: Path,
) -> None:
    """Release control calls must retain installer rather than runtime authority."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "active_workspace": "workspace_1",
                "active_project": "project_1",
                "runtime_api_key": "runtime-secret-must-not-be-used",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "id": "release_1",
                "version": 1,
                "status": "candidate",
                "fingerprint": "sha256-release-one",
            }
        ),
        json_response(
            {
                "id": "deployment_1",
                "runtime_mode": "test",
                "active_release_id": "release_1",
            }
        ),
    ]

    release_result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "release", "create"],
    )
    promote_result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "release",
            "promote",
            "release_1",
            "--mode",
            "test",
        ],
    )

    assert release_result.exit_code == 0, release_result.output
    assert promote_result.exit_code == 0, promote_result.output
    assert all(
        request["headers"] == {"Authorization": "Bearer gainst_access_secret"}
        for request in FakeHTTPClient.requests
    )
    assert FakeHTTPClient.requests[1]["json"] == {
        "runtime_mode": "test",
        "idempotency_key": "cli-promote-project_1-release_1-test",
    }
    assert "runtime-secret-must-not-be-used" not in json.dumps(FakeHTTPClient.requests)


def test_release_live_actions_require_explicit_confirmation(tmp_path: Path) -> None:
    """Live promotion and rollback must fail before making a network request."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "active_project": "project_1",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )

    promote = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "release",
            "promote",
            "release_1",
            "--mode",
            "live",
        ],
    )
    rollback = CliRunner().invoke(
        app,
        ["--config", str(config_path), "release", "rollback", "--mode", "live"],
    )

    assert promote.exit_code != 0
    assert rollback.exit_code != 0
    assert FakeHTTPClient.requests == []


def test_auth_login_browser_flow_stores_installer_session_without_printing_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser login should exchange an installer code and keep tokens out of output."""
    config_path = tmp_path / "config.yaml"
    runner = CliRunner()
    monkeypatch.setattr("platform.node", lambda: "Rune's MacBook Pro\n")
    FakeHTTPClient.queue = [
        json_response(
            {
                "request_id": "req_1",
                "authorize_url": "https://app.generalaugment.com/cli/authorize?request_id=req_1",
                "expires_at": "2026-05-23T19:30:00Z",
                "scopes": ["projects:write", "runtime_keys:create"],
            }
        ),
        json_response(
            {
                "token_type": "Bearer",
                "access_token": "gainst_access_secret",
                "refresh_token": "garefr_refresh_secret",
                "expires_at": "2099-05-23T20:30:00Z",
                "scopes": ["projects:write", "runtime_keys:create"],
                "project_id": "proj/1",
            }
        ),
        json_response(
            {
                "auth_method": "installer",
                "clerk_user_id": "user_123",
                "clerk_email": "dev@example.com",
                "scopes": ["projects:write", "runtime_keys:create"],
                "project_id": "proj/1",
                "project_ids": ["proj/1"],
            }
        ),
    ]

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "auth",
            "login",
            "--base-url",
            "http://api.test",
            "--no-browser",
            "--authorization-code",
            "gacode_once",
            "--code-verifier",
            "verifier",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "gainst_access_secret" not in result.output
    assert "garefr_refresh_secret" not in result.output
    assert "Browser authorization started" in result.output
    assert FakeHTTPClient.requests[0]["url"] == (
        "http://api.test/api/v1/installer/auth/browser/start"
    )
    assert FakeHTTPClient.requests[0]["headers"] == {}
    assert FakeHTTPClient.requests[0]["json"]["client_name"] == "General Augment CLI"
    assert FakeHTTPClient.requests[0]["json"]["device_name"] == "Rune's MacBook Pro"
    assert FakeHTTPClient.requests[0]["json"]["redirect_uri"] == auth_command.MANUAL_REDIRECT_URI
    assert FakeHTTPClient.requests[1]["url"] == "http://api.test/api/v1/installer/auth/token"
    assert FakeHTTPClient.requests[2]["headers"] == {"Authorization": "Bearer gainst_access_secret"}
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["api_key"] is None
    assert config["metadata"]["installer"]["access_token"] == "gainst_access_secret"
    assert config["metadata"]["installer"]["refresh_token"] == "garefr_refresh_secret"


def test_auth_login_browser_flow_accepts_local_callback_without_paste(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Browser login should accept the loopback callback code without terminal paste."""
    config_path = tmp_path / "config.yaml"
    runner = CliRunner()
    callback = _FakeLocalCallback("gacode_callback")
    monkeypatch.setattr(auth_command, "_start_local_callback_server", lambda: callback)
    monkeypatch.setattr(cast(Any, auth_command).webbrowser, "open", lambda _: True)
    FakeHTTPClient.queue = [
        json_response(
            {
                "request_id": "req_1",
                "authorize_url": "https://app.generalaugment.com/cli/authorize?request_id=req_1",
                "expires_at": "2026-05-23T19:30:00Z",
                "scopes": ["projects:write", "runtime_keys:create"],
            }
        ),
        json_response(
            {
                "token_type": "Bearer",
                "access_token": "gainst_access_secret",
                "refresh_token": "garefr_refresh_secret",
                "expires_at": "2099-05-23T20:30:00Z",
                "scopes": ["projects:write", "runtime_keys:create"],
                "project_id": "proj/1",
            }
        ),
        json_response(
            {
                "auth_method": "installer",
                "clerk_user_id": "user_123",
                "clerk_email": "dev@example.com",
                "scopes": ["projects:write", "runtime_keys:create"],
                "project_id": "proj/1",
                "project_ids": ["proj/1"],
            }
        ),
    ]

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "auth",
            "login",
            "--base-url",
            "http://api.test",
            "--code-verifier",
            "verifier",
            "--callback-timeout",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Waiting for browser approval" in result.output
    assert "gainst_access_secret" not in result.output
    assert "garefr_refresh_secret" not in result.output
    assert callback.wait_timeout == 7
    assert callback.closed is True
    assert FakeHTTPClient.requests[0]["json"]["redirect_uri"] == ("http://127.0.0.1:49231/callback")
    assert FakeHTTPClient.requests[1]["json"]["code"] == "gacode_callback"


def test_local_callback_server_captures_browser_authorization_code() -> None:
    """The loopback callback helper should capture the browser authorization code."""
    callback = auth_command._start_local_callback_server(port=0)
    try:
        with urllib.request.urlopen(
            f"{callback.redirect_uri}?code=gacode_local",
            timeout=2,
        ) as response:
            body = response.read().decode("utf-8")

        assert response.status == 200
        assert "Access approved" in body
        assert "close this tab" in body
        assert callback.wait(0.5) == "gacode_local"
    finally:
        callback.close()


def test_local_callback_server_closes_with_idle_browser_connection() -> None:
    """Browser preconnect sockets must not prevent the CLI from saving auth state."""
    callback = auth_command._start_local_callback_server(port=0)
    parsed = urllib.parse.urlparse(callback.redirect_uri)
    idle_socket = socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 0))
    try:
        started = time.monotonic()
        callback.close()
        assert time.monotonic() - started < 1
    finally:
        idle_socket.close()


def test_dashboard_success_return_url_is_strictly_allowlisted() -> None:
    assert auth_command._safe_dashboard_success_url(
        "https://app.generalaugment.com/cli/authorize/success"
    )
    assert auth_command._safe_dashboard_success_url(
        "https://general-augment-pr61.vercel.app/cli/authorize/success"
    )
    assert not auth_command._safe_dashboard_success_url("https://example.com/cli/authorize/success")
    assert not auth_command._safe_dashboard_success_url(
        "https://app.generalaugment.com/cli/authorize/success?next=https://example.com"
    )


def test_setup_bootstrap_persists_runtime_key_into_config_but_not_artifact(
    tmp_path: Path,
) -> None:
    """Bootstrap should persist the minted runtime key into chmod-600 config, not artifacts."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": None,
                "metadata": {
                    "installer": {
                        "access_token": "gainst_access_secret",
                        "refresh_token": "garefr_refresh_secret",
                        "scopes": ["projects:read", "projects:write", "runtime_keys:create"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    artifact_path = tmp_path / "setup-plan.json"
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "proj_1", "name": "Demo App", "slug": "demo-app"}),
        json_response(
            {
                "id": "key_1",
                "name": "Self-serve app backend",
                "api_key": "ga_runtime_secret_once",
                "masked_key": "ga...once",
                "project_id": "proj_1",
                "scopes": ["responses:create"],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "setup",
            "--workspace",
            str(workspace),
            "--bootstrap",
            "--project-name",
            "Demo App",
            "--project-slug",
            "demo-app",
            "--output",
            str(artifact_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ga_runtime_secret_once" not in result.output
    payload = json.loads(result.output)
    assert payload["auth"]["method"] == "installer"
    assert payload["target"]["project_ref"] == "proj_1"
    assert payload["bootstrap"] == {
        "applied": True,
        "project": {"id": "proj_1", "name": "Demo App", "slug": "demo-app"},
        "runtime_key": {
            "id": "key_1",
            "name": "Self-serve app backend",
            "masked_key": "ga...once",
            "project_id": "proj_1",
            "scopes": ["responses:create"],
        },
    }
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/installer/projects"
    assert FakeHTTPClient.requests[0]["headers"] == {"Authorization": "Bearer gainst_access_secret"}
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["json"]["slug"] == "demo-app"
    assert FakeHTTPClient.requests[2]["url"] == (
        "http://api.test/api/v1/installer/projects/proj_1/runtime-keys"
    )
    assert FakeHTTPClient.requests[2]["json"]["runtime_mode"] == "test"
    persisted_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted_config["active_project"] == "proj_1"
    # The runtime key is persisted into config so a brand-new user gets a working
    # auth login -> setup --bootstrap -> smoke chain with no manual export.
    assert persisted_config["api_key"] is None
    assert persisted_config["runtime_api_key"] == "ga_runtime_secret_once"
    assert persisted_config["runtime_key_mode"] == "test"
    assert config_path.stat().st_mode & 0o777 == 0o600
    # The redacted setup artifact must never contain the raw runtime secret.
    assert "ga_runtime_secret_once" not in artifact_path.read_text(encoding="utf-8")


def test_setup_bootstrap_can_print_runtime_env_once_without_artifact_secret(
    tmp_path: Path,
) -> None:
    """Explicit env output should show the runtime key once without persisting it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": None,
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "proj_1", "name": "Demo App", "slug": "demo-app"}),
        json_response(
            {
                "id": "key_1",
                "name": "Self-serve app backend",
                "api_key": "ga_runtime_secret_once",
                "masked_key": "ga...once",
                "project_id": "proj_1",
                "scopes": ["responses:create"],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "setup",
            "--workspace",
            str(workspace),
            "--bootstrap",
            "--project-name",
            "Demo App",
            "--project-slug",
            "demo-app",
            "--print-env",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "GENAUG_API_KEY=ga_runtime_secret_once" in result.output
    artifact = workspace / ".genaug" / "setup-plan.json"
    assert artifact.exists()
    assert "ga_runtime_secret_once" not in artifact.read_text(encoding="utf-8")
    # --print-env still works, and the key is also persisted into chmod-600 config
    # so the next command authenticates automatically.
    persisted_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted_config["api_key"] is None
    assert persisted_config["runtime_api_key"] == "ga_runtime_secret_once"


def test_setup_bootstrap_can_run_browser_login_inline_without_leaking_runtime_key(
    tmp_path: Path,
) -> None:
    """Setup bootstrap should optionally run browser auth before tenant bootstrap."""
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    artifact_path = tmp_path / "setup-plan.json"
    FakeHTTPClient.queue = [
        json_response(
            {
                "request_id": "req_1",
                "authorize_url": "https://app.generalaugment.com/cli/authorize?request_id=req_1",
                "expires_at": "2026-05-23T19:30:00Z",
                "scopes": ["projects:write", "runtime_keys:create"],
            }
        ),
        json_response(
            {
                "token_type": "Bearer",
                "access_token": "gainst_access_secret",
                "refresh_token": "garefr_refresh_secret",
                "expires_at": "2099-05-23T20:30:00Z",
                "scopes": ["projects:write", "runtime_keys:create"],
                "project_id": None,
            }
        ),
        json_response(
            {
                "auth_method": "installer",
                "clerk_user_id": "user_123",
                "clerk_email": "dev@example.com",
                "project_ids": [],
            }
        ),
        json_response({"items": []}),
        json_response({"id": "proj_1", "name": "Demo App", "slug": "demo-app"}),
        json_response(
            {
                "id": "key_1",
                "name": "Self-serve app backend",
                "api_key": "ga_runtime_secret_once",
                "masked_key": "ga...once",
                "project_id": "proj_1",
                "scopes": ["responses:create"],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "--base-url",
            "http://api.test",
            "setup",
            "--workspace",
            str(workspace),
            "--login",
            "--no-browser",
            "--authorization-code",
            "gacode_once",
            "--code-verifier",
            "verifier",
            "--bootstrap",
            "--project-name",
            "Demo App",
            "--project-slug",
            "demo-app",
            "--output",
            str(artifact_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Browser authorization started" in result.output
    assert "Setup plan written without changing app code or storing secrets." in result.output
    assert "ga_runtime_secret_once" not in result.output
    assert "gainst_access_secret" not in result.output
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["bootstrap"]["project"]["id"] == "proj_1"
    assert payload["bootstrap"]["runtime_key"]["masked_key"] == "ga...once"
    assert "ga_runtime_secret_once" not in artifact_path.read_text(encoding="utf-8")
    assert FakeHTTPClient.requests[0]["url"] == (
        "http://api.test/api/v1/installer/auth/browser/start"
    )
    assert FakeHTTPClient.requests[3]["url"] == "http://api.test/api/v1/installer/projects"
    assert FakeHTTPClient.requests[3]["headers"] == {"Authorization": "Bearer gainst_access_secret"}
    persisted_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted_config["active_project"] == "proj_1"
    # The minted runtime key is persisted into chmod-600 config so the next command
    # authenticates without a manual export; the raw key still never hits output.
    assert persisted_config["api_key"] is None
    assert persisted_config["runtime_api_key"] == "ga_runtime_secret_once"


def test_setup_guided_can_configure_provider_health_from_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guided setup should optionally store provider keys and run health checks."""
    config_path = write_config(tmp_path)
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "project_name": "Demo App",
                "project_slug": "demo-app",
                "capabilities": ["browse"],
                "provider_env_vars": {"browserbase": "BROWSERBASE_API_KEY"},
                "job_type": "website-builder",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb_secret_raw")
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "proj_1", "slug": "demo-agent", "name": "Demo Agent"}]}),
        json_response(
            {
                "provider": "browserbase",
                "status": "active",
                "credential_kind": "external_mcp_provider",
                "base_url_configured": False,
                "updated_at": "2026-05-24T10:00:00Z",
            }
        ),
        json_response(
            {
                "provider": "browserbase",
                "status": "available",
                "message": "Browserbase credential is configured.",
                "checked_at": "2026-05-24T10:00:01Z",
                "latency_ms": 14,
                "last_validated_at": "2026-05-24T10:00:01Z",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "setup",
            "--workspace",
            str(workspace),
            "--project",
            "demo-agent",
            "--guided",
            "--answers-file",
            str(answers_path),
            "--configure-providers",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "bb_secret_raw" not in result.output
    payload = json.loads(result.output)
    provider_setup = payload["guided"]["provider_setup"]
    assert provider_setup["status"] == "passed"
    assert provider_setup["security"] == {
        "credential_custody": "general_augment",
        "raw_secrets_in_output": False,
        "raw_provider_payloads_in_output": False,
    }
    assert provider_setup["providers"] == [
        {
            "provider": "browserbase",
            "capability": "browse",
            "credential_kind": "external_mcp_provider",
            "env_var": "BROWSERBASE_API_KEY",
            "status": "passed",
            "checks": [
                {"name": "credential_custody", "status": "passed"},
                {"name": "provider_health", "status": "passed"},
            ],
            "evidence": {
                "credential": {
                    "base_url_configured": False,
                    "credential_kind": "external_mcp_provider",
                    "provider": "browserbase",
                    "status": "active",
                    "updated_at": "2026-05-24T10:00:00Z",
                },
                "provider_health": {
                    "checked_at": "2026-05-24T10:00:01Z",
                    "last_validated_at": "2026-05-24T10:00:01Z",
                    "latency_ms": 14,
                    "message": "Browserbase credential is configured.",
                    "provider": "browserbase",
                    "status": "available",
                },
            },
            "blockers": [],
        }
    ]
    assert "bb_secret_raw" not in (workspace / ".genaug" / "setup-plan.json").read_text(
        encoding="utf-8"
    )
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/admin/projects"
    assert FakeHTTPClient.requests[1]["method"] == "PUT"
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/admin/projects/proj_1/capability-providers/browserbase"
    )
    assert FakeHTTPClient.requests[1]["json"]["api_key"] == "bb_secret_raw"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/admin/projects/proj_1/capability-providers/browserbase/health-check"
    )


def test_setup_guided_provider_setup_blocks_when_env_var_missing(tmp_path: Path) -> None:
    """Guided provider setup should report missing env vars without custody calls."""
    config_path = write_config(tmp_path)
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "capabilities": ["browse"],
                "provider_env_vars": {"browserbase": "BROWSERBASE_API_KEY"},
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "setup",
            "--workspace",
            str(workspace),
            "--project",
            "demo-agent",
            "--guided",
            "--answers-file",
            str(answers_path),
            "--configure-providers",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    provider_setup = payload["guided"]["provider_setup"]
    assert provider_setup["status"] == "blocked"
    assert provider_setup["providers"][0]["checks"] == [{"name": "env_var", "status": "blocked"}]
    assert provider_setup["providers"][0]["blockers"] == [
        "Environment variable BROWSERBASE_API_KEY is not set."
    ]
    assert FakeHTTPClient.requests == []


def test_init_existing_app_passes_through_guided_provider_setup(tmp_path: Path) -> None:
    """Bare init should expose the same guided provider execution as setup."""
    config_path = write_config(tmp_path)
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "capabilities": ["browse"],
                "provider_env_vars": {"browserbase": "BROWSERBASE_API_KEY"},
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "init",
            "--workspace",
            str(workspace),
            "--project",
            "demo-agent",
            "--guided",
            "--answers-file",
            str(answers_path),
            "--configure-providers",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["guided"]["provider_setup"]["status"] == "blocked"
    assert payload["guided"]["provider_setup"]["providers"][0]["provider"] == "browserbase"


def test_init_existing_app_can_pass_through_inline_login_bootstrap(tmp_path: Path) -> None:
    """genaug init should be able to run the existing-app auth/bootstrap flow."""
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    artifact_path = tmp_path / "setup-plan.json"
    FakeHTTPClient.queue = [
        json_response(
            {
                "request_id": "req_1",
                "authorize_url": "https://app.generalaugment.com/cli/authorize?request_id=req_1",
            }
        ),
        json_response(
            {
                "access_token": "gainst_access_secret",
                "refresh_token": "garefr_refresh_secret",
                "scopes": ["projects:write", "runtime_keys:create"],
            }
        ),
        json_response({"auth_method": "installer", "project_ids": []}),
        json_response({"items": []}),
        json_response({"id": "proj_1", "name": "Demo App", "slug": "demo-app"}),
        json_response(
            {
                "id": "key_1",
                "name": "Self-serve app backend",
                "api_key": "ga_runtime_secret_once",
                "masked_key": "ga...once",
                "project_id": "proj_1",
                "scopes": ["responses:create"],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "--base-url",
            "http://api.test",
            "init",
            "--workspace",
            str(workspace),
            "--login",
            "--no-browser",
            "--authorization-code",
            "gacode_once",
            "--code-verifier",
            "verifier",
            "--bootstrap",
            "--project-name",
            "Demo App",
            "--project-slug",
            "demo-app",
            "--output",
            str(artifact_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Browser authorization started" in result.output
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["bootstrap"]["project"]["id"] == "proj_1"
    assert "ga_runtime_secret_once" not in result.output
    assert "ga_runtime_secret_once" not in artifact_path.read_text(encoding="utf-8")


def test_providers_setup_writes_installer_custody_and_health_checks(
    tmp_path: Path,
) -> None:
    """Provider setup should store BYO keys in GA custody without echoing secrets."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": None,
                "active_project": "proj_1",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "proj_1", "name": "Demo App", "slug": "demo-app"}]}),
        json_response(
            {
                "provider": "browserbase",
                "label": "Browserbase provider",
                "credential_kind": "external_mcp_provider",
                "status": "active",
                "base_url_configured": True,
                "created_at": "2026-05-23T19:30:00Z",
                "updated_at": "2026-05-23T19:30:00Z",
                "last_validated_at": None,
            }
        ),
        json_response(
            {
                "provider": "browserbase",
                "credential_kind": "external_mcp_provider",
                "status": "available",
                "message": "Browserbase credential is configured.",
                "checked_at": "2026-05-23T19:31:00Z",
                "latency_ms": 12,
                "status_code": 200,
                "retryable": False,
                "last_validated_at": "2026-05-23T19:31:00Z",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "setup",
            "--capability",
            "browse",
            "--project",
            "demo-app",
            "--api-key",
            "bb_secret_raw",
            "--health-check",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "bb_secret_raw" not in result.output
    payload = json.loads(result.output)
    assert payload["providers"][0]["provider"] == "browserbase"
    assert payload["providers"][0]["credential"]["status"] == "active"
    assert payload["providers"][0]["health"]["status"] == "available"
    # The installer slug is resolved to its UUID before the typed installer route.
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/installer/projects"
    assert FakeHTTPClient.requests[1]["method"] == "PUT"
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/installer/projects/proj_1/capability-providers/browserbase"
    )
    assert FakeHTTPClient.requests[1]["headers"] == {"Authorization": "Bearer gainst_access_secret"}
    assert FakeHTTPClient.requests[1]["json"]["api_key"] == "bb_secret_raw"
    assert FakeHTTPClient.requests[2]["method"] == "POST"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/installer/projects/proj_1/capability-providers/browserbase/health-check"
    )


def test_providers_setup_routes_model_provider_health_through_admin_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X/FAL/Veo provider setup should use model-provider custody and health APIs."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": "gaadm_secret",
                "active_project": "demo-agent",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XAI_API_KEY", "xai_secret_raw")
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "proj_1", "slug": "demo-agent", "name": "Demo Agent"}]}),
        json_response(
            {
                "provider": "xai",
                "status": "active",
                "api_mode": "codex_responses",
                "base_url_configured": False,
                "model_prefixes": ["xai/", "grok-"],
                "last_validated_at": None,
            }
        ),
        json_response(
            {
                "provider": "xai",
                "status": "available",
                "message": "xAI credential is configured.",
                "checked_at": "2026-05-23T19:31:00Z",
                "latency_ms": 12,
                "status_code": 200,
                "retryable": False,
                "last_validated_at": "2026-05-23T19:31:00Z",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "setup",
            "--provider",
            "xai",
            "--project",
            "demo-agent",
            "--api-key-env",
            "XAI_API_KEY",
            "--health-check",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "xai_secret_raw" not in result.output
    payload = json.loads(result.output)
    assert payload["providers"][0]["credential_kind"] == "model_provider"
    assert payload["providers"][0]["credential"]["status"] == "active"
    assert payload["providers"][0]["health"]["status"] == "available"
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/admin/projects"
    assert FakeHTTPClient.requests[1]["method"] == "PUT"
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/admin/projects/proj_1/model-providers/xai"
    )
    assert FakeHTTPClient.requests[1]["json"] == {
        "api_key": "xai_secret_raw",
        "api_mode": "codex_responses",
        "model_prefixes": ["xai/", "grok-"],
    }
    assert FakeHTTPClient.requests[2]["method"] == "POST"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/admin/projects/proj_1/model-providers/xai/health-check"
    )


def test_providers_readiness_lists_productized_and_planned_workflows(tmp_path: Path) -> None:
    """Provider readiness should expose current and planned delegated workflows."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": "gaadm_secret",
                "active_project": "demo-agent",
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "proj_1", "slug": "demo-agent", "name": "Demo Agent"}]}),
        json_response(
            {
                "items": [
                    {
                        "provider": "anthropic-managed-agents",
                        "label": "Anthropic Managed Agents provider",
                        "credential_kind": "managed_agent_provider",
                        "capabilities": ["anthropic_managed_agent"],
                        "delegated_workflows": ["coding"],
                        "planned_workflows": ["research"],
                        "configured": True,
                        "status": "active",
                        "health_status": "available",
                        "readiness": "ready",
                        "setup_hint": "Provider is configured.",
                    },
                    {
                        "provider": "browserbase",
                        "label": "Browserbase provider",
                        "credential_kind": "external_mcp_provider",
                        "capabilities": ["browser"],
                        "delegated_workflows": ["browser", "browser_action"],
                        "planned_workflows": [],
                        "configured": False,
                        "status": "missing",
                        "health_status": "missing",
                        "readiness": "setup_required",
                        "readiness_details": {
                            "browser_artifact_storage_backend": "filesystem",
                            "hosted_screenshot_storage": "local_only",
                            "blockers": [
                                (
                                    "Use GCS artifact storage before treating screenshots as "
                                    "durable evidence."
                                )
                            ],
                        },
                        "setup_hint": "Run genaug providers setup.",
                    },
                ]
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "readiness",
            "--project",
            "demo-agent",
        ],
        terminal_width=220,
    )

    assert result.exit_code == 0, result.output
    assert "coding" in result.output
    assert "research" in result.output
    assert "browser" in result.output
    assert "hosted screenshots" in result.output
    assert "local only (filesystem)" in result.output
    assert "Use GCS artifact storage" in result.output
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/admin/projects"
    assert FakeHTTPClient.requests[0]["params"] == {"limit": 1000, "offset": 0}
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/admin/projects/proj_1/coding-providers"
    )


def test_skills_design_can_push_starter_bundle_with_installer_auth(tmp_path: Path) -> None:
    """Skill design should optionally push SKILL.md and prompt flow drafts."""
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": None,
                "active_project": "proj_1",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response({"items": [{"id": "proj_1", "name": "Demo App", "slug": "demo-app"}]}),
        json_response(
            {
                "name": "Website Builder",
                "content": "# Website Builder",
                "metadata": {"description": "Build, review, and prepare website previews."},
            }
        ),
        json_response(
            {
                "id": "flow_row_1",
                "project_id": "proj_1",
                "flow_id": "website_builder",
                "version_id": "website-builder:v1",
                "name": "Website Builder",
                "status": "draft",
                "graph": {"id": "website_builder", "nodes": [{"id": "intake"}]},
                "compiled_snapshot": {},
                "created_at": "2026-05-23T19:40:00Z",
                "updated_at": "2026-05-23T19:40:00Z",
                "published_at": None,
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "skills",
            "design",
            "--workspace",
            str(workspace),
            "--project",
            "demo-app",
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"]["skill"]["name"] == "Website Builder"
    assert payload["applied"]["prompt_flow"]["flow_id"] == "website_builder"
    # The installer slug is resolved to its UUID before the typed installer routes.
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/installer/projects"
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/installer/projects/proj_1/skills"
    )
    assert "Build safe website previews" in FakeHTTPClient.requests[1]["json"]["content"]
    assert FakeHTTPClient.requests[1]["headers"] == {"Authorization": "Bearer gainst_access_secret"}
    assert FakeHTTPClient.requests[2]["method"] == "PUT"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/installer/projects/proj_1/prompt-flows/website_builder"
    )


def test_connectors_setup_can_push_mcp_server_with_installer_auth(tmp_path: Path) -> None:
    """Connector setup should add MCP servers through installer-scoped setup auth."""
    config_path = tmp_path / "config.yaml"
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": None,
                "active_project": "proj_1",
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response(
            {
                "name": "browserbase",
                "url": PLACEHOLDER_MCP_URL,
                "tools": {"include": ["browser_navigate"]},
                "enabled": True,
            }
        ),
        json_response({"name": "browserbase", "ok": True, "transport": "http", "detail": None}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "connectors",
            "setup",
            "--workspace",
            str(workspace),
            "--project",
            "proj_1",
            "--name",
            "browserbase",
            "--url",
            PLACEHOLDER_MCP_URL,
            "--include-tool",
            "browser_navigate",
            "--health-check",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"]["server"]["name"] == "browserbase"
    assert payload["applied"]["health"]["ok"] is True
    assert FakeHTTPClient.requests[0]["method"] == "POST"
    assert FakeHTTPClient.requests[0]["url"] == (
        "http://api.test/api/v1/installer/projects/proj_1/mcp-servers"
    )
    assert FakeHTTPClient.requests[0]["headers"] == {"Authorization": "Bearer gainst_access_secret"}
    assert FakeHTTPClient.requests[0]["json"]["tools"] == {"include": ["browser_navigate"]}
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/installer/projects/proj_1/mcp-servers/browserbase/test"
    )


def test_connectors_setup_rejects_raw_url_query_secrets(tmp_path: Path) -> None:
    """Connector setup must not store raw provider keys in MCP URLs."""
    config_path = write_config(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "connectors",
            "setup",
            "--name",
            "browserbase",
            "--url",
            "https://mcp.browserbase.com/mcp?api_key=bb_secret_raw",
        ],
    )

    assert result.exit_code != 0
    assert "credential placeholder" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_auth_login_fails_without_saving_invalid_key(tmp_path: Path) -> None:
    """Login should prove the key works before writing local credentials."""
    config_path = tmp_path / "config.yaml"
    FakeHTTPClient.queue = [json_response({"detail": "Invalid admin API key."}, status_code=401)]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "auth",
            "login",
            "--api-key",
            "bad",
            "--base-url",
            "http://api.test",
        ],
    )

    assert result.exit_code != 0
    assert "Run genaug auth login" in result.output
    assert not config_path.exists()
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/admin/me"


def test_authenticated_success_malformed_json_fails_cleanly(tmp_path: Path) -> None:
    """Authenticated CLI calls should not leak raw JSON decoder errors."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "api_key: secret\nbase_url: http://api.test\n",
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        httpx.Response(200, text="not-json", headers={"content-type": "application/json"})
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "auth", "whoami"])

    assert result.exit_code != 0
    assert "malformed JSON for an authenticated request" in result.output
    assert "JSONDecodeError" not in result.output


def test_console_scripts_include_public_command_only() -> None:
    """Package metadata should expose only the genaug command."""
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts["genaug"] == "platform_cli.main:run"
    assert "general_augment" not in scripts


def test_console_entrypoint_returns_nonzero_for_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed console command must not turn a failed operation into success."""
    main_module = import_module("platform_cli.main")

    def fail() -> None:
        raise CLIError("synthetic failure")

    monkeypatch.setattr(main_module, "app", fail)

    with pytest.raises(typer.Exit) as exc_info:
        main_module.run()

    assert exc_info.value.exit_code == 1


def test_version_flag_exposes_cli_package_version() -> None:
    """Automation should be able to check the installed CLI version."""
    from platform_cli import __version__

    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    # Asserted against the single-sourced package metadata (pyproject.toml) rather
    # than a hardcoded string, so this never needs touching on a version bump.
    assert f"genaug {__version__}" in result.output
    assert __version__ != "0.0.0+local"  # editable install must resolve real metadata


def test_local_mock_covers_app_facing_health_alias() -> None:
    """The local mock should expose the same app health alias as the hosted API."""
    import platform_cli.local_mock as local_mock

    assert "/v1/health" in local_mock.HEALTH_PATHS
    assert "/health/ready" in local_mock.HEALTH_PATHS


def test_local_mock_decodes_memory_route_segments() -> None:
    """SDK-encoded app user and memory IDs should match stored mock memory."""
    import platform_cli.local_mock as local_mock

    store = local_mock.LocalGAMockStore()
    _, stored = store.store_memory({"user_id": "app/user", "fact": "Likes tea"})

    user_id = local_mock._path_suffix(
        "/api/v1/agent/memory/profile/app%2Fuser",
        "/api/v1/agent/memory/profile/",
    )
    _, profile = store.memory_profile(user_id)
    memory_id = local_mock._path_suffix(
        f"/api/v1/agent/memory/{stored['memory_id']}",
        "/api/v1/agent/memory/",
    )
    _, corrected = store.correct_memory(
        memory_id,
        {"user_id": "app/user", "fact": "Likes green tea", "source": "test"},
    )
    _, lineage = store.memory_lineage(memory_id, user_id)
    _, deleted = store.delete_memory(memory_id, user_id)

    assert profile["total_facts"] == 1
    assert corrected["corrected_memory_id"]
    assert lineage["related_count"] == 2
    assert deleted["deleted_count"] == 1


def test_local_mock_extracts_nested_generated_tool_includes() -> None:
    """Generated manifests should enable tools from the current MCP shape."""
    import platform_cli.local_mock as local_mock

    manifest = {
        "tools": {
            "mcp": [
                {
                    "name": "customer-success-api",
                    "tools": {"include": ["listaccounts", "createaccountriskinsight"]},
                }
            ]
        }
    }

    assert local_mock._manifest_tool_ids(manifest) == [
        "createaccountriskinsight",
        "listaccounts",
    ]


def test_local_mock_reports_project_channel_status() -> None:
    """Project verify should have a hosted-compatible channel status route locally."""
    import platform_cli.local_mock as local_mock

    store = local_mock.LocalGAMockStore()
    _, project = store.deploy_project(
        {
            "yaml_content": yaml.safe_dump(
                {
                    "metadata": {"name": "dayplan", "display_name": "DayPlan"},
                    "channels": {"sms": {}, "telegram": {}},
                }
            )
        }
    )

    status, payload = store.project_channel_status(project["id"])

    assert status == 200
    assert payload["project_id"] == project["id"]
    assert {row["channel"] for row in payload["channels"]} == {
        "in_app",
        "sms",
        "telegram",
        "whatsapp",
    }
    assert all("provider_status" in row for row in payload["channels"])


def test_local_mock_persists_project_skills() -> None:
    """Local mock should support CLI skill CRUD for contract tests."""
    import platform_cli.local_mock as local_mock

    store = local_mock.LocalGAMockStore()
    _, project = store.deploy_project(
        {
            "yaml_content": yaml.safe_dump({"metadata": {"name": "dayplan"}}),
            "skills": [skill_markdown("Support Triage")],
        }
    )

    status, listed = store.project_skills(project["id"])
    get_status, skill = store.get_project_skill(project["id"], "Support Triage")
    add_status, added = store.add_project_skill(
        project["id"], {"content": skill_markdown("Renewals")}
    )
    delete_status, deleted = store.delete_project_skill(project["id"], "Support Triage")

    assert status == 200
    assert listed["items"][0]["name"] == "Support Triage"
    assert get_status == 200
    assert "Route support work." in skill["content"]
    assert add_status == 200
    assert added["name"] == "Renewals"
    assert delete_status == 200
    assert deleted == {"status": "deleted", "name": "Support Triage"}


def test_local_mock_returns_project_soul_content() -> None:
    """Local verify should be able to check deployed SOUL.md content."""
    import platform_cli.local_mock as local_mock

    store = local_mock.LocalGAMockStore()
    _, project = store.deploy_project(
        {
            "yaml_content": yaml.safe_dump({"metadata": {"name": "dayplan"}}),
            "soul_content": "# DayPlan\n\nUse concise onboarding notes.",
        }
    )

    status, payload = store.project_soul(project["id"])

    assert status == 200
    assert payload == {
        "project_id": project["id"],
        "content": "# DayPlan\n\nUse concise onboarding notes.",
    }


def test_local_mock_identity_tracks_project_key() -> None:
    """Local verification should distinguish admin keys from project-scoped keys."""
    import platform_cli.local_mock as local_mock

    store = local_mock.LocalGAMockStore()
    _, project = store.deploy_project(
        {"yaml_content": yaml.safe_dump({"metadata": {"name": "dayplan"}})}
    )
    _, key = store.create_api_key(
        {
            "name": "Backend key",
            "project_id": project["id"],
            "scopes": ["responses:create"],
        }
    )

    _, identity = store.me({"X-Admin-Key": key["api_key"]})
    _, projects = store.list_projects({"X-Admin-Key": key["api_key"]})
    _, keys = store.list_api_keys({"X-Admin-Key": key["api_key"]})

    assert identity["project_id"] == project["id"]
    assert [item["id"] for item in projects["items"]] == [project["id"]]
    assert keys["items"][0]["project_id"] == project["id"]
    assert "api_key" not in keys["items"][0]


def test_keys_create_list_update_and_revoke(tmp_path: Path) -> None:
    """Keys commands should manage project-scoped API keys."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "id": "key/1",
                "name": "Production backend",
                "api_key": "gabtest_secret",
                "masked_key": "gabtest_se...cret",
                "project_id": "proj/1",
                "scopes": ["responses:create"],
                "runtime_mode": "test",
            }
        ),
        json_response(
            {
                "items": [
                    {
                        "id": "key/1",
                        "name": "Production backend",
                        "masked_key": "gabtest_se...cret",
                        "project_id": "proj/1",
                        "scopes": ["responses:create"],
                        "runtime_mode": "test",
                        "expires_at": None,
                    }
                ]
            }
        ),
        json_response({"id": "key/1", "name": "Staging", "masked_key": "ga...cret"}),
        json_response({"status": "revoked", "id": "key/1"}),
    ]
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "keys",
            "create",
            "--name",
            "Production backend",
            "--project",
            "dayplan",
        ],
    )
    listed = runner.invoke(app, ["--config", str(config_path), "keys", "list"])
    updated = runner.invoke(
        app,
        ["--config", str(config_path), "keys", "update", "key/1", "--name", "Staging"],
    )
    revoked = runner.invoke(app, ["--config", str(config_path), "keys", "revoke", "key/1"])

    assert created.exit_code == 0
    assert "gabtest_secret" in created.output
    assert listed.exit_code == 0
    assert "Production" in listed.output
    assert "test" in listed.output
    assert updated.exit_code == 0
    assert revoked.exit_code == 0
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/keys")
    assert FakeHTTPClient.requests[1]["json"]["project_id"] == "proj/1"
    assert FakeHTTPClient.requests[1]["json"]["scopes"] == ["responses:create"]
    assert FakeHTTPClient.requests[1]["json"]["runtime_mode"] == "test"
    assert FakeHTTPClient.requests[3]["url"].endswith("/api/v1/admin/keys/key%2F1")
    assert FakeHTTPClient.requests[4]["method"] == "DELETE"


def test_integrate_generates_openapi_scaffold(tmp_path: Path) -> None:
    """Integrate should generate config, SOUL.md, and tool definitions."""
    spec_path = ROOT / "tests/fixtures/sample_openapi_specs/health_app_api.yaml"
    output_dir = tmp_path / "mysti-agent"

    result = CliRunner().invoke(
        app,
        ["integrate", str(spec_path), "--name", "mysti", "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0
    assert "Generated" in result.output
    assert (output_dir / "genaug-agent.yaml").exists()
    assert (output_dir / "SOUL.md").exists()
    assert (output_dir / "CODING_AGENT_PROMPT.md").exists()
    assert (output_dir / ".env.example").exists()
    assert list((output_dir / "tools").glob("*.yaml"))
    assert "GENAUG_ADMIN_API_KEY=" in (output_dir / ".env.example").read_text(encoding="utf-8")
    prompt = (output_dir / "CODING_AGENT_PROMPT.md").read_text(encoding="utf-8")
    assert "genaug auth login" in prompt
    assert "uv run --project packages/cli genaug --version" in prompt
    assert "managed agent backend" in prompt
    assert "Handle 402 as a budget/setup blocker" in prompt
    assert "genaug verify --project mysti --json" in prompt
    assert "Do not:" in prompt
    agent_config = yaml.safe_load((output_dir / "genaug-agent.yaml").read_text(encoding="utf-8"))
    assert agent_config["apiVersion"] == "genaug/v1"
    assert agent_config["model"]["balanced"] == "google/gemini-2.5-flash"


def test_init_generates_starter_agent_scaffold(tmp_path: Path) -> None:
    """Init should generate a deployable starter config without an OpenAPI spec."""
    output_dir = tmp_path / "spark-agent"

    result = CliRunner().invoke(
        app,
        [
            "init",
            "spark",
            "--display-name",
            "Spark",
            "--description",
            "Calendar intelligence for busy teams.",
            "--tool",
            "web_search",
            "--tool",
            "web_search",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert "Generated starter agent" in result.output
    assert (output_dir / "genaug-agent.yaml").exists()
    assert (output_dir / "SOUL.md").exists()
    assert (output_dir / "skills/README.md").exists()
    assert (output_dir / "tools/README.md").exists()
    assert (output_dir / "CODING_AGENT_PROMPT.md").exists()
    handoff = (output_dir / "CODING_AGENT_PROMPT.md").read_text(encoding="utf-8")
    assert "Start with one backend `/v1/responses` route" in handoff
    assert "approved OpenAPI or MCP registration" in handoff
    agent_config = yaml.safe_load((output_dir / "genaug-agent.yaml").read_text(encoding="utf-8"))
    assert agent_config["apiVersion"] == "genaug/v1"
    assert agent_config["metadata"]["name"] == "spark"
    assert agent_config["metadata"]["display_name"] == "Spark"
    assert agent_config["tools"]["builtin"] == ["web_search"]
    assert agent_config["tools"]["mcp"] == []
    assert agent_config["behavior"]["tool_discovery"]["mode"] == "auto"


def test_init_refuses_to_overwrite_existing_scaffold(tmp_path: Path) -> None:
    """Init should avoid clobbering local starter files unless --force is used."""
    output_dir = tmp_path / "spark-agent"
    output_dir.mkdir()
    (output_dir / "genaug-agent.yaml").write_text("existing: true\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["init", "spark", "--output-dir", str(output_dir)],
    )

    assert result.exit_code != 0
    assert "Refusing to overwrite existing files" in result.output


def test_validate_agent_config_reports_local_manifest_summary(tmp_path: Path) -> None:
    """Validate should inspect a local manifest without calling the hosted API."""
    agent_config = write_agent_config(tmp_path)

    result = CliRunner().invoke(app, ["validate", str(agent_config)])

    assert result.exit_code == 0
    assert "Agent config validation passed" in result.output
    assert "dayplan" in result.output
    assert FakeHTTPClient.requests == []


def test_validate_agent_config_json_reports_errors(tmp_path: Path) -> None:
    """Validate --json should return machine-readable errors for automation."""
    agent_config = write_agent_config(tmp_path)
    payload = yaml.safe_load(agent_config.read_text(encoding="utf-8"))
    payload["model"]["simple"] = "not a model"
    payload["tools"]["mcp"] = [
        {
            "name": "github",
            "url": "https://mcp.github.example.com/mcp",
            "headers": {"Authorization": "Bearer raw-token"},
        }
    ]
    agent_config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    result = CliRunner().invoke(app, ["validate", str(agent_config), "--json"])

    assert result.exit_code != 0
    payload = first_json_object(result.output)
    assert payload["status"] == "FAIL"
    assert "Invalid model for simple: not a model" in payload["errors"]
    assert any("raw secret" in error for error in payload["errors"])
    assert FakeHTTPClient.requests == []


def test_validate_agent_config_rejects_empty_manifest(tmp_path: Path) -> None:
    """Validate should not treat an empty YAML object as a usable manifest."""
    agent_config = tmp_path / "genaug-agent.yaml"
    agent_config.write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["validate", str(agent_config), "--json"])

    assert result.exit_code != 0
    payload = first_json_object(result.output)
    assert payload["status"] == "FAIL"
    errors = " ".join(payload["errors"])
    assert "Manifest must use apiVersion genaug/v1" in errors
    assert "apiVersion genaug/v2" in errors


def test_integrate_auto_deploy_registers_openapi_tools(tmp_path: Path) -> None:
    """Auto deploy should create the project and register generated OpenAPI tools."""
    config_path = write_config(tmp_path)
    spec_path = ROOT / "tests/fixtures/sample_openapi_specs/health_app_api.yaml"
    output_dir = tmp_path / "mysti-agent"
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "proj/1", "name": "Mysti", "slug": "mysti"}),
        json_response(
            {
                "generated_count": 3,
                "curated_count": 2,
                "enabled_tool_ids": ["health_check"],
                "auto_deployed": True,
                "mcp_server": {"name": "mysti-api"},
                "tools": [],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "integrate",
            str(spec_path),
            "--name",
            "mysti",
            "--output-dir",
            str(output_dir),
            "--auto-deploy",
            "--target-count",
            "7",
        ],
    )

    assert result.exit_code == 0
    assert "Project created" in result.output
    assert "Registered OpenAPI tools" in result.output
    assert FakeHTTPClient.requests[2]["method"] == "POST"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/tools/from-openapi"
    )
    body = FakeHTTPClient.requests[2]["json"]
    assert "openapi:" in body["spec_url"]
    assert body["target_count"] == 7
    assert body["auto_deploy"] is True


def test_integrate_json_emits_tool_summary(tmp_path: Path) -> None:
    """integrate --json should emit a machine-readable scaffold summary."""
    spec_path = ROOT / "tests/fixtures/sample_openapi_specs/health_app_api.yaml"
    output_dir = tmp_path / "mysti-agent"

    result = CliRunner().invoke(
        app,
        [
            "integrate",
            str(spec_path),
            "--name",
            "mysti",
            "--output-dir",
            str(output_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["deployed"] is False
    assert isinstance(payload["tools"], list)
    assert payload["tools"]
    assert {"tool_id", "http_method", "risk_level", "enabled"} <= set(payload["tools"][0])


def test_keys_create_json_includes_one_time_secret(tmp_path: Path) -> None:
    """keys create --json must emit the one-time secret so an agent can capture it."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "id": "key/1",
                "name": "Production backend",
                "api_key": "gabtest_secret",
                "masked_key": "gabtest_se...cret",
                "project_id": "proj/1",
                "scopes": ["responses:create"],
                "runtime_mode": "test",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "keys",
            "create",
            "--name",
            "Production backend",
            "--project",
            "dayplan",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["api_key"] == "gabtest_secret"
    assert payload["id"] == "key/1"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ["--scope", "responses:create", "--scope", "admin"],
            "Runtime and management scopes cannot be combined",
        ),
        (["--scope", "responses:create"], "Runtime-scoped keys require --project"),
        (["--scope", "admin", "--runtime-mode", "live"], "runtime-scoped keys"),
    ],
)
def test_keys_create_rejects_ambiguous_credential_roles(
    tmp_path: Path,
    arguments: list[str],
    message: str,
) -> None:
    """The CLI must reject keys that blur management and runtime authority."""

    config_path = write_config(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "keys",
            "create",
            "--name",
            "Unsafe key",
            *arguments,
        ],
    )

    assert result.exit_code == 2
    assert message in plain_cli_output(result)


def test_keys_create_supports_explicit_project_management_key(tmp_path: Path) -> None:
    """Operators may explicitly mint a project management key without runtime mode."""

    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "id": "key/1",
                "name": "Project operator",
                "api_key": "gaadmlive_secret",
                "masked_key": "gaadmlive_s...cret",
                "project_id": "proj/1",
                "scopes": ["admin"],
                "runtime_mode": None,
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "keys",
            "create",
            "--name",
            "Project operator",
            "--project",
            "dayplan",
            "--scope",
            "admin",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeHTTPClient.requests[1]["json"] == {
        "name": "Project operator",
        "project_id": "proj/1",
        "scopes": ["admin"],
    }


def test_setup_bootstrap_persisted_key_authenticates_smoke_without_export(
    tmp_path: Path,
) -> None:
    """After bootstrap, smoke should authenticate from saved config with no manual export."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "base_url": "http://api.test",
                "api_key": None,
                "metadata": {"installer": {"access_token": "gainst_access_secret"}},
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "demo-app"
    workspace.mkdir()
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "proj_1", "name": "Demo App", "slug": "demo-app"}),
        json_response(
            {
                "id": "key_1",
                "name": "Self-serve app backend",
                "api_key": "ga_runtime_secret_once",
                "masked_key": "ga...once",
                "project_id": "proj_1",
                "scopes": ["responses:create"],
            }
        ),
    ]

    bootstrap = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "setup",
            "--workspace",
            str(workspace),
            "--bootstrap",
            "--project-name",
            "Demo App",
            "--project-slug",
            "demo-app",
            "--json",
        ],
    )
    assert bootstrap.exit_code == 0, bootstrap.output

    # No manual export: smoke reads the persisted runtime key from config.
    FakeHTTPClient.requests = []
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {"id": "resp_smoke", "status": "completed", "output_text": "genaug-smoke-ok"}
        ),
    ]
    smoke_result = CliRunner().invoke(app, ["--config", str(config_path), "smoke", "--json"])

    assert smoke_result.exit_code == 0, smoke_result.output
    payload = json.loads(smoke_result.output)
    assert payload["verdict"] == "PASS"
    # The persisted runtime key authenticated the app-facing responses call.
    assert FakeHTTPClient.requests[1]["headers"]["Authorization"] == (
        "Bearer ga_runtime_secret_once"
    )


def test_tools_list_and_toggle(tmp_path: Path) -> None:
    """Tools commands should list and update enabled tools."""
    config_path = write_config(tmp_path)
    project = {"id": "p1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": ["web_search"]}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response([{"id": "web_search", "risk_level": "low", "requires_approval": False}]),
        json_response({"items": [project]}),
        json_response({}),
    ]
    runner = CliRunner()

    listed = runner.invoke(
        app,
        ["--config", str(config_path), "tools", "list", "--project", "dayplan"],
    )
    toggled = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "toggle",
            "web_search",
            "--project",
            "dayplan",
            "--disable",
        ],
    )

    assert listed.exit_code == 0
    assert toggled.exit_code == 0
    assert "web_search" in listed.output
    assert FakeHTTPClient.requests[-1]["json"] == {"tool_ids": []}


def test_tools_toggle_encodes_project_id(tmp_path: Path) -> None:
    """Project IDs with reserved characters should be URL-encoded."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({}),
    ]
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "toggle",
            "web_search",
            "--project",
            "dayplan",
            "--enable",
        ],
    )

    assert result.exit_code == 0
    assert FakeHTTPClient.requests[-1]["url"].endswith("/api/v1/admin/projects/proj%2F1/tools")


def test_tools_catalog_lists_normalized_project_tools(tmp_path: Path) -> None:
    """Tools catalog should show the normalized tenant catalog from the admin API."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    catalog = {
        "project_id": "proj/1",
        "schema_version": "general-augment-tool-catalog/v1",
        "counts": {"total": 3, "mcp": 1, "generated_openapi": 1, "unknown": 1},
        "items": [
            {
                "id": "get_ticket",
                "source": "mcp",
                "status": "available",
                "risk_level": "unknown",
                "approval_policy": "server_policy",
                "auth_requirement": "mcp_server",
            },
            {
                "id": "support_list_tickets",
                "source": "generated_openapi",
                "status": "available",
                "risk_level": "low",
                "approval_policy": "auto_execute",
                "auth_requirement": "identity_link",
            },
            {
                "id": "missing_tool",
                "source": "unknown",
                "status": "unavailable",
                "risk_level": "unknown",
                "approval_policy": "unknown",
                "auth_requirement": "unknown",
            },
        ],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(catalog),
        json_response({"items": [project]}),
        json_response(catalog),
    ]

    shown = CliRunner().invoke(
        app,
        ["--config", str(config_path), "tools", "catalog", "--project", "dayplan"],
    )
    filtered = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "catalog",
            "--project",
            "dayplan",
            "--source",
            "generated_openapi",
            "--json",
        ],
    )

    assert shown.exit_code == 0
    assert filtered.exit_code == 0
    shown_output = plain_cli_output(shown)
    assert "Tool Catalog" in shown_output
    assert "get_ticket" in shown_output
    assert "unavailable" in shown_output
    filtered_payload = json.loads(filtered.output)
    assert [item["id"] for item in filtered_payload["items"]] == ["support_list_tickets"]
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/tools/catalog"
    )


def test_tools_discovery_shows_and_updates_project_policy(tmp_path: Path) -> None:
    """Tool discovery command should configure the existing project policy contract."""
    config_path = write_config(tmp_path)
    project = {
        "id": "proj/1",
        "name": "DayPlan",
        "slug": "dayplan",
        "enabled_tool_ids": ["web_search"],
        "tool_discovery": {
            "mode": "auto",
            "direct_schema_tool_limit": 10,
            "max_search_results": 5,
            "approval_policy": {"mode": "tool_defaults"},
        },
    }
    updated_project = {
        **project,
        "tool_discovery": {
            "mode": "always",
            "direct_schema_tool_limit": 4,
            "max_search_results": 2,
            "approval_policy": {"mode": "risky_tools"},
        },
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [project]}),
        json_response(updated_project),
    ]
    runner = CliRunner()

    shown = runner.invoke(
        app,
        ["--config", str(config_path), "tools", "discovery", "--project", "dayplan"],
    )
    updated = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "discovery",
            "--project",
            "dayplan",
            "--mode",
            "always",
            "--direct-schema-tool-limit",
            "4",
            "--max-search-results",
            "2",
            "--approval-policy",
            "risky_tools",
            "--json",
        ],
    )

    assert shown.exit_code == 0
    assert updated.exit_code == 0
    assert "Tool Discovery" in shown.output
    assert json.loads(updated.output) == updated_project["tool_discovery"]
    assert FakeHTTPClient.requests[2]["method"] == "PATCH"
    assert FakeHTTPClient.requests[2]["url"].endswith("/api/v1/admin/projects/proj%2F1")
    assert FakeHTTPClient.requests[2]["json"] == {
        "tool_discovery": {
            "mode": "always",
            "direct_schema_tool_limit": 4,
            "max_search_results": 2,
            "approval_policy": {"mode": "risky_tools"},
        }
    }
    assert FakeHTTPClient.requests[2]["headers"] == {"X-Admin-Key": "secret"}


def test_tools_explain_turn_reports_dynamic_discovery_decision(tmp_path: Path) -> None:
    """Tool explain-turn should show how a request will expose schemas to Hermes."""
    config_path = write_config(tmp_path)
    project = {
        "id": "proj/1",
        "name": "DayPlan",
        "slug": "dayplan",
    }
    runtime_policy = {
        "tool_discovery": {
            "mode": "auto",
            "direct_schema_tool_limit": 2,
            "max_search_results": 5,
        },
        "hermes_exposure": {
            "uses_dynamic_discovery_by_default": True,
            "direct_platform_tool_count": 0,
            "search_result_limit": 5,
        },
    }
    catalog = {
        "counts": {"total": 3, "enabled": 2, "unavailable": 1},
        "items": [
            {"id": "get_ticket", "enabled": True, "status": "available"},
            {"id": "close_ticket", "enabled": True, "status": "available"},
            {"id": "missing_tool", "enabled": False, "status": "unavailable"},
        ],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(runtime_policy),
        json_response(catalog),
        json_response({"items": [project]}),
        json_response(runtime_policy),
        json_response(catalog),
    ]
    runner = CliRunner()

    shown = runner.invoke(
        app,
        ["--config", str(config_path), "tools", "explain-turn", "--project", "dayplan"],
    )
    explicit = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "explain-turn",
            "--project",
            "dayplan",
            "--requested-tool",
            "missing_tool",
            "--json",
        ],
    )

    assert shown.exit_code == 0
    assert explicit.exit_code == 0
    assert "Tool Discovery Decision" in plain_cli_output(shown)
    explicit_payload = json.loads(explicit.output)
    assert explicit_payload["schema_version"] == "genaug.tool_discovery_explanation.v1"
    assert explicit_payload["decision"]["exposure"] == "explicit_tool_subset"
    assert explicit_payload["decision"]["unavailable_requested_tools"] == ["missing_tool"]
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/runtime-policy"
    )
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/tools/catalog"
    )


def test_tools_discovery_rejects_invalid_mode(tmp_path: Path) -> None:
    """Tool discovery mode should stay within the documented project schema."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "discovery",
            "--project",
            "dayplan",
            "--mode",
            "everything",
        ],
    )

    assert result.exit_code != 0
    assert "--mode must be one of: auto, always, direct" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_skills_list_view_apply_and_delete(tmp_path: Path) -> None:
    """Skills commands should manage tenant SKILL.md files through the admin API."""
    config_path = write_config(tmp_path)
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text(skill_markdown("Support Triage"), encoding="utf-8")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "items": [
                    {
                        "name": "Support Triage",
                        "description": "Route support work.",
                        "version": "1.0",
                        "tags": ["support"],
                        "tools": ["web_search"],
                        "path": "skills/support-triage/SKILL.md",
                    }
                ]
            }
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "name": "Support Triage",
                "content": skill_markdown("Support Triage"),
                "metadata": {"name": "Support Triage"},
            }
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "name": "Support Triage",
                "content": skill_markdown("Support Triage"),
                "metadata": {"name": "Support Triage"},
            }
        ),
        json_response({"items": [project]}),
        json_response({"status": "deleted", "name": "Support Triage"}),
    ]
    runner = CliRunner()

    listed = runner.invoke(
        app,
        ["--config", str(config_path), "skills", "list", "--project", "dayplan"],
    )
    viewed = runner.invoke(
        app,
        ["--config", str(config_path), "skills", "view", "Support Triage", "--project", "dayplan"],
    )
    applied = runner.invoke(
        app,
        ["--config", str(config_path), "skills", "apply", str(skill_path), "--project", "dayplan"],
    )
    deleted = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "skills",
            "delete",
            "Support Triage",
            "--project",
            "dayplan",
        ],
    )

    assert listed.exit_code == 0
    assert viewed.exit_code == 0
    assert applied.exit_code == 0
    assert deleted.exit_code == 0
    assert "Support Triage" in listed.output
    assert "Route support work." in viewed.output
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/skills")
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/skills/Support%20Triage"
    )
    assert FakeHTTPClient.requests[5]["method"] == "POST"
    assert FakeHTTPClient.requests[5]["json"] == {"content": skill_markdown("Support Triage")}
    assert FakeHTTPClient.requests[7]["method"] == "DELETE"


def test_skills_list_json_is_machine_readable(tmp_path: Path) -> None:
    """Skills list JSON should preserve API response shape."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [{"name": "Support Triage", "description": "Route work."}]}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "skills", "list", "--project", "dayplan", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["items"][0]["name"] == "Support Triage"


def test_tools_add_mcp_alias_uses_project_mcp_endpoint(tmp_path: Path) -> None:
    """The tools namespace should expose the first-afternoon MCP add workflow."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    server = {
        "name": "github",
        "url": "https://mcp.github.example.com/mcp",
        "tools": {"include": ["search_repos"]},
        "enabled": True,
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(server),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "tools",
            "add-mcp",
            "github",
            "--project",
            "dayplan",
            "--url",
            "https://mcp.github.example.com/mcp",
            "--include-tool",
            "search_repos",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == server
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/mcp-servers")
    assert FakeHTTPClient.requests[1]["json"] == server


def test_status_json_includes_queue_depths_and_project_usage(tmp_path: Path) -> None:
    """Machine-readable status should expose queue pressure and project usage."""
    config_path = write_config(tmp_path)
    project = {"id": "p1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response({"status": "ok"}),
        json_response({"status": "ready"}),
        text_response(
            "\n".join(
                [
                    "# HELP general_augment_queue_depth Current queue depth by queue name.",
                    'general_augment_queue_depth{queue="arq:queue"} 3.0',
                    'general_augment_queue_depth{queue="priority"} 1',
                ]
            )
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "totals": {
                    "agent_turns_count": 2,
                    "messages_count": 3,
                    "tool_calls_count": 1,
                    "total_cost_usd": 0.12,
                }
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "status", "--project", "dayplan", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["live"]["status"] == "ok"
    assert payload["ready"]["status"] == "ready"
    assert payload["metrics"]["queue_depths"] == [
        {"depth": 3, "queue": "arq:queue"},
        {"depth": 1, "queue": "priority"},
    ]
    assert payload["project"]["usage"]["agent_turns_count"] == 2
    assert FakeHTTPClient.requests[2]["url"] == "http://api.test/metrics"


def test_smoke_calls_ready_and_responses_with_correlation_headers(tmp_path: Path) -> None:
    """Smoke should exercise app-facing responses with replay/debug headers."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {
                "id": "resp_smoke",
                "status": "completed",
                "model": "mock/balanced",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "genaug-smoke-ok"}],
                    }
                ],
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 24,
                    "total_tokens": 104,
                },
                "metadata": {
                    "trace_id": "trace_smoke",
                    "request_id": "req_smoke",
                    "general_augment_latency_ms": 912,
                    "general_augment_cost_usd": 0.004,
                },
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "smoke",
            "--idempotency-key",
            "smoke-replay-1",
            "--request-id",
            "req_smoke",
            "--metadata",
            "feature=spark",
        ],
    )

    assert result.exit_code == 0
    assert "resp_smoke" in result.output
    assert "genaug-smoke-ok" in result.output
    assert "Latency" in result.output
    assert "912 ms" in result.output
    assert "Tokens" in result.output
    assert "104 total (80 input, 24 output)" in result.output
    assert "Cost" in result.output
    assert "0.004" in result.output
    assert "Support receipt" in result.output
    assert "genaug-cli-smoke" in result.output
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/health/ready"
    assert FakeHTTPClient.requests[1]["url"] == "http://api.test/v1/responses"
    assert FakeHTTPClient.requests[1]["headers"] == {
        "Authorization": "Bearer secret",
        "X-Idempotency-Key": "smoke-replay-1",
        "X-Request-ID": "req_smoke",
    }
    assert FakeHTTPClient.requests[1]["json"]["metadata"] == {
        "source": "genaug-cli-smoke",
        "feature": "spark",
    }


def test_smoke_can_scope_management_key_to_project(tmp_path: Path) -> None:
    """Smoke should send X-Project-ID when using a management key."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"items": [project]}),
        json_response(
            {"id": "resp_smoke", "status": "completed", "output_text": "genaug-smoke-ok"}
        ),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "smoke", "--project", "dayplan", "--json"],
    )

    assert result.exit_code == 0
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects")
    assert FakeHTTPClient.requests[2]["headers"]["X-Project-ID"] == "proj/1"
    payload = json.loads(result.output)
    assert payload["ready"] == {"status": "ready"}
    assert payload["response"]["id"] == "resp_smoke"
    assert payload["response_id"] == "resp_smoke"


def test_smoke_resolves_uuid_project_by_direct_lookup(tmp_path: Path) -> None:
    """UUID project refs should not depend on the first project-list page."""
    config_path = write_config(tmp_path)
    project_id = "d4b45a89-dd1e-4add-9434-4b252adf1d5a"
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"id": project_id, "name": "Spark", "slug": "spark"}),
        json_response(
            {"id": "resp_smoke", "status": "completed", "output_text": "genaug-smoke-ok"}
        ),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "smoke", "--project", project_id, "--json"],
    )

    assert result.exit_code == 0, result.output
    assert FakeHTTPClient.requests[1]["url"].endswith(f"/api/v1/admin/projects/{project_id}")
    assert FakeHTTPClient.requests[2]["headers"]["X-Project-ID"] == project_id


def test_smoke_resolves_project_beyond_first_list_page(tmp_path: Path) -> None:
    """Slug/name refs should paginate through the admin project list."""
    config_path = write_config(tmp_path)
    first_page = [
        {"id": f"proj_{index}", "name": f"Project {index}", "slug": f"project-{index}"}
        for index in range(1000)
    ]
    project = {"id": "spark-id", "name": "Spark", "slug": "spark"}
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"items": first_page}),
        json_response({"items": [project]}),
        json_response(
            {"id": "resp_smoke", "status": "completed", "output_text": "genaug-smoke-ok"}
        ),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "smoke", "--project", "spark", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert FakeHTTPClient.requests[1]["params"] == {"limit": 1000, "offset": 0}
    assert FakeHTTPClient.requests[2]["params"] == {"limit": 1000, "offset": 1000}
    assert FakeHTTPClient.requests[3]["headers"]["X-Project-ID"] == "spark-id"


def test_smoke_invalid_runtime_key_explains_project_key_recovery(tmp_path: Path) -> None:
    """App-facing smoke auth failures should point at project runtime keys."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"detail": "Unauthorized"}, status_code=401),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke"])

    assert result.exit_code != 0
    assert "project runtime key" in result.output
    assert "server-side" in result.output
    assert "Unauthorized" in result.output


def test_smoke_budget_limit_explains_billing_recovery(tmp_path: Path) -> None:
    """App-facing smoke budget failures should name billing and budget checks."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {
                "detail": {
                    "reason": "llm_budget_exhausted",
                    "message": "LLM daily budget exhausted.",
                }
            },
            status_code=402,
        ),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke"])
    output = plain_cli_output(result)

    assert result.exit_code != 0
    assert "billing" in output
    assert "LLM budget limit" in output
    assert "genaug billing status" in output
    assert "LLM daily budget exhausted" in output


def test_smoke_rate_limit_explains_retry_after(tmp_path: Path) -> None:
    """App-facing smoke rate limits should preserve stable reason and retry timing."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {
                "detail": {
                    "reason": "messages_per_user_per_minute_exceeded",
                    "message": "Project per-user message rate limit exceeded.",
                }
            },
            status_code=429,
            headers={"Retry-After": "30"},
        ),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke"])

    assert result.exit_code != 0
    assert "messages_per_user_per_minute_exceeded" in result.output
    assert "Retry after 30 seconds" in result.output


def test_app_facing_provider_failure_includes_model_provider_next_actions() -> None:
    """Structured provider failures should point operators at provider controls."""

    message = helpful_api_error(
        503,
        {
            "detail": {
                "message": "OpenAI route is unavailable.",
                "provider": "openai",
                "model": "gpt-5",
                "model_provider_source": "tenant",
            }
        },
        request_path="/v1/responses",
        auth_mode="bearer",
    )

    assert "provider=openai" in message
    assert "model=gpt-5" in message
    assert "source=tenant" in message
    assert "genaug model-providers check --project <project> --provider openai" in message
    assert "genaug projects runtime-policy --project <project> --json" in message


def test_app_facing_provider_failure_preserves_api_next_actions() -> None:
    """Backend-supplied remediation commands should beat generic provider guesses."""

    message = helpful_api_error(
        500,
        {
            "detail": {
                "message": "Model route is not configured.",
                "model_provider": "anthropic",
                "next_actions": [
                    {
                        "command": (
                            "genaug model-providers upsert --project <project> --provider anthropic"
                        )
                    },
                    "genaug providers smoke --provider anthropic --project <project>",
                ],
            }
        },
        request_path="/v1/responses",
        auth_mode="bearer",
    )

    assert "provider=anthropic" in message
    assert "genaug model-providers upsert --project <project> --provider anthropic" in message
    assert "genaug providers smoke --provider anthropic --project <project>" in message


def test_smoke_json_includes_readiness_and_trace_ids(tmp_path: Path) -> None:
    """Machine-readable smoke output should keep health proof with the response."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response(
            {
                "id": "resp_smoke",
                "status": "completed",
                "output_text": "genaug-smoke-ok",
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 24,
                    "total_tokens": 104,
                },
                "metadata": {
                    "general_augment_cost_usd": 0.004,
                    "general_augment_request_id": "req_1",
                    "general_augment_trace_id": "trace_1",
                    "general_augment_latency_ms": 912,
                },
            }
        ),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ready"]["status"] == "ok"
    assert payload["ready"]["db"] == "connected"
    assert payload["response"]["id"] == "resp_smoke"
    assert payload["response_id"] == "resp_smoke"
    assert payload["request_id"] == "req_1"
    assert payload["trace_id"] == "trace_1"
    assert payload["support_receipt"] == {
        "source": "genaug-cli-smoke",
        "project_id": None,
        "project_slug": None,
        "response_id": "resp_smoke",
        "request_id": "req_1",
        "trace_id": "trace_1",
        "model": None,
        "status": "completed",
        "latency_ms": 912,
        "input_tokens": 80,
        "output_tokens": 24,
        "total_tokens": 104,
        "cost_usd": 0.004,
        "ready_status": "ok",
        "next_action": (
            "Open the response in dashboard observability, then verify trace and "
            "memory evidence before production traffic."
        ),
    }


def test_smoke_writes_launch_evidence_with_support_bundle(tmp_path: Path) -> None:
    """Smoke evidence should link response proof, support bundle, and dashboard trace URLs."""
    config_path = write_config(tmp_path)
    evidence_path = tmp_path / "smoke-evidence.json"
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    support_bundle = {
        "api_version": "genaug.observability_support_bundle.v1",
        "project_id": "proj/1",
        "metrics": {"trace_count": 1, "audit_event_count": 1, "usage_event_count": 1},
        "traces": [
            {
                "trace_id": "trace_1",
                "response_id": "resp_smoke",
                "metadata": {"api_key": "support-api-key-secret"},
            }
        ],
        "audit_events": [{"event_type": "responses.create"}],
        "usage_events": [{"event_type": "agent_turn"}],
    }
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"items": [project]}),
        json_response(
            {
                "id": "resp_smoke",
                "status": "completed",
                "model": "mock/balanced",
                "output_text": "genaug-smoke-ok",
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 24,
                    "total_tokens": 104,
                },
                "metadata": {
                    "general_augment_request_id": "req_1",
                    "general_augment_trace_id": "trace_1",
                    "general_augment_latency_ms": 912,
                },
            }
        ),
        json_response(support_bundle),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "smoke",
            "--project",
            "dayplan",
            "--include-support-bundle",
            "--evidence-output",
            str(evidence_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert_no_canary_secrets(result.output)
    payload = json.loads(result.output)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert_no_canary_secrets(payload)
    assert_no_canary_secrets(evidence)
    assert payload["evidence"]["schema_version"] == "general-augment-smoke-evidence/v1"
    assert evidence["support_receipt"]["trace_id"] == "trace_1"
    assert evidence["support_receipt"]["response_id"] == "resp_smoke"
    assert evidence["support_receipt"]["status"] == "completed"
    assert evidence["support_receipt"]["latency_ms"] == 912
    assert evidence["support_receipt"]["total_tokens"] == 104
    assert "dashboard observability" in evidence["support_receipt"]["next_action"]
    assert evidence["support_bundle"]["traces"][0]["metadata"]["api_key"] == "[REDACTED]"
    assert "trace_id=trace_1" in evidence["dashboard_urls"]["observability_url"]
    assert "response_id=resp_smoke" in evidence["dashboard_urls"]["observability_url"]
    assert evidence["dashboard_urls"]["project_url"].endswith("/projects/dayplan")
    assert evidence["security"]["raw_secrets_included"] is False
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/observability/support-bundle"
    )
    assert FakeHTTPClient.requests[3]["params"] == {
        "limit": 25,
        "user_id": "genaug-smoke",
        "trace_id": "trace_1",
        "response_id": "resp_smoke",
    }


def test_smoke_can_import_launch_evidence_after_success(tmp_path: Path) -> None:
    """Smoke should be able to retain its evidence in launch-readiness review."""

    config_path = write_config(tmp_path)
    evidence_path = tmp_path / "smoke-evidence.json"
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"items": [project]}),
        json_response(
            {
                "id": "resp_smoke",
                "status": "completed",
                "output_text": "genaug-smoke-ok",
                "metadata": {
                    "general_augment_request_id": "req_1",
                    "general_augment_trace_id": "trace_1",
                },
            }
        ),
        json_response(
            {
                "schema_version": "genaug.launch_readiness_evidence_import.v1",
                "audit_event_id": "audit_1",
                "artifact_type": "smoke_evidence",
                "artifact_sha256": "abc123",
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "smoke",
            "--project",
            "dayplan",
            "--evidence-output",
            str(evidence_path),
            "--import-evidence",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["evidence_import"]["audit_event_id"] == "audit_1"
    import_request = FakeHTTPClient.requests[3]
    assert import_request["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/launch-readiness/evidence"
    )
    assert import_request["json"]["artifact"] == evidence
    assert import_request["json"]["artifact_type"] == "smoke_evidence"
    assert import_request["json"]["source"] == "cli"
    assert import_request["json"]["artifact_path"] == str(evidence_path)


def test_smoke_import_evidence_requires_project(tmp_path: Path) -> None:
    """Evidence import needs a project because the retained audit receipt is project scoped."""

    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "smoke", "--import-evidence"],
    )

    assert result.exit_code != 0
    assert "--import-evidencerequires--project" in compact_cli_output(result)


def test_smoke_fails_on_empty_agent_reply(tmp_path: Path) -> None:
    """An HTTP 200 with an empty body must fail so agents gating on exit code are safe."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"id": "resp_smoke", "status": "completed", "output_text": ""}),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke", "--json"])

    assert result.exit_code != 0
    payload = first_json_object(result.output)
    assert payload["verdict"] == "FAIL"
    assert "empty response body" in payload["verdict_detail"]
    assert "genaug smoke" in payload["verdict_detail"]


def test_smoke_fails_when_agent_does_not_echo_expected_token(tmp_path: Path) -> None:
    """A wrong reply to the built-in prompt must fail even on HTTP 200."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {"id": "resp_smoke", "status": "completed", "output_text": "totally unrelated"}
        ),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke", "--json"])

    assert result.exit_code != 0
    payload = first_json_object(result.output)
    assert payload["verdict"] == "FAIL"
    assert "genaug-smoke-ok" in payload["verdict_detail"]


def test_smoke_passes_on_good_reply(tmp_path: Path) -> None:
    """A well-formed reply that echoes the token should pass with exit 0."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {"id": "resp_smoke", "status": "completed", "output_text": "genaug-smoke-ok"}
        ),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "smoke", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verdict"] == "PASS"


def test_smoke_fails_on_bad_structured_output(tmp_path: Path) -> None:
    """Structured smoke must fail when the model does not satisfy the built-in schema."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {
                "id": "resp_smoke",
                "status": "completed",
                "output_text": '{"ok": false, "label": "nope"}',
            }
        ),
    ]

    result = CliRunner().invoke(
        app, ["--config", str(config_path), "smoke", "--structured", "--json"]
    )

    assert result.exit_code != 0
    payload = first_json_object(result.output)
    assert payload["verdict"] == "FAIL"


def test_smoke_memory_recall_seeds_memory_and_records_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory recall smoke should prove memory through /v1/responses, not prompt echo."""

    config_path = write_config(tmp_path)
    evidence_path = tmp_path / "smoke-evidence.json"
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}

    def memory_response(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        recall_code = re.search(r"genaug-memory-[a-f0-9]+", str(payload["fact"]))
        assert recall_code is not None
        return json_response({"memory_id": "mem_smoke", "status": "stored"})

    def responses_response(request: httpx.Request) -> httpx.Response:
        memory_payload = FakeHTTPClient.requests[2]["json"]
        recall_code = re.search(r"genaug-memory-[a-f0-9]+", str(memory_payload["fact"]))
        assert recall_code is not None
        return json_response(
            {
                "id": "resp_memory_smoke",
                "status": "completed",
                "output_text": recall_code.group(0),
                "metadata": {
                    "general_augment_request_id": "req_memory",
                    "general_augment_trace_id": "trace_memory",
                },
            }
        )

    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"items": [project]}),
    ]
    original_request = FakeHTTPClient.request

    def request_with_dynamic_responses(
        self: FakeHTTPClient,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if url.endswith("/api/v1/agent/memory/store"):
            self.requests.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers or {},
                    "json": json,
                    "params": params,
                }
            )
            return memory_response(httpx.Request(method, url, json=json))
        if url.endswith("/v1/responses"):
            self.requests.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers or {},
                    "json": json,
                    "params": params,
                }
            )
            return responses_response(httpx.Request(method, url, json=json))
        return original_request(self, method, url, headers=headers, json=json, params=params)

    monkeypatch.setattr(FakeHTTPClient, "request", request_with_dynamic_responses)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "smoke",
            "--project",
            "dayplan",
            "--memory-recall",
            "--evidence-output",
            str(evidence_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    memory_request = FakeHTTPClient.requests[2]
    response_request = FakeHTTPClient.requests[3]
    recall_code = re.search(r"genaug-memory-[a-f0-9]+", memory_request["json"]["fact"])
    assert recall_code is not None
    assert memory_request["url"] == "http://api.test/api/v1/agent/memory/store"
    assert memory_request["headers"]["X-Project-ID"] == "proj/1"
    assert memory_request["json"]["user_id"] == "genaug-smoke"
    assert response_request["url"] == "http://api.test/v1/responses"
    assert recall_code.group(0) not in response_request["json"]["input"]
    assert payload["memory_recall"]["status"] == "passed"
    assert payload["memory_recall"]["seed_memory_id"] == "mem_smoke"
    assert payload["memory_recall"]["expected_present_in_response"] is True
    assert payload["memory_recall"]["prompt_included_expected_code"] is False
    assert evidence["memory_recall"] == payload["memory_recall"]
    assert recall_code.group(0) not in json.dumps(evidence)


def test_smoke_memory_recall_fails_when_response_misses_seeded_fact(tmp_path: Path) -> None:
    """Memory recall smoke should fail CI when the runtime does not recall the seeded fact."""

    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"items": [project]}),
        json_response({"memory_id": "mem_smoke", "status": "stored"}),
        json_response(
            {
                "id": "resp_memory_smoke",
                "status": "completed",
                "output_text": "I do not know the recall code.",
                "metadata": {
                    "general_augment_request_id": "req_memory",
                    "general_augment_trace_id": "trace_memory",
                },
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "smoke",
            "--project",
            "dayplan",
            "--memory-recall",
            "--json",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["memory_recall"]["status"] == "failed"
    assert payload["memory_recall"]["expected_present_in_response"] is False
    assert payload["memory_recall"]["prompt_included_expected_code"] is False


def test_providers_smoke_plans_launch_evidence_for_all_requested_options() -> None:
    """Provider smoke planning should expose launch evidence and exact blockers."""
    result = CliRunner().invoke(
        app,
        [
            "providers",
            "smoke",
            "--capability",
            "code",
            "--capability",
            "browse",
            "--capability",
            "video",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "general-augment-provider-smoke/v1"
    assert payload["mode"] == "plan"
    assert payload["security"] == {
        "raw_secrets_in_output": False,
        "raw_provider_payloads_in_output": False,
    }
    items = {item["provider"]: item for item in payload["providers"]}
    assert list(items) == [
        "anthropic-managed-agents",
        "codex-mcp",
        "browserbase",
        "xai",
        "fal",
        "veo",
    ]
    assert "managed_agent_session_id" in items["anthropic-managed-agents"]["required_evidence"]
    assert "codex_thread_id" in items["codex-mcp"]["required_evidence"]
    assert "browser_session_id" in items["browserbase"]["required_evidence"]
    assert "tool_call_audit" in items["xai"]["required_evidence"]
    assert "signed_media_url" in items["fal"]["required_evidence"]
    assert "retention_policy" in items["veo"]["required_evidence"]
    assert items["browserbase"]["status"] == "blocked"
    assert "Pass --project" in items["browserbase"]["blockers"]
    assert "Set BROWSERBASE_API_KEY" in items["browserbase"]["blockers"]


def test_providers_smoke_writes_redacted_evidence_artifact(tmp_path: Path) -> None:
    """Provider smoke should be able to persist support-ready evidence JSON."""
    evidence_path = tmp_path / "provider-smoke.json"

    result = CliRunner().invoke(
        app,
        [
            "providers",
            "smoke",
            "--provider",
            "browserbase",
            "--project",
            "spark",
            "--evidence-output",
            str(evidence_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload == evidence
    assert payload["schema_version"] == "general-augment-provider-smoke/v1"
    assert payload["artifact_path"] == str(evidence_path)
    assert payload["generated_at"].endswith("Z")
    assert payload["providers"][0]["provider"] == "browserbase"
    assert payload["providers"][0]["status"] == "blocked"
    assert payload["security"] == {
        "raw_secrets_in_output": False,
        "raw_provider_payloads_in_output": False,
    }


def test_providers_smoke_runs_model_provider_health_and_responses_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-provider smoke should prove custody, health, response, and support evidence."""
    config_path = write_config(tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-should-not-print")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    support_bundle = {
        "usage_events": [
            {
                "event_type": "agent_turn",
                "metadata": {
                    "response_id": "resp_provider_smoke",
                    "model_provider": "xai",
                    "model_provider_source": "tenant",
                },
            }
        ],
        "control_plane_events": [
            {
                "event_type": "model_provider_credential.health_check",
                "metadata": {"provider": "xai", "status": "available"},
            }
        ],
        "audit_events": [
            {
                "action_type": "tool_call",
                "tool_id": "x_search",
                "success": True,
            }
        ],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "provider": "xai",
                "status": "active",
                "base_url_configured": False,
                "model_prefixes": ["xai/", "grok-"],
            }
        ),
        json_response(
            {
                "provider": "xai",
                "status": "available",
                "message": "Provider credential accepted by health endpoint.",
                "last_validated_at": "2026-05-24T05:00:00Z",
            }
        ),
        json_response(
            {
                "id": "resp_provider_smoke",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "tenant-provider-smoke-ok"}],
                    }
                ],
                "metadata": {
                    "general_augment_trace_id": "trace_provider_smoke",
                    "general_augment_model_provider": "xai",
                    "general_augment_model_provider_source": "tenant",
                },
            }
        ),
        json_response(support_bundle),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "smoke",
            "--provider",
            "xai",
            "--project",
            "dayplan",
            "--api-key-env",
            "XAI_API_KEY",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "xai-secret-should-not-print" not in result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "live"
    assert payload["providers"][0]["status"] == "passed"
    assert payload["providers"][0]["evidence"]["response_id"] == "resp_provider_smoke"
    assert payload["providers"][0]["evidence"]["trace_id"] == "trace_provider_smoke"
    assert payload["providers"][0]["checks"] == [
        {"name": "credential_custody", "status": "passed"},
        {"name": "provider_health", "status": "passed"},
        {"name": "responses_smoke", "status": "passed"},
        {"name": "support_bundle", "status": "passed"},
        {"name": "launch_evidence", "status": "passed"},
    ]
    assert FakeHTTPClient.requests[0]["url"].endswith("/api/v1/admin/projects")
    assert FakeHTTPClient.requests[1]["method"] == "PUT"
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/model-providers/xai"
    )
    assert FakeHTTPClient.requests[1]["json"]["api_key"] == "xai-secret-should-not-print"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/model-providers/xai/health-check"
    )
    assert FakeHTTPClient.requests[3]["url"] == "http://api.test/v1/responses"
    assert FakeHTTPClient.requests[3]["headers"]["X-Project-ID"] == "proj/1"
    assert FakeHTTPClient.requests[4]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/observability/support-bundle"
    )
    assert FakeHTTPClient.requests[4]["params"] == {
        "limit": 50,
        "response_id": "resp_provider_smoke",
        "trace_id": "trace_provider_smoke",
    }


def test_providers_smoke_blocks_xai_without_tool_call_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X launch smoke should not pass on generic model-provider evidence alone."""
    config_path = write_config(tmp_path)
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-should-not-print")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    support_bundle = {
        "usage_events": [
            {
                "event_type": "agent_turn",
                "metadata": {
                    "response_id": "resp_provider_smoke",
                    "model_provider": "xai",
                    "model_provider_source": "tenant",
                },
            }
        ],
        "control_plane_events": [
            {
                "event_type": "model_provider_credential.health_check",
                "metadata": {"provider": "xai", "status": "available"},
            }
        ],
        "audit_events": [],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"provider": "xai", "status": "active"}),
        json_response({"provider": "xai", "status": "available"}),
        json_response(
            {
                "id": "resp_provider_smoke",
                "status": "completed",
                "output_text": "tenant-provider-smoke-ok",
                "metadata": {
                    "general_augment_trace_id": "trace_provider_smoke",
                    "general_augment_model_provider": "xai",
                    "general_augment_model_provider_source": "tenant",
                },
            }
        ),
        json_response(support_bundle),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "smoke",
            "--provider",
            "xai",
            "--project",
            "dayplan",
            "--api-key-env",
            "XAI_API_KEY",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "xai-secret-should-not-print" not in result.output
    payload = json.loads(result.output)
    item = payload["providers"][0]
    assert item["status"] == "blocked"
    assert item["checks"][-1] == {"name": "launch_evidence", "status": "blocked"}
    assert (
        "Support bundle did not include X search/video tool-call audit evidence."
        in (item["blockers"])
    )
    assert item["evidence"]["tool_call_audit"]["matching_event_count"] == 0


def test_providers_smoke_blocks_video_without_media_lifecycle_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Video launch smoke should require generated-media storage and retention proof."""
    config_path = write_config(tmp_path)
    monkeypatch.setenv("FAL_API_KEY", "fal-secret-should-not-print")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    support_bundle = {
        "usage_events": [
            {
                "event_type": "agent_turn",
                "metadata": {
                    "response_id": "resp_video_smoke",
                    "model_provider": "fal",
                    "model_provider_source": "tenant",
                },
            }
        ],
        "control_plane_events": [
            {
                "event_type": "model_provider_credential.health_check",
                "metadata": {"provider": "fal", "status": "available"},
            }
        ],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"provider": "fal", "status": "active"}),
        json_response({"provider": "fal", "status": "available"}),
        json_response(
            {
                "id": "resp_video_smoke",
                "status": "completed",
                "output_text": "tenant-provider-smoke-ok",
                "metadata": {
                    "general_augment_trace_id": "trace_video_smoke",
                    "general_augment_model_provider": "fal",
                    "general_augment_model_provider_source": "tenant",
                    "general_augment_media_asset_id": "media_123",
                    "general_augment_signed_media_url": (
                        "https://storage.example.com/video.mp4?signature=secret"
                    ),
                },
            }
        ),
        json_response(support_bundle),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "smoke",
            "--provider",
            "fal",
            "--project",
            "dayplan",
            "--api-key-env",
            "FAL_API_KEY",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "fal-secret-should-not-print" not in result.output
    assert "signature=secret" not in result.output
    item = json.loads(result.output)["providers"][0]
    assert item["status"] == "blocked"
    assert item["checks"][-1] == {"name": "launch_evidence", "status": "blocked"}
    assert (
        "Generated-video response did not include retention policy evidence." in (item["blockers"])
    )
    assert item["evidence"]["generated_media"] == {
        "media_asset_id": "media_123",
        "signed_media_url_present": True,
        "retention_policy": None,
    }


def test_providers_smoke_returns_blocked_evidence_when_health_api_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider smoke should produce an artifact instead of crashing on API 5xx."""
    config_path = write_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-should-not-print")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "provider": "codex-mcp",
                "status": "active",
                "base_url_configured": False,
            }
        ),
        json_response({"detail": "Internal Server Error"}, status_code=500),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "smoke",
            "--provider",
            "codex-mcp",
            "--project",
            "dayplan",
            "--api-key-env",
            "OPENAI_API_KEY",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "openai-secret-should-not-print" not in result.output
    payload = json.loads(result.output)
    item = payload["providers"][0]
    assert item["status"] == "blocked"
    assert item["checks"] == [{"name": "platform_api", "status": "blocked"}]
    assert item["evidence"]["platform_api"]["status_code"] == 500
    assert "platform API returned 500" in item["blockers"][0]
    assert "Codex MCP launch still requires managed MCP discovery" in item["blockers"][1]


def test_providers_smoke_surfaces_capability_provider_health_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capability-provider smokes should name rejected credentials before launch blockers."""
    config_path = write_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-should-not-print")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "provider": "codex-mcp",
                "status": "active",
                "base_url_configured": False,
            }
        ),
        json_response(
            {
                "provider": "codex-mcp",
                "status": "unavailable",
                "message": "Provider rejected the credential.",
                "checked_at": "2026-05-24T09:32:56Z",
                "latency_ms": 133,
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "providers",
            "smoke",
            "--provider",
            "codex-mcp",
            "--project",
            "dayplan",
            "--api-key-env",
            "OPENAI_API_KEY",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "openai-secret-should-not-print" not in result.output
    item = json.loads(result.output)["providers"][0]
    assert item["status"] == "blocked"
    assert item["checks"] == [
        {"name": "credential_custody", "status": "passed"},
        {"name": "provider_health", "status": "blocked"},
    ]
    assert item["evidence"]["provider_health"]["message"] == "Provider rejected the credential."
    assert item["blockers"][0] == (
        "Provider health status is unavailable: Provider rejected the credential."
    )
    assert "Codex MCP launch still requires managed MCP discovery" in item["blockers"][1]


def test_smoke_can_request_structured_output(tmp_path: Path) -> None:
    """Smoke should support schema-constrained Responses calls."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response(
            {
                "id": "resp_structured",
                "status": "completed",
                "model": "mock/balanced",
                "output_text": '{"label": "genaug-smoke-ok", "ok": true}',
                "metadata": {"trace_id": "trace_structured", "request_id": "req_structured"},
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "smoke", "--structured"],
    )

    assert result.exit_code == 0
    assert "json_schema" in result.output
    payload = FakeHTTPClient.requests[1]["json"]
    assert payload["input"] == 'Return JSON with ok=true and label="genaug-smoke-ok".'
    assert payload["text"]["format"]["type"] == "json_schema"
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["required"] == ["ok", "label"]


def test_smoke_can_load_structured_schema_file(tmp_path: Path) -> None:
    """A schema file should imply structured output for app-specific checks."""
    config_path = write_config(tmp_path)
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        '{"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"]}',
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response({"status": "ready"}),
        json_response({"id": "resp_structured", "status": "completed", "output_text": "{}"}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "smoke", "--schema-file", str(schema_path), "--json"],
    )

    assert result.exit_code == 0
    payload = FakeHTTPClient.requests[1]["json"]
    assert payload["text"]["format"]["schema"]["required"] == ["summary"]


def test_doctor_checks_config_health_and_auth(tmp_path: Path) -> None:
    """Doctor should preflight CLI auth and API readiness without exposing secrets."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "doctor"])

    assert result.exit_code == 0
    assert "General Augment Doctor" in result.output
    assert "configured" in result.output
    assert "secret" not in result.output
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/health/ready"
    assert FakeHTTPClient.requests[1]["url"] == "http://api.test/api/v1/admin/me"
    assert FakeHTTPClient.requests[1]["headers"] == {"X-Admin-Key": "secret"}


def test_doctor_can_check_project_agent_cloud_readiness(tmp_path: Path) -> None:
    """Project doctor should preflight read-only agent-cloud surfaces."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
        json_response({"items": [{"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}]}),
        json_response(project_launch_readiness_response(verdict="ready")),
        json_response({"tool_discovery": {"mode": "auto"}}),
        json_response({"items": [{"id": "web_search", "status": "available"}]}),
        json_response(
            {
                "items": [
                    {
                        "provider": "anthropic-managed-agents",
                        "delegated_workflows": ["coding"],
                        "planned_workflows": ["research"],
                        "readiness": "ready",
                    },
                    {
                        "provider": "browserbase",
                        "delegated_workflows": ["browser", "browser_action"],
                        "planned_workflows": [],
                        "readiness": "setup_required",
                        "readiness_details": {
                            "browser_artifact_storage_backend": "filesystem",
                            "hosted_screenshot_storage": "local_only",
                        },
                    },
                ]
            }
        ),
        json_response({"items": [{"approval_id": "approval/1"}]}),
        json_response({"items": [{"id": "run/1", "status": "completed"}]}),
        json_response(
            {
                "id": "run/1",
                "status": "completed",
                "run_events": [{"event_type": "agent_run.completed"}],
            }
        ),
        json_response({"facts": [{"id": "memory/1"}], "profile": {"timezone": "America/Toronto"}}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "doctor",
            "--project",
            "dayplan",
            "--user",
            "user-1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verdict"] == "PASS"
    assert payload["launch_readiness"]["schema_version"] == "genaug.project_launch_readiness.v1"
    check_names = {item["name"] for item in payload["checks"]}
    assert {
        "project",
        "launch_readiness",
        "runtime_policy",
        "tool_catalog",
        "delegated_providers",
        "approvals_inbox",
        "run_timeline",
        "memory_profile",
        "governance_proof",
    } <= check_names
    delegated_check = next(
        item for item in payload["checks"] if item["name"] == "delegated_providers"
    )
    assert delegated_check["detail"] == (
        "providers=2, ready=1, productized=browser,browser_action,coding, "
        "planned=research, hosted_screenshots=local_only:filesystem"
    )
    governance_check = next(
        item for item in payload["checks"] if item["name"] == "governance_proof"
    )
    assert governance_check["detail"] == "memory_user=user-1, commands=3"
    assert (
        "genaug memory profile --project dayplan --user user-1 --json"
        in governance_check["next_action"]
    )
    assert (
        "genaug memory export --project dayplan --user user-1 --output genaug-memory-export.json"
    ) in governance_check["next_action"]
    assert (
        "genaug approvals list --project dayplan --status pending --json"
        in governance_check["next_action"]
    )
    assert "secret" not in result.output
    assert FakeHTTPClient.requests[2]["url"] == "http://api.test/api/v1/admin/projects"
    assert (
        FakeHTTPClient.requests[3]["url"]
        == "http://api.test/api/v1/admin/projects/proj%2F1/launch-readiness"
    )
    assert (
        FakeHTTPClient.requests[6]["url"]
        == "http://api.test/api/v1/admin/projects/proj%2F1/coding-providers"
    )
    assert (
        FakeHTTPClient.requests[7]["url"]
        == "http://api.test/api/v1/admin/projects/proj%2F1/approvals"
    )
    assert FakeHTTPClient.requests[8]["url"] == "http://api.test/v1/agent-runs"
    assert FakeHTTPClient.requests[8]["params"] == {"limit": 1}
    assert FakeHTTPClient.requests[8]["headers"] == {
        "Authorization": "Bearer secret",
        "X-Project-ID": "proj/1",
    }
    assert FakeHTTPClient.requests[9]["url"] == "http://api.test/v1/agent-runs/run%2F1"
    assert FakeHTTPClient.requests[9]["headers"] == {
        "Authorization": "Bearer secret",
        "X-Project-ID": "proj/1",
    }
    assert (
        FakeHTTPClient.requests[10]["url"] == "http://api.test/api/v1/agent/memory/profile/user-1"
    )


def test_doctor_warns_when_project_has_no_run_timeline_yet(tmp_path: Path) -> None:
    """Project doctor should distinguish no traffic from broken run timelines."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
        json_response({"items": [{"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}]}),
        json_response(project_launch_readiness_response(verdict="blocked", required_open=1)),
        json_response({"tool_discovery": {"mode": "auto"}}),
        json_response({"items": []}),
        json_response({"items": []}),
        json_response({"items": []}),
        json_response({"items": []}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "doctor",
            "--project",
            "dayplan",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verdict"] == "WARN"
    launch_check = next(item for item in payload["checks"] if item["name"] == "launch_readiness")
    assert launch_check["status"] == "WARN"
    assert "verdict=blocked" in launch_check["detail"]
    run_check = next(item for item in payload["checks"] if item["name"] == "run_timeline")
    assert run_check["status"] == "WARN"
    assert run_check["detail"] == "recent_runs=0"
    assert "create the first timeline" in run_check["next_action"]
    governance_check = next(
        item for item in payload["checks"] if item["name"] == "governance_proof"
    )
    assert governance_check["detail"] == "memory_user=<app-user-id>, commands=3"
    assert (
        "genaug memory profile --project dayplan --user <app-user-id> --json"
        in governance_check["next_action"]
    )
    assert len(FakeHTTPClient.requests) == 9


def test_doctor_can_fail_on_blocked_launch_readiness(tmp_path: Path) -> None:
    """Doctor should support strict launch-gate behavior for CI."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
        json_response({"items": [{"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}]}),
        json_response(project_launch_readiness_response(verdict="blocked", required_open=1)),
        json_response({"tool_discovery": {"mode": "auto"}}),
        json_response({"items": []}),
        json_response({"items": []}),
        json_response({"items": []}),
        json_response({"items": []}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "doctor",
            "--project",
            "dayplan",
            "--fail-on-launch-blocked",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["verdict"] == "FAIL"
    launch_check = next(item for item in payload["checks"] if item["name"] == "launch_readiness")
    assert launch_check["status"] == "FAIL"
    assert payload["launch_readiness"]["verdict"] == "blocked"


def test_doctor_accepts_browser_artifact_production_proof(tmp_path: Path) -> None:
    """Doctor should compose retained browser artifact proof with platform checks."""
    config_path = write_config(tmp_path)
    proof_path = tmp_path / "production-proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "schema_version": "2026-06-14.browser-artifact-production-proof.v1",
                "generated_at": "2026-06-14T12:00:00+00:00",
                "verdict": "PASS",
                "checks": [
                    {"name": "bootstrap_schema", "status": "PASS"},
                    {"name": "hosted_screenshot_readiness", "status": "PASS"},
                ],
                "next_actions": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "doctor",
            "--browser-artifact-production-proof",
            str(proof_path),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    proof_check = next(
        item for item in payload["checks"] if item["name"] == "browser_artifact_production_proof"
    )
    assert proof_check == {
        "name": "browser_artifact_production_proof",
        "status": "PASS",
        "detail": "verdict=PASS, checks=2/2",
        "next_action": "No action needed.",
    }
    assert payload["browser_artifact_production_proof"] == {
        "schema_version": "2026-06-14.browser-artifact-production-proof.v1",
        "path": str(proof_path),
        "generated_at": "2026-06-14T12:00:00+00:00",
        "verdict": "PASS",
        "checks": {"passed": 2, "total": 2},
        "next_actions": [],
    }


def test_doctor_fails_on_browser_artifact_production_proof_failure(tmp_path: Path) -> None:
    """Doctor should fail closed when an explicitly supplied proof artifact is failing."""
    config_path = write_config(tmp_path)
    proof_path = tmp_path / "production-proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "schema_version": "2026-06-14.browser-artifact-production-proof.v1",
                "generated_at": "2026-06-14T12:00:00+00:00",
                "verdict": "FAIL",
                "checks": [
                    {"name": "bootstrap_schema", "status": "PASS"},
                    {"name": "hosted_screenshot_readiness", "status": "FAIL"},
                ],
                "next_actions": ["Deploy the GCS browser artifact settings."],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "doctor",
            "--browser-artifact-production-proof",
            str(proof_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    proof_check = next(
        item for item in payload["checks"] if item["name"] == "browser_artifact_production_proof"
    )
    assert proof_check["status"] == "FAIL"
    assert proof_check["detail"] == "verdict=FAIL, checks=1/2"
    assert proof_check["next_action"] == "Deploy the GCS browser artifact settings."


def test_doctor_browser_artifact_proof_schema_hint_uses_cli_command(
    tmp_path: Path,
) -> None:
    """Doctor should send stale browser proof artifacts to the productized CLI flow."""
    config_path = write_config(tmp_path)
    proof_path = tmp_path / "production-proof.json"
    proof_path.write_text('{"schema_version": "old"}\n', encoding="utf-8")
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"auth_method": "api_key", "project_ids": ["proj/1"]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "doctor",
            "--browser-artifact-production-proof",
            str(proof_path),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    proof_check = next(
        item for item in payload["checks"] if item["name"] == "browser_artifact_production_proof"
    )
    assert proof_check["status"] == "FAIL"
    assert (
        proof_check["next_action"]
        == "Regenerate the proof with genaug projects browser-artifacts prove-production."
    )


def test_doctor_fails_without_api_key(tmp_path: Path) -> None:
    """Doctor should make missing auth obvious before a developer starts integrating."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("base_url: http://api.test\n", encoding="utf-8")
    FakeHTTPClient.queue = [json_response({"status": "ok"})]

    result = CliRunner().invoke(app, ["--config", str(config_path), "doctor", "--json"])

    assert result.exit_code == 1
    assert '"verdict": "FAIL"' in result.output
    assert "Run genaug auth login" in result.output
    assert len(FakeHTTPClient.requests) == 1


def test_verify_runs_project_acceptance_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify should stitch platform, project, test, logs, usage, and observability checks."""
    config_path = write_config(tmp_path)
    monkeypatch.setattr("platform_cli.commands.verify.uuid.uuid4", lambda: _FixedVerifyUUID())
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"items": [project]}),
        json_response({"auth_method": "api_key", "project_id": None, "project_ids": []}),
        json_response(
            {
                "items": [
                    {
                        "id": "key/1",
                        "name": "Production backend",
                        "masked_key": "gaadmlive...cret",
                        "project_id": "proj/1",
                        "scopes": ["admin"],
                    }
                ]
            }
        ),
        json_response({"items": [{"id": "web_search", "risk_level": "low"}]}),
        json_response(runtime_policy_response()),
        json_response({"content": "# DayPlan\n\nHelpful."}),
        json_response(skill_list_response()),
        json_response(
            {
                "response_text": "General Augment project works.",
                "metadata": {"agent_run_id": "run/1"},
            }
        ),
        json_response(
            {
                "id": "run/1",
                "status": "completed",
                "run_events": [{"event_type": "agent_run.completed"}],
            }
        ),
        json_response({"items": [{"role": "assistant", "content": "ok"}]}),
        json_response(
            {
                "totals": {"agent_turns_count": 1, "total_cost_usd": 0.01},
                "limits": {
                    "agent_turns_per_day": 100,
                    "tokens_per_day": 100_000,
                    "over_limit": False,
                },
                "days": [],
            }
        ),
        json_response({"traces": [{"trace_id": "trace_1"}], "metrics": {}}),
        json_response({"channels": [{"channel": "sms", "status": "configured"}]}),
        json_response(
            {
                "memory_id": "mem/1",
                "content": "CLI verification user prefers concise onboarding notes.",
            }
        ),
        json_response({"facts": [{"memory_id": "mem/1", "content": "concise onboarding notes"}]}),
        json_response({"total_facts": 1, "recent_facts": []}),
        json_response(
            {
                "id": "resp_memory_recall",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "genaug-memory-verify-abcdef123456",
                            }
                        ],
                    }
                ],
            }
        ),
        json_response({"deleted_count": 1, "deleted_ids": ["mem/1"], "status": "deleted"}),
        json_response({"items": [{"tool_id": "memory_delete", "success": True}]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "verify",
            "--project",
            "dayplan",
            "--dashboard-url",
            "https://app.test",
        ],
    )

    assert result.exit_code == 0
    assert "Project Verify: dayplan" in result.output
    assert "project_api_key" in result.output
    assert "project_key_execution" in result.output
    assert "SKIP" in result.output
    assert "soul_visible" in result.output
    assert "skills_visible" in result.output
    assert "run_timeline_inspect" in result.output
    assert "usage_limits" in result.output
    assert "Dashboard Follow-up" in result.output
    assert "https://app.test/dashboard/projects/proj%2F1/tools" in result.output
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/health/ready"
    assert FakeHTTPClient.requests[2]["url"].endswith("/api/v1/admin/me")
    assert FakeHTTPClient.requests[3]["url"].endswith("/api/v1/admin/keys")
    assert FakeHTTPClient.requests[5]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/runtime-policy"
    )
    assert FakeHTTPClient.requests[6]["url"].endswith("/api/v1/admin/projects/proj%2F1/soul")
    assert FakeHTTPClient.requests[7]["url"].endswith("/api/v1/admin/projects/proj%2F1/skills")
    assert FakeHTTPClient.requests[7]["params"] == {"limit": 100}
    assert FakeHTTPClient.requests[8]["method"] == "POST"
    assert FakeHTTPClient.requests[8]["url"].endswith("/api/v1/admin/projects/proj%2F1/test")
    assert FakeHTTPClient.requests[9]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/runs/run%2F1"
    )
    assert FakeHTTPClient.requests[12]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/observability"
    )
    assert FakeHTTPClient.requests[13]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/channels/status"
    )
    assert FakeHTTPClient.requests[14]["url"].endswith("/api/v1/agent/memory/store")
    assert FakeHTTPClient.requests[14]["headers"] == {
        "Authorization": "Bearer secret",
        "X-Project-ID": "proj/1",
    }
    assert FakeHTTPClient.requests[14]["json"]["source"] == "genaug-cli-verify"
    assert FakeHTTPClient.requests[14]["json"]["metadata"]["scenario"] == "project-verify"
    assert FakeHTTPClient.requests[14]["json"]["metadata"]["verification_id"]
    assert FakeHTTPClient.requests[14]["json"]["idempotency_key"].startswith(
        "genaug-verify-proj/1-genaug-verify-user-"
    )
    assert (
        FakeHTTPClient.requests[14]["json"]["idempotency_key"]
        != "genaug-verify-proj/1-genaug-verify-user"
    )
    assert FakeHTTPClient.requests[15]["url"].endswith("/api/v1/agent/memory/search")
    assert FakeHTTPClient.requests[17]["method"] == "POST"
    assert FakeHTTPClient.requests[17]["url"] == "http://api.test/v1/responses"
    assert FakeHTTPClient.requests[17]["headers"] == {
        "Authorization": "Bearer secret",
        "X-Project-ID": "proj/1",
    }
    assert FakeHTTPClient.requests[17]["json"]["metadata"]["feature"] == ("memory_response_recall")
    assert FakeHTTPClient.requests[17]["json"]["input"] == (
        "What is my stored onboarding note code? Reply with only the code."
    )
    assert "abcdef123456" not in FakeHTTPClient.requests[17]["json"]["input"]
    assert FakeHTTPClient.requests[18]["method"] == "DELETE"
    assert FakeHTTPClient.requests[18]["url"].endswith("/api/v1/agent/memory/mem%2F1")
    assert FakeHTTPClient.requests[18]["params"] == {"user_id": "genaug-verify-user"}
    assert FakeHTTPClient.requests[19]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/audit/tool-calls"
    )


def test_verify_exercises_responses_when_cli_key_is_project_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify should prove the configured project key can call `/v1/responses`."""
    config_path = write_config(tmp_path)
    monkeypatch.setattr("platform_cli.commands.verify.uuid.uuid4", lambda: _FixedVerifyUUID())
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"items": [project]}),
        json_response({"auth_method": "api_key", "project_id": "proj/1", "project_ids": []}),
        json_response(
            {
                "items": [
                    {
                        "id": "key/1",
                        "name": "Production backend",
                        "masked_key": "gaadmlive...cret",
                        "project_id": "proj/1",
                        "scopes": ["admin"],
                    }
                ]
            }
        ),
        json_response(
            {
                "id": "resp_verify",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "verified"}],
                    }
                ],
            }
        ),
        json_response({"items": [{"id": "web_search", "risk_level": "low"}]}),
        json_response(runtime_policy_response()),
        json_response({"content": "# DayPlan\n\nHelpful."}),
        json_response(skill_list_response()),
        json_response(
            {
                "response_text": "General Augment project works.",
                "metadata": {"agent_run_id": "run/1"},
            }
        ),
        json_response(
            {
                "id": "run/1",
                "status": "completed",
                "run_events": [{"event_type": "agent_run.completed"}],
            }
        ),
        json_response({"items": [{"role": "assistant", "content": "ok"}]}),
        json_response(
            {
                "totals": {"agent_turns_count": 1, "total_cost_usd": 0.01},
                "limits": {
                    "agent_turns_per_day": 100,
                    "tokens_per_day": 100_000,
                    "over_limit": False,
                },
                "days": [],
            }
        ),
        json_response({"traces": [{"trace_id": "trace_1"}], "metrics": {}}),
        json_response({"channels": [{"channel": "sms", "status": "configured"}]}),
        json_response({"memory_id": "mem/1", "content": "ok"}),
        json_response({"facts": [{"memory_id": "mem/1", "content": "concise onboarding notes"}]}),
        json_response({"total_facts": 1, "recent_facts": []}),
        json_response(
            {
                "id": "resp_memory_recall",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "genaug-memory-verify-abcdef123456",
                            }
                        ],
                    }
                ],
            }
        ),
        json_response({"deleted_count": 1, "deleted_ids": ["mem/1"], "status": "deleted"}),
        json_response({"items": [{"tool_id": "memory_delete", "success": True}]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "verify",
            "--project",
            "dayplan",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["auth"]["project_id"] == "proj/1"
    execution_check = next(
        item for item in payload["checks"] if item["name"] == "project_key_execution"
    )
    assert execution_check == {
        "name": "project_key_execution",
        "status": "PASS",
        "detail": "resp_verify",
    }
    readiness = payload["readiness_checklist"]
    assert readiness["version"] == "general-augment-readiness/v1"
    assert {item["key"] for item in readiness["items"]} >= {
        "project_created",
        "project_key_created",
        "project_key_execution",
        "first_response_passed",
        "run_timeline_visible",
        "runtime_policy_visible",
        "tenant_behavior_configured",
        "memory_tested",
        "memory_response_recall",
        "trace_visible",
        "usage_limits_visible",
        "channel_status_known",
        "billing_state_known",
    }
    assert (
        next(item for item in readiness["items"] if item["key"] == "project_key_execution")[
            "status"
        ]
        == "PASS"
    )
    assert (
        next(item for item in readiness["items"] if item["key"] == "runtime_policy_visible")[
            "status"
        ]
        == "PASS"
    )
    assert (
        next(item for item in readiness["items"] if item["key"] == "run_timeline_visible")["status"]
        == "PASS"
    )
    routing_check = next(
        item for item in payload["checks"] if item["name"] == "runtime_policy_model_routing"
    )
    assert routing_check["status"] == "PASS"
    assert (
        next(item for item in payload["checks"] if item["name"] == "soul_visible")["status"]
        == "PASS"
    )
    assert (
        next(item for item in payload["checks"] if item["name"] == "skills_visible")["status"]
        == "PASS"
    )
    assert (
        next(item for item in payload["checks"] if item["name"] == "memory_response_recall")[
            "status"
        ]
        == "PASS"
    )
    assert payload["runtime_policy"]["model_routing"]["channel_parity"] is True
    assert payload["runtime_policy"]["model_routing"]["tiers"] == {
        "simple": "google/gemini-2.5-flash-lite",
        "balanced": "google/gemini-2.5-flash",
        "complex": "google/gemini-2.5-pro",
    }
    assert FakeHTTPClient.requests[4]["method"] == "POST"
    assert FakeHTTPClient.requests[4]["url"] == "http://api.test/v1/responses"
    assert FakeHTTPClient.requests[4]["headers"] == {"Authorization": "Bearer secret"}
    assert FakeHTTPClient.requests[4]["json"] == {
        "model": "balanced",
        "user": "genaug-verify-user",
        "input": "Reply with one short sentence confirming this General Augment project works.",
        "metadata": {
            "source": "genaug-cli-verify",
            "feature": "project_key_execution",
        },
    }


def test_verify_fails_when_agent_test_fails(tmp_path: Path) -> None:
    """Verify should exit nonzero when a critical project check fails."""
    config_path = write_config(tmp_path)
    project = {"id": "p1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"items": [project]}),
        json_response({"auth_method": "api_key", "project_id": None, "project_ids": []}),
        json_response({"items": [{"id": "key_1", "name": "Backend", "project_id": "p1"}]}),
        json_response({"items": []}),
        json_response(runtime_policy_response()),
        json_response({"content": "# DayPlan\n\nHelpful."}),
        json_response(skill_list_response()),
        json_response(
            {"error": "provider_missing", "details": "Active LLM provider not configured"}
        ),
        json_response({"items": []}),
        json_response({"totals": {}, "limits": {}, "days": []}),
        json_response({"traces": [], "metrics": {}}),
        json_response({"channels": []}),
        json_response({"memory_id": "mem_1", "content": "ok"}),
        json_response({"facts": [{"memory_id": "mem_1"}]}),
        json_response({"total_facts": 1, "recent_facts": []}),
        json_response({"deleted_count": 1, "deleted_ids": ["mem_1"], "status": "deleted"}),
        json_response({"items": []}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "verify", "--project", "dayplan", "--json"],
    )

    assert result.exit_code != 0
    assert "Project verification failed: agent_test" in result.output
    assert '"verdict": "FAIL"' in result.output


def test_verify_fails_when_runtime_policy_model_routing_is_incomplete(tmp_path: Path) -> None:
    """Verify should fail if tenant-visible model routing is not launch-ready."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    bad_policy = runtime_policy_response()
    bad_policy["model_routing"]["channel_parity"] = False
    del bad_policy["model_routing"]["tiers"]["complex"]
    FakeHTTPClient.queue = [
        json_response({"status": "ok", "db": "connected", "redis": "connected"}),
        json_response({"items": [project]}),
        json_response({"auth_method": "api_key", "project_id": None, "project_ids": []}),
        json_response({"items": [{"id": "key_1", "name": "Backend", "project_id": "proj/1"}]}),
        json_response({"items": []}),
        json_response(bad_policy),
        json_response({"content": "# DayPlan\n\nHelpful."}),
        json_response(skill_list_response()),
        json_response(
            {
                "response_text": "General Augment project works.",
                "metadata": {"agent_run_id": "run/1"},
            }
        ),
        json_response(
            {
                "id": "run/1",
                "status": "completed",
                "run_events": [{"event_type": "agent_run.completed"}],
            }
        ),
        json_response({"items": [{"role": "assistant", "content": "ok"}]}),
        json_response({"totals": {}, "limits": {}, "days": []}),
        json_response({"traces": [], "metrics": {}}),
        json_response({"channels": []}),
        json_response({"memory_id": "mem_1", "content": "ok"}),
        json_response({"facts": [{"memory_id": "mem_1"}]}),
        json_response({"total_facts": 1, "recent_facts": []}),
        json_response({"deleted_count": 1, "deleted_ids": ["mem_1"], "status": "deleted"}),
        json_response({"items": []}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "verify", "--project", "dayplan", "--json"],
    )

    assert result.exit_code != 0
    assert "Project verification failed: runtime_policy_model_routing" in result.output
    payload = first_json_object(result.output)
    routing_check = next(
        item for item in payload["checks"] if item["name"] == "runtime_policy_model_routing"
    )
    assert routing_check["status"] == "FAIL"
    assert "complex=missing" in routing_check["detail"]


@pytest.mark.parametrize(
    ("status_code", "message"),
    [
        (401, "Run genaug auth login"),
        (404, "not found"),
        (500, "Retry shortly"),
    ],
)
def write_config(tmp_path: Path) -> Path:
    """Write a CLI config fixture."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("base_url: http://api.test\napi_key: secret\n", encoding="utf-8")
    return config_path


def coding_run_detail_payload() -> dict[str, Any]:
    """Return a delegated coding detail fixture."""
    run = {
        "id": "coding/run/1",
        "project_id": "proj/1",
        "agent_run_id": "agent/run/1",
        "provider": "anthropic-managed-agents",
        "job_type": "website_builder",
        "state": "preview_ready",
        "review_status": "passed",
        "latest_progress_summary": "Provider generated the preview.",
        "created_at": "2026-06-13T10:00:00Z",
        "updated_at": "2026-06-13T10:01:00Z",
        "completed_at": "2026-06-13T10:01:00Z",
        "error_message": None,
        "build_packet_id": "packet-1",
        "artifacts": [
            {
                "id": "artifact-1",
                "kind": "site_bundle",
                "filename": "site.html",
                "safe_summary": "Generated dentist website preview bundle",
            }
        ],
        "events": [],
    }
    return {
        "run": run,
        "parent_run": {
            "id": "agent/run/1",
            "project_id": "proj/1",
            "user_id": "0f7b181b-7bb6-4bc2-a34e-781f6c5b7e61",
            "session_id": "705ecffa-1ff9-4ae7-91dd-1c71ae631d1e",
            "surface": "delegated_coding",
            "status": "completed",
            "created_at": "2026-06-13T10:00:00Z",
            "updated_at": "2026-06-13T10:01:00Z",
        },
        "build_packet": {"id": "packet-1", "status": "preview_ready", "assets": []},
        "artifacts": run["artifacts"],
        "supervisor_reviews": [],
        "iteration_history": [],
        "platform_events": [],
    }


def research_run_detail_payload() -> dict[str, Any]:
    """Return a governed research detail fixture."""
    run = {
        "id": "research/run/1",
        "project_id": "proj/1",
        "agent_run_id": "agent/run/1",
        "provider": "perplexity",
        "capability_id": "web_search",
        "specialist_type": "research",
        "state": "handoff_ready",
        "review_status": "passed",
        "latest_progress_summary": "Research handoff is ready.",
        "created_at": "2026-06-13T10:00:00Z",
        "updated_at": "2026-06-13T10:01:00Z",
        "completed_at": "2026-06-13T10:01:00Z",
        "error_message": None,
        "build_packet_id": "packet-1",
        "artifacts": [
            {
                "id": "artifact-1",
                "kind": "review_report",
                "safe_summary": "HarnessAgent normalizes managed-agent streams.",
                "metadata": {
                    "answer_preview": "HarnessAgent normalizes managed-agent streams.",
                    "citations": [
                        "https://vercel.com/changelog/program-agent-harnesses-with-ai-sdk"
                    ],
                },
            }
        ],
        "events": [
            {
                "id": "event-1",
                "sequence": 1,
                "event_type": "research_run.completed",
                "provider_event_type": "search_completed",
                "summary": "Research handoff is ready.",
                "safe_payload": {"citation_count": 1},
            }
        ],
    }
    return {
        "run": run,
        "parent_run": {
            "id": "agent/run/1",
            "project_id": "proj/1",
            "user_id": "0f7b181b-7bb6-4bc2-a34e-781f6c5b7e61",
            "session_id": "705ecffa-1ff9-4ae7-91dd-1c71ae631d1e",
            "surface": "delegated_research",
            "status": "completed",
            "created_at": "2026-06-13T10:00:00Z",
            "updated_at": "2026-06-13T10:01:00Z",
        },
        "build_packet": {"id": "packet-1", "status": "active", "assets": []},
        "artifacts": run["artifacts"],
        "events": run["events"],
        "platform_events": [],
    }


def browser_run_detail_payload() -> dict[str, Any]:
    """Return a governed browser detail fixture."""
    run = {
        "id": "browser/run/1",
        "project_id": "proj/1",
        "agent_run_id": "agent/run/1",
        "provider": "browserbase",
        "capability_id": "browserbase_browser",
        "specialist_type": "browser",
        "state": "handoff_ready",
        "review_status": "passed",
        "external_environment_id": "bb_proj_123",
        "external_session_id": "bb_sess_123",
        "latest_progress_summary": "Browserbase live view is ready.",
        "created_at": "2026-06-13T10:00:00Z",
        "updated_at": "2026-06-13T10:01:00Z",
        "completed_at": "2026-06-13T10:01:00Z",
        "error_message": None,
        "build_packet_id": "packet-1",
        "artifacts": [
            {
                "id": "artifact-1",
                "kind": "preview_url",
                "signed_url": "https://debug.browserbase.test/session/fullscreen",
                "safe_summary": "Browserbase live view is ready.",
                "metadata": {
                    "schema_version": "general-augment-browser-live-view/v1",
                    "debug": {"ws_url_present": True, "page_count": 1},
                },
            }
        ],
        "events": [
            {
                "id": "event-1",
                "sequence": 1,
                "event_type": "browser_run.live_view_ready",
                "provider_event_type": "debug_links_ready",
                "summary": "Browserbase live view is ready.",
                "safe_payload": {"artifact_id": "artifact-1"},
            }
        ],
    }
    return {
        "run": run,
        "parent_run": {
            "id": "agent/run/1",
            "project_id": "proj/1",
            "user_id": "0f7b181b-7bb6-4bc2-a34e-781f6c5b7e61",
            "session_id": "705ecffa-1ff9-4ae7-91dd-1c71ae631d1e",
            "surface": "delegated_browser",
            "status": "completed",
            "created_at": "2026-06-13T10:00:00Z",
            "updated_at": "2026-06-13T10:01:00Z",
        },
        "build_packet": {"id": "packet-1", "status": "active", "assets": []},
        "artifacts": run["artifacts"],
        "events": run["events"],
        "platform_events": [],
    }


def write_agent_config(tmp_path: Path, *, api_version: str = "genaug/v1") -> Path:
    """Write a local agent config fixture."""
    (tmp_path / "SOUL.md").write_text("# DayPlan\n\nHelpful.\n", encoding="utf-8")
    (tmp_path / "skills").mkdir(exist_ok=True)
    (tmp_path / "skills" / "README.md").write_text("skills\n", encoding="utf-8")
    config: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": "Agent",
        "metadata": {"name": "dayplan", "display_name": "DayPlan"},
        "personality": {"soul_file": "./SOUL.md"},
        "model": {
            "simple": "google/gemini-2.5-flash-lite",
            "balanced": "google/gemini-2.5-flash",
            "complex": "google/gemini-2.5-pro",
        },
        "tools": {"builtin": ["web_search"], "mcp": []},
        "skills": {"directory": "./skills/"},
        "channels": {"whatsapp": {}, "sms": {}, "telegram": {}},
        "behavior": {"max_tool_calls_per_turn": 10},
    }
    path = tmp_path / "genaug-agent.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def runtime_policy_response() -> dict[str, Any]:
    """Return a hosted-compatible runtime policy fixture."""

    return {
        "project_id": "proj/1",
        "model_routing": {
            "mode": "tiered_complexity",
            "tiers": {
                "simple": "google/gemini-2.5-flash-lite",
                "balanced": "google/gemini-2.5-flash",
                "complex": "google/gemini-2.5-pro",
            },
            "default_tier": "balanced",
            "auto_routes_by": [
                "prompt complexity",
                "enabled tools",
                "conversation history",
                "reasoning_effort override when supplied",
            ],
            "channel_parity": True,
        },
        "tool_discovery": {"mode": "auto"},
        "hermes_exposure": {"uses_dynamic_discovery_by_default": True},
        "platform_tools": {"enabled_tool_ids": ["web_search"], "unknown_tool_ids": []},
        "mcp": {"enabled_tool_ids": []},
        "skills": {"names": ["Support Triage"]},
    }


def project_launch_readiness_response(
    *,
    verdict: str = "ready",
    required_open: int = 0,
) -> dict[str, Any]:
    """Return a hosted-compatible project launch-readiness fixture."""

    return {
        "schema_version": "genaug.project_launch_readiness.v1",
        "project_id": "proj/1",
        "project_slug": "dayplan",
        "project_name": "DayPlan",
        "generated_at": "2026-06-14T00:00:00Z",
        "verdict": verdict,
        "summary": {
            "total": 2,
            "required_total": 1,
            "required_ready": 1 - required_open,
            "required_open": required_open,
            "recommended_total": 1,
            "recommended_ready": 0,
            "recommended_open": 1,
        },
        "items": [
            {
                "key": "run_timeline_visible",
                "label": "Run timeline visible",
                "description": "A retained run has canonical timeline events.",
                "required": True,
                "status": "open" if required_open else "ready",
                "detail": "No retained run timeline events are visible yet.",
                "evidence": {"agent_run_event_count": 0},
                "next_actions": [
                    {
                        "label": "Run hosted verify",
                        "command": "genaug verify --project dayplan --json",
                        "href": "/dashboard/projects/proj/1/runs",
                    }
                ],
            },
            {
                "key": "platform_webhook_configured",
                "label": "Platform webhook configured",
                "description": "Async lifecycle events have a delivery URL.",
                "required": False,
                "status": "open",
                "detail": "No platform lifecycle webhook URL is configured.",
                "evidence": {"platform_webhook_configured": False},
                "next_actions": [],
            },
        ],
        "next_actions": [
            {
                "label": "Run hosted verify",
                "command": "genaug verify --project dayplan --json",
                "href": "/dashboard/projects/proj/1/runs",
            }
        ]
        if required_open
        else [],
    }


def skill_list_response(*names: str) -> dict[str, Any]:
    """Return a hosted-compatible tenant skill list fixture."""

    skill_names = names or ("Support Triage",)
    return {
        "items": [
            {
                "name": name,
                "description": "Route support work.",
                "version": "1.0",
                "tags": ["support"],
                "tools": ["web_search"],
                "path": f"skills/{name.lower().replace(' ', '-')}/SKILL.md",
            }
            for name in skill_names
        ]
    }


def skill_markdown(name: str) -> str:
    """Return a small SKILL.md fixture."""

    return f"""---
name: {name}
description: Route support work.
version: "1.0"
tags:
  - support
tools:
  - web_search
---
# {name}

Route support work.
"""


def first_json_object(output: str) -> dict[str, Any]:
    """Return the first JSON object printed before any Rich error panel."""

    payload, _ = json.JSONDecoder().raw_decode(output)
    assert isinstance(payload, dict)
    return payload


def queue_project_verification_success() -> None:
    """Queue the HTTP responses used by verify and onboarding verify."""

    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response(
            {
                "status": "ok",
                "db": "connected",
                "redis": "connected",
                "version": "0.1.0",
                "build_sha": "abc123",
            }
        ),
        json_response({"items": [project]}),
        json_response({"auth_method": "api_key", "project_id": None, "project_ids": []}),
        json_response(
            {
                "items": [
                    {
                        "id": "key/1",
                        "name": "Production backend",
                        "masked_key": "gaadmlive...cret",
                        "project_id": "proj/1",
                        "scopes": ["admin"],
                    }
                ]
            }
        ),
        json_response({"items": [{"id": "web_search", "risk_level": "low"}]}),
        json_response(runtime_policy_response()),
        json_response({"content": "# DayPlan\n\nHelpful."}),
        json_response(skill_list_response()),
        json_response(
            {
                "response_text": "General Augment project works.",
                "metadata": {"agent_run_id": "run/1"},
            }
        ),
        json_response(
            {
                "id": "run/1",
                "status": "completed",
                "run_events": [{"event_type": "agent_run.completed"}],
            }
        ),
        json_response({"items": [{"role": "assistant", "content": "ok"}]}),
        json_response(
            {
                "totals": {"agent_turns_count": 1, "total_cost_usd": 0.01},
                "limits": {
                    "agent_turns_per_day": 100,
                    "tokens_per_day": 100_000,
                    "over_limit": False,
                },
                "days": [],
            }
        ),
        json_response({"traces": [{"trace_id": "trace_1"}], "metrics": {}}),
        json_response({"channels": [{"channel": "sms", "status": "configured"}]}),
        json_response(
            {
                "memory_id": "mem/1",
                "content": "CLI verification user prefers concise onboarding notes.",
            }
        ),
        json_response({"facts": [{"memory_id": "mem/1", "content": "concise onboarding notes"}]}),
        json_response({"total_facts": 1, "recent_facts": []}),
        json_response(
            {
                "id": "resp_memory_recall",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "memory recall response completed",
                            }
                        ],
                    }
                ],
            }
        ),
        json_response({"deleted_count": 1, "deleted_ids": ["mem/1"], "status": "deleted"}),
        json_response({"items": [{"tool_id": "memory_delete", "success": True}]}),
    ]


def json_response(
    payload: Any,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Return a JSON response."""
    return httpx.Response(status_code, json=payload, headers=headers)


def text_response(payload: str, status_code: int = 200) -> httpx.Response:
    """Return a text response."""
    return httpx.Response(status_code, text=payload)
