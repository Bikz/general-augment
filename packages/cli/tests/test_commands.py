"""Command tests for the standalone CLI package."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from platform_cli.main import app

ROOT = Path(__file__).resolve().parents[3]
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PLACEHOLDER_MCP_URL = (
    "https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}"
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

    def close(self) -> None:
        """Close fake client."""


def plain_cli_output(result: Any) -> str:
    """Return CLI output without ANSI or Rich box wrapping artifacts."""
    output = ANSI_RE.sub("", str(result.output))
    output = output.translate(RICH_BOX_TRANSLATION)
    return " ".join(output.split())


@pytest.fixture(autouse=True)
def fake_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch HTTP calls for every command test."""
    FakeHTTPClient.requests = []
    FakeHTTPClient.queue = []
    monkeypatch.setattr(httpx, "Client", FakeHTTPClient)


def test_auth_login_logout_whoami(tmp_path: Path) -> None:
    """Auth commands should write config, call /me, and clear config."""
    config_path = tmp_path / "config.yaml"
    runner = CliRunner()
    FakeHTTPClient.queue = [
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
    logout = runner.invoke(app, ["--config", str(config_path), "auth", "logout"])

    assert login.exit_code == 0
    assert "Verified API access" in login.output
    assert "projects: p1" in login.output
    assert whoami.exit_code == 0
    assert "api_key" in whoami.output
    assert "Project IDs: p1" in whoami.output
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/admin/me"
    assert FakeHTTPClient.requests[1]["url"] == "http://api.test/api/v1/admin/me"
    assert logout.exit_code == 0
    assert not config_path.exists()


def test_auth_login_browser_flow_stores_installer_session_without_printing_tokens(
    tmp_path: Path,
) -> None:
    """Browser login should exchange an installer code and keep tokens out of output."""
    config_path = tmp_path / "config.yaml"
    runner = CliRunner()
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
                "expires_at": "2026-05-23T20:30:00Z",
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
    assert FakeHTTPClient.requests[1]["url"] == "http://api.test/api/v1/installer/auth/token"
    assert FakeHTTPClient.requests[2]["headers"] == {
        "Authorization": "Bearer gainst_access_secret"
    }
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["api_key"] is None
    assert config["metadata"]["installer"]["access_token"] == "gainst_access_secret"
    assert config["metadata"]["installer"]["refresh_token"] == "garefr_refresh_secret"


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
    assert FakeHTTPClient.requests[0]["headers"] == {
        "Authorization": "Bearer gainst_access_secret"
    }
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["json"]["slug"] == "demo-app"
    assert FakeHTTPClient.requests[2]["url"] == (
        "http://api.test/api/v1/installer/projects/proj_1/runtime-keys"
    )
    persisted_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted_config["active_project"] == "proj_1"
    # The runtime key is persisted into config so a brand-new user gets a working
    # auth login -> setup --bootstrap -> smoke chain with no manual export.
    assert persisted_config["api_key"] == "ga_runtime_secret_once"
    assert config_path.stat().st_mode & 0o777 == 0o600
    # The redacted setup artifact must never contain the raw runtime secret.
    assert "ga_runtime_secret_once" not in artifact_path.read_text(encoding="utf-8")
    assert "ga_runtime_secret_once" not in result.output


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
    assert persisted_config["api_key"] == "ga_runtime_secret_once"


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
        json_response(
            {"items": [{"id": "proj_1", "name": "Demo App", "slug": "demo-app"}]}
        ),
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
    assert FakeHTTPClient.requests[1]["headers"] == {
        "Authorization": "Bearer gainst_access_secret"
    }
    assert FakeHTTPClient.requests[1]["json"]["api_key"] == "bb_secret_raw"
    assert FakeHTTPClient.requests[2]["method"] == "POST"
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/installer/projects/proj_1/capability-providers/browserbase/health-check"
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
        json_response(
            {"items": [{"id": "proj_1", "name": "Demo App", "slug": "demo-app"}]}
        ),
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
    # The installer slug is resolved to its UUID before the typed installer route.
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/installer/projects"
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/installer/projects/proj_1/skills"
    )
    assert "Build safe website previews" in FakeHTTPClient.requests[1]["json"]["content"]
    assert FakeHTTPClient.requests[1]["headers"] == {
        "Authorization": "Bearer gainst_access_secret"
    }
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
    assert FakeHTTPClient.requests[0]["headers"] == {
        "Authorization": "Bearer gainst_access_secret"
    }
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

    assert scripts["genaug"] == "platform_cli.main:app"
    assert "general_augment" not in scripts


def test_version_flag_exposes_cli_package_version() -> None:
    """Automation should be able to check the installed CLI version."""
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "genaug 0.1.1" in result.output


def test_mock_command_runs_shared_local_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public mock command should delegate to the shared mock server."""
    calls: list[dict[str, object]] = []

    def fake_run_server(host: str, port: int, *, quiet: bool = False) -> None:
        calls.append({"host": host, "port": port, "quiet": quiet})

    monkeypatch.setattr("platform_cli.commands.mock.run_server", fake_run_server)

    result = CliRunner().invoke(
        app,
        ["mock", "--host", "127.0.0.1", "--port", "8787", "--quiet"],
    )

    assert result.exit_code == 0
    assert calls == [{"host": "127.0.0.1", "port": 8787, "quiet": True}]


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
    _, deleted = store.delete_memory(memory_id, user_id)

    assert profile["total_facts"] == 1
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


def test_evals_run_gate_outputs_machine_readable_verdict(tmp_path: Path) -> None:
    """Eval CLI should expose the local CI gate verdict for checked-in suites."""
    artifact_dir = tmp_path / "eval-artifacts"
    suite_path = ROOT / "tests" / "fixtures" / "agent_evals" / "platform_moat_v1.json"

    result = CliRunner().invoke(
        app,
        [
            "evals",
            "run",
            str(suite_path),
            "--artifact-dir",
            str(artifact_dir),
            "--gate",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verdict"] == "PASS"
    assert payload["suite"]["dataset"]["kind"] == "golden"
    assert Path(payload["artifact_path"]).exists()


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


def test_projects_list_and_create_use_admin_api(tmp_path: Path) -> None:
    """Project commands should call expected admin endpoints."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response(
            {"items": [{"id": "p1", "name": "DayPlan", "slug": "dayplan", "status": "active"}]}
        ),
        json_response({"id": "p2", "name": "Mysti"}),
    ]
    runner = CliRunner()

    listed = runner.invoke(app, ["--config", str(config_path), "projects", "list"])
    created = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "projects",
            "create",
            "--name",
            "Mysti",
            "--slug",
            "mysti",
        ],
    )

    assert listed.exit_code == 0
    assert created.exit_code == 0
    assert "DayPlan" in listed.output
    assert FakeHTTPClient.requests[0]["headers"] == {"X-Admin-Key": "secret"}
    assert FakeHTTPClient.requests[0]["url"].endswith("/api/v1/admin/projects")
    assert FakeHTTPClient.requests[1]["method"] == "POST"


def test_projects_usage_exposes_usage_api(tmp_path: Path) -> None:
    """Usage should have a discoverable project command with date filters."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "project_id": "proj/1",
                "totals": {
                    "agent_turns_count": 3,
                    "messages_count": 4,
                    "tool_calls_count": 2,
                    "total_cost_usd": 0.01,
                },
                "days": [
                    {
                        "date": "2026-04-24",
                        "agent_turns_count": 3,
                        "tool_calls_count": 2,
                        "total_cost_usd": 0.01,
                    }
                ],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "projects",
            "usage",
            "--project",
            "dayplan",
            "--start-date",
            "2026-04-01",
            "--end-date",
            "2026-04-24",
        ],
    )

    assert result.exit_code == 0
    assert "Agent turns" in result.output
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/usage")
    assert FakeHTTPClient.requests[1]["params"] == {
        "start_date": "2026-04-01",
        "end_date": "2026-04-24",
    }


def test_projects_runtime_policy_exposes_tenant_agent_surface(tmp_path: Path) -> None:
    """Runtime policy should expose model routing, tools, MCP, and skills."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(runtime_policy_response()),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "projects", "runtime-policy", "--project", "dayplan"],
    )

    assert result.exit_code == 0
    assert "Runtime Policy for dayplan" in result.output
    assert "google/gemini-2.5-flash-lite" in result.output
    assert "channel_parity=True" in result.output
    assert "Support Triage" in result.output
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/runtime-policy"
    )


def test_projects_runtime_policy_json_is_machine_readable(tmp_path: Path) -> None:
    """Runtime policy JSON should preserve the API response for automation."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(runtime_policy_response()),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "projects",
            "runtime-policy",
            "--project",
            "dayplan",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["model_routing"]["channel_parity"] is True
    assert payload["platform_tools"]["enabled_tool_ids"] == ["web_search"]


def test_billing_commands_create_hosted_sessions_and_list_events(tmp_path: Path) -> None:
    """Billing commands should expose hosted Checkout, Portal, and event state."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"url": "https://checkout.stripe.com/c/pay/cs_test"}),
        json_response({"items": [project]}),
        json_response({"url": "https://billing.stripe.com/session/test"}),
        json_response({"items": [project]}),
        json_response(
            {
                "items": [
                    {
                        "event_type": "invoice.payment_failed",
                        "status": "failed",
                        "target_pricing_tier": "pro",
                        "stripe_invoice_id": "in_test",
                        "amount_due_cents": 2900,
                        "processed_at": "2026-05-05T10:00:00Z",
                    }
                ]
            }
        ),
    ]
    runner = CliRunner()

    checkout = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "billing",
            "checkout",
            "--project",
            "dayplan",
            "--tier",
            "build",
        ],
    )
    portal = runner.invoke(
        app,
        ["--config", str(config_path), "billing", "portal", "--project", "dayplan"],
    )
    events = runner.invoke(
        app,
        ["--config", str(config_path), "billing", "events", "--project", "dayplan"],
    )

    assert checkout.exit_code == 0
    assert portal.exit_code == 0
    assert events.exit_code == 0
    assert "checkout.stripe.com" in checkout.output
    assert "billing.stripe.com" in portal.output
    assert "invoice.payment_failed" in events.output
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/checkout-session"
    )
    assert FakeHTTPClient.requests[1]["json"] == {"target_tier": "build"}
    assert FakeHTTPClient.requests[3]["method"] == "POST"
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/portal-session"
    )
    assert FakeHTTPClient.requests[5]["method"] == "GET"
    assert FakeHTTPClient.requests[5]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/events"
    )


def test_billing_events_json_is_machine_readable(tmp_path: Path) -> None:
    """Billing event JSON should preserve the API payload for launch evidence."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [{"event_type": "checkout.session.completed"}]}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "billing", "events", "--project", "dayplan", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["items"][0]["event_type"] == "checkout.session.completed"


def test_billing_status_shows_credit_balance_and_auto_top_up_state(tmp_path: Path) -> None:
    """Billing status should expose credit balance and funding state from admin APIs."""
    config_path = write_config(tmp_path)
    project = {
        "id": "proj/1",
        "name": "DayPlan",
        "slug": "dayplan",
        "status": "active",
        "pricing_tier": "build",
        "plan": "build",
        "rate_limits": {"funding_mode": "subscription_included"},
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "project_id": "proj/1",
                "active_balance_usd": "24.500000",
                "grants": [{"id": "grant-1"}, {"id": "grant-2"}],
                "reservations": [{"id": "reservation-1"}],
                "ledger_entries": [{"id": "ledger-1"}],
            }
        ),
        json_response(
            {
                "enabled": True,
                "threshold_usd": "5.000000",
                "top_up_amount_usd": "25.000000",
                "monthly_cap_usd": "100.000000",
                "payment_method_status": "ready",
                "charge_attempts_enabled": False,
                "last_triggered_at": "2026-05-09T12:00:00Z",
            }
        ),
        json_response(
            {"items": [{"status": "blocked", "failure_code": "charge_attempts_disabled"}]}
        ),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "billing", "status", "--project", "dayplan"],
    )

    assert result.exit_code == 0
    assert "Billing status for dayplan" in result.output
    assert "24.500000" in result.output
    assert "subscription_included" in result.output
    assert "enabled, charges disabled" in result.output
    assert "charge_attempts_disabled" in result.output
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/credits"
    )
    assert FakeHTTPClient.requests[2]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/credits/auto-top-up"
    )
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/credits/auto-top-up/attempts"
    )


def test_billing_top_up_creates_credit_checkout_session(tmp_path: Path) -> None:
    """Billing top-up should create a hosted paid-credit Checkout session."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"url": "https://checkout.stripe.com/session/top-up"}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "billing",
            "top-up",
            "--project",
            "dayplan",
            "--amount-usd",
            "25.00",
            "--save-payment-method",
        ],
    )

    assert result.exit_code == 0
    assert "top-up checkout session" in result.output
    assert "checkout.stripe.com" in result.output
    assert FakeHTTPClient.requests[1]["method"] == "POST"
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/credits/top-up-checkout-session"
    )
    assert FakeHTTPClient.requests[1]["json"] == {
        "amount_usd": "25.00",
        "save_payment_method": True,
    }


def test_billing_usage_json_exposes_usage_endpoint(tmp_path: Path) -> None:
    """Billing usage should expose machine-readable usage rollups for reconciliation."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "totals": {"agent_turns_count": 7, "total_cost_usd": 1.23},
                "days": [{"date": "2026-05-09", "agent_turns_count": 7}],
            }
        ),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "billing",
            "usage",
            "--project",
            "dayplan",
            "--start-date",
            "2026-05-01",
            "--end-date",
            "2026-05-09",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["usage"]["totals"]["agent_turns_count"] == 7
    assert payload["project"]["slug"] == "dayplan"
    assert FakeHTTPClient.requests[1]["method"] == "GET"
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/usage")
    assert FakeHTTPClient.requests[1]["params"] == {
        "start_date": "2026-05-01",
        "end_date": "2026-05-09",
    }


def test_billing_verify_json_proves_credit_gate_and_metered_platform_ledger(
    tmp_path: Path,
) -> None:
    """Billing verify should prove platform-funded work has credit guardrails."""
    config_path = write_config(tmp_path)
    project = {
        "id": "proj/1",
        "name": "DayPlan",
        "slug": "dayplan",
        "status": "active",
        "rate_limits": {
            "credit_billing_enabled": True,
            "funding_mode": "subscription_included",
        },
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "project_id": "proj/1",
                "active_balance_usd": "12.500000",
                "ledger_entries": [
                    {
                        "event_type": "agent_turn_reserved",
                        "provider_source": "platform",
                        "reservation_id": "reservation-1",
                    },
                    {
                        "event_type": "agent_turn_completed",
                        "provider_source": "platform",
                        "reservation_id": "reservation-1",
                    },
                ],
                "reservations": [{"id": "reservation-1", "status": "settled"}],
            }
        ),
        json_response({"totals": {"agent_turns_count": 3, "total_cost_usd": "0.09"}}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "billing", "verify", "--project", "dayplan", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["verdict"] == "PASS"
    assert {check["name"]: check["status"] for check in payload["checks"]} == {
        "credit_billing_enabled": "PASS",
        "funding_mode_declared": "PASS",
        "credit_balance_reachable": "PASS",
        "platform_ledger_metered": "PASS",
        "usage_rollup_reachable": "PASS",
    }
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/billing/credits"
    )
    assert FakeHTTPClient.requests[1]["params"] == {"limit": 500}
    assert FakeHTTPClient.requests[2]["url"].endswith("/api/v1/admin/projects/proj%2F1/usage")


def test_billing_verify_fails_when_credit_billing_gate_is_disabled(tmp_path: Path) -> None:
    """Billing verify should fail closed when platform credit billing is disabled."""
    config_path = write_config(tmp_path)
    project = {
        "id": "proj/1",
        "name": "DayPlan",
        "slug": "dayplan",
        "status": "active",
        "rate_limits": {"funding_mode": "subscription_included"},
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"project_id": "proj/1", "active_balance_usd": "0", "ledger_entries": []}),
        json_response({"totals": {}}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "billing", "verify", "--project", "dayplan"],
    )

    assert result.exit_code != 0
    assert "Billing verification failed: credit_billing_enabled" in result.output


def test_billing_verify_fails_when_platform_ledger_lacks_reservation(
    tmp_path: Path,
) -> None:
    """Billing verify should flag platform-funded ledger rows without reservation linkage."""
    config_path = write_config(tmp_path)
    project = {
        "id": "proj/1",
        "name": "DayPlan",
        "slug": "dayplan",
        "status": "active",
        "rate_limits": {
            "credit_billing_enabled": True,
            "funding_mode": "subscription_included",
        },
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "project_id": "proj/1",
                "active_balance_usd": "3.000000",
                "ledger_entries": [
                    {
                        "event_type": "agent_turn_completed",
                        "provider_source": "platform",
                    }
                ],
            }
        ),
        json_response({"totals": {"agent_turns_count": 1}}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "billing", "verify", "--project", "dayplan", "--json"],
    )

    assert result.exit_code != 0
    assert "platform_ledger_metered" in result.output
    assert '"verdict": "FAIL"' in result.output
    assert "platform ledger rows without reservation_id: agent_turn_completed" in result.output


def test_billing_checkout_rejects_unknown_tier_before_http(tmp_path: Path) -> None:
    """Checkout should only allow configured paid public tiers."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "billing",
            "checkout",
            "--project",
            "dayplan",
            "--tier",
            "enterprise",
        ],
    )

    assert result.exit_code != 0
    assert "Paid target tier must be 'build', 'pro', or 'team'" in result.output
    assert FakeHTTPClient.requests == []


def test_projects_export_writes_bounded_project_archive(tmp_path: Path) -> None:
    """Project export should write the backend's bounded archive payload."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    export_payload = {
        "api_version": "genaug.project_export.v1",
        "exported_at": "2026-05-05T10:00:00Z",
        "project_id": "proj/1",
        "project": project,
        "filters": {"include": ["config", "logs"], "limit": 25},
        "config": {"yaml_content": "apiVersion: genaug/v1\n"},
        "logs": [{"id": "log-1"}],
        "traces": [],
        "audit_events": [],
        "control_plane_events": [],
        "memory_facts": [],
        "usage_events": [],
        "notes": ["bounded"],
    }
    output_path = tmp_path / "project-export.json"
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(export_payload),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "projects",
            "export",
            "--project",
            "dayplan",
            "--include",
            "config",
            "--include",
            "logs",
            "--limit",
            "25",
            "--output",
            str(output_path),
        ],
    )

    assert result.exit_code == 0
    assert "Wrote project export" in result.output
    assert json.loads(output_path.read_text(encoding="utf-8"))["filters"]["limit"] == 25
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/export")
    assert FakeHTTPClient.requests[1]["params"] == {
        "include": ["config", "logs"],
        "limit": 25,
    }


def test_projects_archive_requires_confirmation_and_can_emit_json(tmp_path: Path) -> None:
    """Project archive should require confirmation unless --yes is supplied."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    archived = {**project, "status": "archived"}
    unconfirmed = CliRunner().invoke(
        app,
        ["--config", str(config_path), "projects", "archive", "dayplan"],
        input="n\n",
    )
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(archived),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "projects",
            "archive",
            "dayplan",
            "--yes",
            "--json",
        ],
    )

    assert unconfirmed.exit_code != 0
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "archived"
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/archive")
    assert FakeHTTPClient.requests[1]["method"] == "POST"


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
                "api_key": "gaadmlive_secret",
                "masked_key": "gaadmlive_s...cret",
                "project_id": "proj/1",
                "scopes": ["admin"],
            }
        ),
        json_response(
            {
                "items": [
                    {
                        "id": "key/1",
                        "name": "Production backend",
                        "masked_key": "gaadmlive_s...cret",
                        "project_id": "proj/1",
                        "scopes": ["admin"],
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
    assert "gaadmlive_secret" in created.output
    assert listed.exit_code == 0
    assert "gaadmlive_s...cret" in listed.output
    assert updated.exit_code == 0
    assert revoked.exit_code == 0
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/keys")
    assert FakeHTTPClient.requests[1]["json"]["project_id"] == "proj/1"
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
    assert "Agent manifest must use apiVersion genaug/v1 and kind Agent." in payload["errors"]


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


def test_deploy_validates_and_uploads_config(tmp_path: Path) -> None:
    """Deploy should upload local config through from-config when project does not exist."""
    config_path = write_config(tmp_path)
    agent_config = write_agent_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "p1", "name": "DayPlan", "slug": "dayplan"}),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "deploy", str(agent_config)])

    assert result.exit_code == 0
    assert "Project created" in result.output
    assert FakeHTTPClient.requests[0]["method"] == "GET"
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/from-config")
    assert FakeHTTPClient.requests[1]["json"]["yaml_content"].startswith("apiVersion: genaug/v1")
    assert FakeHTTPClient.requests[1]["json"]["soul_content"].startswith("# DayPlan")


def test_deploy_ignores_skill_readme_placeholders(tmp_path: Path) -> None:
    """Deploy should only upload real SKILL.md files, not generated README placeholders."""
    config_path = write_config(tmp_path)
    agent_config = write_agent_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "p1", "name": "DayPlan", "slug": "dayplan"}),
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "deploy", str(agent_config)])

    assert result.exit_code == 0
    assert FakeHTTPClient.requests[1]["json"]["skills"] == []


def test_deploy_rejects_general_augment_manifest(tmp_path: Path) -> None:
    """Deploy should reject removed GeneralAugment manifests."""
    config_path = write_config(tmp_path)
    agent_config = write_agent_config(tmp_path, api_version="legacy/v1")

    result = CliRunner().invoke(app, ["--config", str(config_path), "deploy", str(agent_config)])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "apiVersion genaug/v1" in str(result.exception)
    assert FakeHTTPClient.requests == []


def test_deploy_rejects_local_manifest_validation_errors(tmp_path: Path) -> None:
    """Deploy should run local manifest validation before calling the API."""
    config_path = write_config(tmp_path)
    agent_config = write_agent_config(tmp_path)
    payload = yaml.safe_load(agent_config.read_text(encoding="utf-8"))
    payload["tools"]["mcp"] = [
        {
            "name": "github",
            "url": "https://mcp.github.example.com/mcp",
            "headers": {"Authorization": "Bearer raw-token"},
        }
    ]
    agent_config.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    result = CliRunner().invoke(app, ["--config", str(config_path), "deploy", str(agent_config)])

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "Agent manifest validation failed" in str(result.exception)
    assert "raw secret" in str(result.exception)
    assert FakeHTTPClient.requests == []


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
        },
    }
    updated_project = {
        **project,
        "tool_discovery": {
            "mode": "always",
            "direct_schema_tool_limit": 4,
            "max_search_results": 2,
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
        }
    }
    assert FakeHTTPClient.requests[2]["headers"] == {"X-Admin-Key": "secret"}


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


def test_memory_commands_manage_tenant_user_memory(tmp_path: Path) -> None:
    """Memory commands should call scoped app-facing memory APIs."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(
            {
                "user_id": "app-user-1",
                "general_augment_user_id": "user-1",
                "memory_id": "mem/1",
                "content": "User prefers window seats.",
                "source": "tenant-app",
                "metadata": {"surface": "cli"},
                "status": "stored",
            }
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "user_id": "app-user-1",
                "facts": [
                    {
                        "id": "mem/1",
                        "fact_type": "preference",
                        "content": "User prefers window seats.",
                        "importance_score": 0.9,
                        "similarity": 0.93,
                        "source": "tenant-app",
                    }
                ],
            }
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "user_id": "app-user-1",
                "general_augment_user_id": "user-1",
                "profile": {"preferences": 1},
                "recent_facts": [{"id": "mem/1"}],
                "total_facts": 1,
            }
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "user_id": "app-user-1",
                "general_augment_user_id": "user-1",
                "memory_id": "mem/1",
                "deleted_ids": ["mem/1"],
                "deleted_count": 1,
                "status": "deleted",
            }
        ),
        json_response({"items": [project]}),
        json_response(
            {
                "user_id": "app-user-1",
                "general_augment_user_id": "user-1",
                "deleted_count": 1,
                "status": "purged",
            }
        ),
    ]
    runner = CliRunner()

    stored = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "memory",
            "store",
            "User prefers window seats.",
            "--project",
            "dayplan",
            "--user",
            "app-user-1",
            "--fact-type",
            "preference",
            "--importance",
            "0.9",
            "--source",
            "tenant-app",
            "--metadata",
            "surface=cli",
            "--idempotency-key",
            "memory-write-1",
        ],
    )
    searched = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "memory",
            "search",
            "--project",
            "dayplan",
            "--user",
            "app-user-1",
            "--query",
            "window seats",
            "--limit",
            "3",
            "--min-similarity",
            "0",
            "--fact-type",
            "preference",
            "--source",
            "tenant-app",
        ],
    )
    profile = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "memory",
            "profile",
            "--project",
            "dayplan",
            "--user",
            "app-user-1",
        ],
    )
    deleted = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "memory",
            "delete",
            "mem/1",
            "--project",
            "dayplan",
            "--user",
            "app-user-1",
        ],
    )
    purged = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "memory",
            "purge-user",
            "--project",
            "dayplan",
            "--user",
            "app-user-1",
            "--yes",
        ],
    )

    assert stored.exit_code == 0
    assert searched.exit_code == 0
    assert profile.exit_code == 0
    assert deleted.exit_code == 0
    assert purged.exit_code == 0
    assert "mem/1" in stored.output
    assert "window seats" in FakeHTTPClient.requests[3]["json"]["query"]
    assert "Total facts" in profile.output
    assert "deleted" in deleted.output
    assert "Purged 1 memory fact" in purged.output
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/agent/memory/store")
    assert FakeHTTPClient.requests[1]["headers"]["X-Project-ID"] == "proj/1"
    assert FakeHTTPClient.requests[1]["headers"]["Authorization"] == "Bearer secret"
    assert FakeHTTPClient.requests[1]["json"] == {
        "user_id": "app-user-1",
        "fact": "User prefers window seats.",
        "fact_type": "preference",
        "importance_score": 0.9,
        "source": "tenant-app",
        "metadata": {"surface": "cli"},
        "idempotency_key": "memory-write-1",
    }
    assert FakeHTTPClient.requests[3]["json"] == {
        "user_id": "app-user-1",
        "query": "window seats",
        "limit": 3,
        "min_similarity": 0.0,
        "fact_type": "preference",
        "source": "tenant-app",
    }
    assert FakeHTTPClient.requests[5]["url"].endswith("/api/v1/agent/memory/profile/app-user-1")
    assert FakeHTTPClient.requests[7]["url"].endswith("/api/v1/agent/memory/mem%2F1")
    assert FakeHTTPClient.requests[7]["params"] == {"user_id": "app-user-1"}
    assert FakeHTTPClient.requests[9]["url"].endswith("/api/v1/agent/memory/user/app-user-1")


def test_memory_json_output_is_machine_readable_with_project_key(tmp_path: Path) -> None:
    """Memory commands should also work when the configured key already scopes the project."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response(
            {
                "user_id": "app-user-1",
                "general_augment_user_id": "user-1",
                "profile": {},
                "recent_facts": [],
                "total_facts": 0,
            }
        )
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "memory", "profile", "--user", "app-user-1", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["user_id"] == "app-user-1"
    assert payload["total_facts"] == 0
    assert FakeHTTPClient.requests[0]["url"].endswith("/api/v1/agent/memory/profile/app-user-1")
    assert "X-Project-ID" not in FakeHTTPClient.requests[0]["headers"]


def test_memory_store_rejects_unknown_fact_type(tmp_path: Path) -> None:
    """Memory fact type validation should fail before any API request."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "memory",
            "store",
            "User prefers window seats.",
            "--user",
            "app-user-1",
            "--fact-type",
            "secret",
        ],
    )

    assert result.exit_code != 0
    assert "--fact-type must be one of" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_mcp_commands_manage_project_servers(tmp_path: Path) -> None:
    """MCP commands should manage tenant-owned tool servers through admin endpoints."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    server = {
        "name": "github",
        "url": "https://mcp.github.example.com/mcp",
        "headers": {"Authorization": "Bearer ${{ secrets.GITHUB_TOKEN }}"},
        "tools": {"include": ["search_repos"], "exclude": ["delete_repo"]},
        "enabled": True,
        "timeout": 10,
        "connect_timeout": 3,
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [server]}),
        json_response({"items": [project]}),
        json_response(server),
        json_response({"items": [project]}),
        json_response({"name": "github", "ok": True, "transport": "http", "detail": None}),
        json_response({"items": [project]}),
        json_response({"status": "deleted", "name": "github"}),
    ]
    runner = CliRunner()

    listed = runner.invoke(
        app,
        ["--config", str(config_path), "mcp", "list", "--project", "dayplan"],
    )
    added = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "github",
            "--project",
            "dayplan",
            "--url",
            "https://mcp.github.example.com/mcp",
            "--header",
            "Authorization=Bearer ${{ secrets.GITHUB_TOKEN }}",
            "--include-tool",
            "search_repos",
            "--exclude-tool",
            "delete_repo",
            "--timeout",
            "10",
            "--connect-timeout",
            "3",
            "--json",
        ],
    )
    tested = runner.invoke(
        app,
        ["--config", str(config_path), "mcp", "test", "github", "--project", "dayplan"],
    )
    deleted = runner.invoke(
        app,
        ["--config", str(config_path), "mcp", "delete", "github", "--project", "dayplan"],
    )

    assert listed.exit_code == 0
    assert added.exit_code == 0
    assert tested.exit_code == 0
    assert deleted.exit_code == 0
    assert "github" in listed.output
    assert json.loads(added.output)["tools"] == {
        "include": ["search_repos"],
        "exclude": ["delete_repo"],
    }
    assert "True" in tested.output
    assert "Deleted MCP server github" in deleted.output
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/mcp-servers")
    assert FakeHTTPClient.requests[3]["method"] == "POST"
    assert FakeHTTPClient.requests[3]["json"] == server
    assert FakeHTTPClient.requests[5]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/mcp-servers/github/test"
    )
    assert FakeHTTPClient.requests[7]["method"] == "DELETE"


def test_mcp_add_rejects_missing_transport(tmp_path: Path) -> None:
    """MCP add should fail locally when neither HTTP nor stdio transport is provided."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "mcp", "add", "github", "--project", "dayplan"],
    )

    assert result.exit_code != 0
    assert "Provide exactly one transport: --url or --command" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_mcp_add_rejects_ambiguous_transport(tmp_path: Path) -> None:
    """MCP add should fail locally when both transports are provided."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "github",
            "--project",
            "dayplan",
            "--url",
            "https://mcp.github.example.com/mcp",
            "--command",
            "github-mcp",
        ],
    )

    assert result.exit_code != 0
    assert "Provide exactly one transport: --url or --command" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_mcp_add_rejects_malformed_key_value_options(tmp_path: Path) -> None:
    """MCP headers and env values should use explicit key=value pairs."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "github",
            "--project",
            "dayplan",
            "--url",
            "https://mcp.github.example.com/mcp",
            "--header",
            "Authorization",
        ],
    )

    assert result.exit_code != 0
    assert "--header values must use key=value" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_model_providers_list_set_health_and_revoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model provider commands should manage tenant keys without printing raw secrets."""
    config_path = write_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-secret")
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "status": "active"}
    credential = {
        "provider": "openai",
        "status": "active",
        "base_url_configured": False,
        "api_mode": "responses",
        "model_prefixes": ["openai/"],
        "created_at": "2026-05-05T07:00:00Z",
        "updated_at": "2026-05-05T07:00:00Z",
        "last_validated_at": None,
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [credential]}),
        json_response({"items": [project]}),
        json_response(credential),
        json_response({"items": [project]}),
        json_response(
            {
                "provider": "openai",
                "status": "available",
                "message": "Provider credential accepted.",
                "checked_at": "2026-05-05T07:01:00Z",
                "latency_ms": 12,
                "status_code": 200,
                "retryable": False,
                "last_validated_at": "2026-05-05T07:01:00Z",
            }
        ),
        json_response({"items": [project]}),
        json_response({"status": "revoked", "provider": "openai"}),
    ]

    runner = CliRunner()
    list_result = runner.invoke(
        app,
        ["--config", str(config_path), "model-providers", "list", "--project", "dayplan"],
    )
    set_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "model-providers",
            "set",
            "openai",
            "--project",
            "dayplan",
            "--api-key-env",
            "OPENAI_API_KEY_TEST",
            "--api-mode",
            "responses",
            "--model-prefix",
            "openai/",
        ],
    )
    health_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "model-providers",
            "health",
            "openai",
            "--project",
            "dayplan",
            "--json",
        ],
    )
    revoke_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "model-providers",
            "revoke",
            "openai",
            "--project",
            "dayplan",
            "--yes",
        ],
    )

    assert list_result.exit_code == 0
    assert set_result.exit_code == 0
    assert health_result.exit_code == 0
    assert revoke_result.exit_code == 0
    combined_output = (
        list_result.output + set_result.output + health_result.output + revoke_result.output
    )
    assert "sk-test-secret" not in combined_output
    assert "Stored model provider credential for openai" in set_result.output
    assert json.loads(health_result.output)["status"] == "available"
    assert "Revoked model provider credential for openai" in revoke_result.output
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/model-providers"
    )
    assert FakeHTTPClient.requests[3]["method"] == "PUT"
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/model-providers/openai"
    )
    assert FakeHTTPClient.requests[3]["json"] == {
        "api_key": "sk-test-secret",
        "api_mode": "responses",
        "model_prefixes": ["openai/"],
    }
    assert FakeHTTPClient.requests[5]["method"] == "POST"
    assert FakeHTTPClient.requests[5]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/model-providers/openai/health-check"
    )
    assert FakeHTTPClient.requests[7]["method"] == "DELETE"
    assert FakeHTTPClient.requests[7]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/model-providers/openai"
    )


def test_model_provider_set_rejects_conflicting_secret_sources(tmp_path: Path) -> None:
    """Model provider set should not accept two raw key sources."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "model-providers",
            "set",
            "openai",
            "--project",
            "dayplan",
            "--api-key",
            "sk-one",
            "--api-key-env",
            "OPENAI_API_KEY_TEST",
        ],
    )

    assert result.exit_code != 0
    assert "Use only one of --api-key or --api-key-env" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_users_commands_manage_project_users(tmp_path: Path) -> None:
    """User commands should list, inspect, and delete scoped tenant users."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    user = {
        "id": "user-1",
        "phone_e164": "+15551234567",
        "display_name": "Ava",
        "last_active_at": "2026-05-05T10:00:00Z",
        "message_count": 7,
    }
    detail = {
        "user": user,
        "memory_facts": [{"id": "mem-1", "fact_type": "preference", "content": "likes SMS"}],
        "credentials": [{"provider": "google", "status": "connected", "scopes": ["calendar"]}],
        "message_count": 7,
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [user], "total": 1, "page": 1, "page_size": 25}),
        json_response({"items": [project]}),
        json_response(detail),
        json_response({"items": [project]}),
        json_response({"status": "deleted", "user_id": "user-1"}),
    ]
    runner = CliRunner()

    listed = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "users",
            "list",
            "--project",
            "dayplan",
            "--page-size",
            "25",
        ],
    )
    inspected = runner.invoke(
        app,
        ["--config", str(config_path), "users", "detail", "user-1", "--project", "dayplan"],
    )
    deleted = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "users",
            "delete",
            "user-1",
            "--project",
            "dayplan",
            "--yes",
        ],
    )

    assert listed.exit_code == 0
    assert inspected.exit_code == 0
    assert deleted.exit_code == 0
    assert "user-1" in listed.output
    assert "Memory Facts" in inspected.output
    assert "Deleted user user-1" in deleted.output
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/users")
    assert FakeHTTPClient.requests[1]["params"] == {"page": 1, "page_size": 25}
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/users/user-1"
    )
    assert FakeHTTPClient.requests[5]["method"] == "DELETE"


def test_users_delete_requires_confirmation(tmp_path: Path) -> None:
    """User deletion should ask for confirmation unless --yes is provided."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "users", "delete", "user-1", "--project", "dayplan"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert FakeHTTPClient.requests == []


def test_identity_commands_manage_test_links(tmp_path: Path) -> None:
    """Identity commands should list and create verified test links."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    link = {
        "project_id": "proj/1",
        "phone_e164": "+15551234567",
        "provider_user_id": "auth0|user_123",
        "provider_name": "mysti",
        "verified": True,
        "linked_at": "2026-05-05T10:00:00Z",
        "metadata": {"source": "genaug-cli"},
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [link]}),
        json_response({"items": [project]}),
        json_response(link),
    ]
    runner = CliRunner()

    listed = runner.invoke(
        app,
        ["--config", str(config_path), "identity", "list", "--project", "dayplan"],
    )
    created = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "identity",
            "create-test",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-user-id",
            "auth0|user_123",
            "--provider-name",
            "mysti",
            "--metadata",
            "source=genaug-cli",
            "--json",
        ],
    )

    assert listed.exit_code == 0
    assert created.exit_code == 0
    assert "auth0|user_123" in listed.output
    assert json.loads(created.output)["provider_user_id"] == "auth0|user_123"
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/identity-links"
    )
    assert FakeHTTPClient.requests[1]["params"] == {"limit": 100, "offset": 0}
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/identity-links/test"
    )
    assert FakeHTTPClient.requests[3]["json"] == {
        "phone_e164": "+15551234567",
        "provider_user_id": "auth0|user_123",
        "provider_name": "mysti",
        "metadata": {"source": "genaug-cli"},
    }


def test_identity_lifecycle_commands_call_integration_routes(tmp_path: Path) -> None:
    """Identity lifecycle commands should use documented admin-authenticated integration APIs."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    challenge = {
        "id": "link-1",
        "phone_e164": "+15551234567",
        "provider_name": "mysti",
        "provider_user_id": "auth0|user_123",
        "verification_expires_at": "2026-05-05T10:10:00Z",
        "magic_link": "https://auth.example/link",
        "debug_verification_code": "123456",
    }
    resolution = {
        "project_id": "proj/1",
        "phone_e164": "+15551234567",
        "provider_name": "mysti",
        "provider_user_id": "auth0|user_123",
        "linked_at": "2026-05-05T10:11:00Z",
        "metadata": {"source": "genaug-cli"},
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(challenge),
        json_response({"items": [project]}),
        json_response(challenge),
        json_response({"items": [project]}),
        json_response(challenge),
        json_response({"items": [project]}),
        json_response(resolution),
        json_response({"items": [project]}),
        json_response(resolution),
        json_response({"items": [project]}),
        json_response({"unlinked": True}),
    ]
    runner = CliRunner()

    commands = [
        [
            "identity",
            "link-user",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
            "--provider-user-id",
            "auth0|user_123",
            "--metadata",
            "source=genaug-cli",
            "--json",
        ],
        [
            "identity",
            "verification-code",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
            "--provider-user-id",
            "auth0|user_123",
            "--metadata",
            "source=genaug-cli",
            "--json",
        ],
        [
            "identity",
            "magic-link",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
            "--user-identifier",
            "person@example.com",
            "--channel",
            "telegram",
            "--metadata",
            "source=genaug-cli",
            "--json",
        ],
        [
            "identity",
            "verify",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
            "--code",
            "123456",
            "--provider-user-id",
            "auth0|user_123",
            "--json",
        ],
        [
            "identity",
            "resolve",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
            "--json",
        ],
        [
            "identity",
            "unlink",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
            "--yes",
            "--json",
        ],
    ]

    for command in commands:
        result = runner.invoke(app, ["--config", str(config_path), *command])
        assert result.exit_code == 0, result.output

    integration_requests = FakeHTTPClient.requests[1::2]
    assert [request["method"] for request in integration_requests] == [
        "POST",
        "POST",
        "POST",
        "POST",
        "GET",
        "DELETE",
    ]
    assert [request["url"].removeprefix("http://api.test") for request in integration_requests] == [
        "/api/v1/integrations/proj%2F1/link-user",
        "/api/v1/integrations/proj%2F1/verification-code",
        "/api/v1/integrations/proj%2F1/magic-link",
        "/api/v1/integrations/proj%2F1/verify",
        "/api/v1/integrations/proj%2F1/resolve/%2B15551234567",
        "/api/v1/integrations/proj%2F1/unlink/%2B15551234567",
    ]
    assert all(request["headers"] == {"X-Admin-Key": "secret"} for request in integration_requests)
    assert all("Authorization" not in request["headers"] for request in integration_requests)
    assert integration_requests[0]["json"] == {
        "phone_e164": "+15551234567",
        "provider_user_id": "auth0|user_123",
        "provider_name": "mysti",
        "metadata": {"source": "genaug-cli"},
    }
    assert integration_requests[1]["json"] == {
        "phone_e164": "+15551234567",
        "provider_user_id": "auth0|user_123",
        "provider_name": "mysti",
        "metadata": {"source": "genaug-cli"},
    }
    assert integration_requests[2]["json"] == {
        "phone_e164": "+15551234567",
        "user_identifier": "person@example.com",
        "provider_name": "mysti",
        "channel": "telegram",
        "metadata": {"source": "genaug-cli"},
    }
    assert integration_requests[3]["json"] == {
        "phone_e164": "+15551234567",
        "provider_name": "mysti",
        "code": "123456",
        "provider_user_id": "auth0|user_123",
    }
    assert integration_requests[4]["params"] == {"provider_name": "mysti"}
    assert integration_requests[5]["params"] == {"provider_name": "mysti"}


def test_identity_unlink_requires_confirmation(tmp_path: Path) -> None:
    """Identity unlink should not touch the API unless the operator confirms."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "identity",
            "unlink",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-name",
            "mysti",
        ],
        input="n\n",
    )

    assert result.exit_code != 0
    assert FakeHTTPClient.requests == []


def test_identity_create_test_rejects_malformed_metadata(tmp_path: Path) -> None:
    """Identity metadata should use explicit key=value pairs."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "identity",
            "create-test",
            "--project",
            "dayplan",
            "--phone",
            "+15551234567",
            "--provider-user-id",
            "auth0|user_123",
            "--metadata",
            "source",
        ],
    )

    assert result.exit_code != 0
    assert "--metadata values must use key=value" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_observability_commands_fetch_trace_and_support_bundle(tmp_path: Path) -> None:
    """Observability commands should fetch one trace and scoped support evidence."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    trace = {
        "id": "msg-1",
        "trace_id": "trace-1",
        "langfuse_url": "https://langfuse.example.com/trace-1",
        "created_at": "2026-05-05T10:00:00Z",
        "session_id": "session-1",
        "user_id": "user-1",
        "input": "hello",
        "output": "hi",
        "model_used": "openai/gpt-5.5",
        "input_tokens": 12,
        "output_tokens": 8,
        "cost_usd": 0.01,
        "latency_ms": 321,
        "tool_calls": [],
    }
    bundle = {
        "api_version": "genaug.observability_support_bundle.v1",
        "generated_at": "2026-05-05T10:01:00Z",
        "project_id": "proj/1",
        "filters": {"trace_id": "trace-1", "response_id": "resp-1", "limit": 25},
        "metrics": {
            "trace_count": 1,
            "log_count": 2,
            "audit_event_count": 1,
            "memory_fact_count": 0,
            "usage_event_count": 1,
            "timeline_event_count": 4,
        },
        "traces": [trace],
        "logs": [],
        "audit_events": [],
        "control_plane_events": [],
        "memory_facts": [],
        "usage_events": [],
        "timeline": [],
        "notes": ["bounded"],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(trace),
        json_response({"items": [project]}),
        json_response(bundle),
    ]
    output_path = tmp_path / "support-bundle.json"
    runner = CliRunner()

    fetched_trace = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "observability",
            "trace",
            "trace-1",
            "--project",
            "dayplan",
        ],
    )
    fetched_bundle = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "observability",
            "support-bundle",
            "--project",
            "dayplan",
            "--trace-id",
            "trace-1",
            "--response-id",
            "resp-1",
            "--limit",
            "25",
            "--output",
            str(output_path),
        ],
    )

    assert fetched_trace.exit_code == 0
    assert fetched_bundle.exit_code == 0
    assert "trace-1" in fetched_trace.output
    assert "Wrote support bundle" in fetched_bundle.output
    assert json.loads(output_path.read_text(encoding="utf-8"))["filters"]["trace_id"] == "trace-1"
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/traces/trace-1"
    )
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/observability/support-bundle"
    )
    assert FakeHTTPClient.requests[3]["params"] == {
        "limit": 25,
        "trace_id": "trace-1",
        "response_id": "resp-1",
    }


def test_observability_support_bundle_json_is_machine_readable(tmp_path: Path) -> None:
    """Support-bundle JSON output should be usable by support automation."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    bundle = {
        "api_version": "genaug.observability_support_bundle.v1",
        "generated_at": "2026-05-05T10:01:00Z",
        "project_id": "proj/1",
        "filters": {"user_id": "user-1"},
        "metrics": {"timeline_event_count": 0},
        "traces": [],
        "logs": [],
        "audit_events": [],
        "control_plane_events": [],
        "memory_facts": [],
        "usage_events": [],
        "timeline": [],
        "notes": [],
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(bundle),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "observability",
            "support-bundle",
            "--project",
            "dayplan",
            "--user-id",
            "user-1",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["filters"]["user_id"] == "user-1"


def test_approval_commands_list_approve_and_deny(tmp_path: Path) -> None:
    """Approval commands should inspect and resolve governed action rows."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    approval = {
        "id": "row-1",
        "approval_id": "approval-1",
        "project_id": "proj/1",
        "user_id": "user-1",
        "session_id": "session-1",
        "tool_id": "email_send",
        "action_summary": "Send email to person@example.com",
        "input_summary": '{"subject":"Hello"}',
        "channel": "sms",
        "status": "pending",
        "requested_at": "2026-05-05T10:00:00Z",
        "resolved_at": None,
        "expires_at": "2026-05-05T10:05:00Z",
        "created_at": "2026-05-05T10:00:00Z",
    }
    approved = {**approval, "status": "approved", "resolved_at": "2026-05-05T10:01:00Z"}
    denied = {**approval, "status": "denied", "resolved_at": "2026-05-05T10:02:00Z"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"items": [approval]}),
        json_response({"items": [project]}),
        json_response({"approval": approved, "enqueued": True}),
        json_response({"items": [project]}),
        json_response({"approval": denied, "enqueued": False}),
    ]
    runner = CliRunner()

    listed = runner.invoke(
        app,
        ["--config", str(config_path), "approvals", "list", "--project", "dayplan"],
    )
    approve = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "approvals",
            "approve",
            "approval-1",
            "--project",
            "dayplan",
            "--yes",
        ],
    )
    deny = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "approvals",
            "deny",
            "approval-1",
            "--project",
            "dayplan",
            "--yes",
            "--json",
        ],
    )

    assert listed.exit_code == 0
    assert approve.exit_code == 0
    assert deny.exit_code == 0
    assert "approval-1" in listed.output
    assert "enqueued=True" in approve.output
    assert json.loads(deny.output)["approval"]["status"] == "denied"
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1/approvals")
    assert FakeHTTPClient.requests[1]["params"] == {"status": "pending"}
    assert FakeHTTPClient.requests[3]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/approvals/approval-1/approve"
    )
    assert FakeHTTPClient.requests[5]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/approvals/approval-1/deny"
    )


def test_approval_actions_require_confirmation(tmp_path: Path) -> None:
    """Approval side effects should require confirmation unless --yes is provided."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "approvals",
            "approve",
            "approval-1",
            "--project",
            "dayplan",
        ],
        input="n\n",
    )

    assert result.exit_code != 0
    assert FakeHTTPClient.requests == []


def test_approval_list_rejects_unknown_status(tmp_path: Path) -> None:
    """Approval list should validate the status filter locally."""
    config_path = write_config(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "approvals",
            "list",
            "--project",
            "dayplan",
            "--status",
            "resolved",
        ],
    )

    assert result.exit_code != 0
    assert "--status must be one of: all, pending" in plain_cli_output(result)
    assert FakeHTTPClient.requests == []


def test_job_commands_cover_lifecycle_and_json_output(tmp_path: Path) -> None:
    """Scheduled job commands should use admin APIs and emit machine-readable JSON."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}
    run = {
        "id": "run/1",
        "scheduled_job_id": "job/1",
        "status": "completed",
        "scheduled_for": "2026-05-20T12:00:00Z",
        "attempt_count": 1,
        "max_attempts": 3,
        "trace_id": "trace-1",
        "agent_run_id": "agent-run-1",
        "dispatch_key": "scheduled:job/1:window",
    }
    job = {
        "id": "job/1",
        "project_id": "proj/1",
        "job_type": "agent_turn",
        "status": "active",
        "name": "Daily review",
        "target_app_user_id": "app-user-123",
        "target_channel": "api",
        "next_run_at": "2026-05-20T13:00:00Z",
        "last_run_at": "2026-05-20T12:00:00Z",
        "retry_history": [run],
        "linked_run_ids": ["agent-run-1"],
        "latest_trace_id": "trace-1",
    }
    paused = {**job, "status": "paused"}
    cancelled = {**job, "status": "cancelled", "terminal_reason": "cancelled"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(job),
        json_response({"items": [project]}),
        json_response({"items": [job]}),
        json_response({"items": [project]}),
        json_response(job),
        json_response({"items": [project]}),
        json_response({"items": [run]}),
        json_response({"items": [project]}),
        json_response({"job": job, "run": run}),
        json_response({"items": [project]}),
        json_response(paused),
        json_response({"items": [project]}),
        json_response(job),
        json_response({"items": [project]}),
        json_response(cancelled),
    ]
    runner = CliRunner()

    created = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "jobs",
            "create",
            "--project",
            "dayplan",
            "--target-app-user-id",
            "app-user-123",
            "--target-channel",
            "api",
            "--name",
            "Daily review",
            "--prompt",
            "Review this account.",
            "--interval-seconds",
            "3600",
            "--json",
        ],
    )
    listed = runner.invoke(
        app,
        ["--config", str(config_path), "jobs", "list", "--project", "dayplan", "--json"],
    )
    detail = runner.invoke(
        app,
        ["--config", str(config_path), "jobs", "detail", "job/1", "--project", "dayplan"],
    )
    runs = runner.invoke(
        app,
        ["--config", str(config_path), "jobs", "runs", "job/1", "--project", "dayplan", "--json"],
    )
    dispatched = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "jobs",
            "run",
            "job/1",
            "--project",
            "dayplan",
            "--dispatch-key",
            "operator-smoke-1",
            "--record-only",
            "--json",
        ],
    )
    paused_result = runner.invoke(
        app,
        ["--config", str(config_path), "jobs", "pause", "job/1", "--project", "dayplan", "--json"],
    )
    resumed_result = runner.invoke(
        app,
        ["--config", str(config_path), "jobs", "resume", "job/1", "--project", "dayplan"],
    )
    deleted = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "jobs",
            "delete",
            "job/1",
            "--project",
            "dayplan",
            "--yes",
            "--json",
        ],
    )

    assert created.exit_code == 0
    assert listed.exit_code == 0
    assert detail.exit_code == 0
    assert runs.exit_code == 0
    assert dispatched.exit_code == 0
    assert paused_result.exit_code == 0
    assert resumed_result.exit_code == 0
    assert deleted.exit_code == 0
    assert json.loads(created.output)["id"] == "job/1"
    assert json.loads(listed.output)["items"][0]["latest_trace_id"] == "trace-1"
    assert "Daily review" in detail.output
    assert json.loads(runs.output)["items"][0]["trace_id"] == "trace-1"
    assert json.loads(dispatched.output)["run"]["dispatch_key"] == "scheduled:job/1:window"
    assert json.loads(paused_result.output)["status"] == "paused"
    assert "Scheduled job job/1 resumed" in resumed_result.output
    assert json.loads(deleted.output)["terminal_reason"] == "cancelled"
    assert FakeHTTPClient.requests[1]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/scheduled-jobs"
    )
    assert FakeHTTPClient.requests[1]["json"]["schedule"] == {
        "type": "interval",
        "every_seconds": 3600,
    }
    assert FakeHTTPClient.requests[3]["params"] == {"limit": 50}
    assert FakeHTTPClient.requests[5]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/scheduled-jobs/job%2F1"
    )
    assert FakeHTTPClient.requests[7]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/scheduled-jobs/job%2F1/runs"
    )
    assert FakeHTTPClient.requests[9]["json"] == {
        "dispatch_key": "operator-smoke-1",
        "execute": False,
    }
    assert FakeHTTPClient.requests[15]["method"] == "DELETE"


def test_channels_status_connect_test_and_disconnect(tmp_path: Path) -> None:
    """Channel commands should call Telegram lifecycle endpoints."""
    config_path = write_config(tmp_path)
    project = {
        "id": "p1",
        "name": "DayPlan",
        "slug": "dayplan",
        "enabled_tool_ids": [],
        "whatsapp_phone_number_id": "wa_123",
    }
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response({"connected": True, "bot_username": "dayplan_bot", "message_count_24h": 2}),
        json_response({"items": [project]}),
        json_response({"connected": True, "bot_username": "dayplan_bot"}),
        json_response({"items": [project]}),
        json_response({"bot_username": "dayplan_bot"}),
        json_response({"items": [project]}),
        json_response({"ok": True, "provider_response": {"ok": True}}),
        json_response({"items": [project]}),
        json_response({"connected": False}),
    ]
    runner = CliRunner()

    status = runner.invoke(
        app,
        ["--config", str(config_path), "channels", "status", "--project", "dayplan"],
    )
    status_json = runner.invoke(
        app,
        ["--config", str(config_path), "channels", "status", "--project", "dayplan", "--json"],
    )
    connect = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "connect",
            "--project",
            "dayplan",
            "--bot-token",
            "123456:token",
        ],
    )
    test = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "test",
            "--project",
            "dayplan",
            "--chat-id",
            "12345",
            "--message",
            "hello",
            "--json",
        ],
    )
    disconnect = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "disconnect",
            "--project",
            "dayplan",
            "--yes",
        ],
    )

    assert status.exit_code == 0
    assert status_json.exit_code == 0
    assert connect.exit_code == 0
    assert test.exit_code == 0
    assert disconnect.exit_code == 0
    assert "dayplan_bot" in status.output
    assert json.loads(status_json.output)["channels"]["whatsapp"]["connected"] is True
    assert json.loads(test.output)["ok"] is True
    assert FakeHTTPClient.requests[5]["url"].endswith("/api/v1/admin/channels/telegram/connect")
    assert FakeHTTPClient.requests[7]["url"].endswith("/api/v1/admin/channels/telegram/test")
    assert FakeHTTPClient.requests[7]["json"] == {
        "project_id": "p1",
        "chat_id": "12345",
        "message": "hello",
    }
    assert FakeHTTPClient.requests[9]["url"].endswith("/api/v1/admin/channels/telegram/disconnect")


def test_channels_configures_whatsapp_and_sms_senders(tmp_path: Path) -> None:
    """Channel commands should configure non-Telegram sender ids through project updates."""
    config_path = write_config(tmp_path)
    project = {"id": "proj/1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    whatsapp_project = {**project, "whatsapp_phone_number_id": "wa_123"}
    sms_project = {**project, "twilio_phone_number": "+15551234567"}
    FakeHTTPClient.queue = [
        json_response({"items": [project]}),
        json_response(whatsapp_project),
        json_response({"items": [project]}),
        json_response(sms_project),
        json_response({"items": [whatsapp_project]}),
        json_response({**whatsapp_project, "whatsapp_phone_number_id": None}),
        json_response({"items": [sms_project]}),
        json_response({**sms_project, "twilio_phone_number": None}),
    ]
    runner = CliRunner()

    whatsapp_connect = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "connect",
            "--project",
            "dayplan",
            "--channel",
            "whatsapp",
            "--phone-number-id",
            "wa_123",
        ],
    )
    sms_connect = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "connect",
            "--project",
            "dayplan",
            "--channel",
            "sms",
            "--twilio-number",
            "+15551234567",
        ],
    )
    whatsapp_disconnect = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "disconnect",
            "--project",
            "dayplan",
            "--channel",
            "whatsapp",
            "--yes",
            "--json",
        ],
    )
    sms_disconnect = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "disconnect",
            "--project",
            "dayplan",
            "--channel",
            "sms",
            "--yes",
            "--json",
        ],
    )

    assert whatsapp_connect.exit_code == 0
    assert sms_connect.exit_code == 0
    assert whatsapp_disconnect.exit_code == 0
    assert sms_disconnect.exit_code == 0
    assert "WhatsApp sender configured" in whatsapp_connect.output
    assert "SMS sender configured" in sms_connect.output
    assert FakeHTTPClient.requests[1]["method"] == "PATCH"
    assert FakeHTTPClient.requests[1]["url"].endswith("/api/v1/admin/projects/proj%2F1")
    assert FakeHTTPClient.requests[1]["json"] == {"whatsapp_phone_number_id": "wa_123"}
    assert FakeHTTPClient.requests[3]["json"] == {"twilio_phone_number": "+15551234567"}
    assert FakeHTTPClient.requests[5]["json"] == {"whatsapp_phone_number_id": None}
    assert FakeHTTPClient.requests[7]["json"] == {"twilio_phone_number": None}
    assert json.loads(whatsapp_disconnect.output)["whatsapp_phone_number_id"] is None
    assert json.loads(sms_disconnect.output)["twilio_phone_number"] is None


def test_channels_rejects_blank_sender_value(tmp_path: Path) -> None:
    """Channel configuration should fail closed before saving empty provider identifiers."""
    config_path = write_config(tmp_path)
    project = {"id": "proj_1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [json_response({"items": [project]})]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "channels",
            "connect",
            "--project",
            "dayplan",
            "--channel",
            "whatsapp",
            "--phone-number-id",
            "   ",
        ],
    )

    assert result.exit_code != 0
    assert "--phone-number-id is required for this channel" in plain_cli_output(result)
    assert len(FakeHTTPClient.requests) == 1
    assert FakeHTTPClient.requests[0]["method"] == "GET"


def test_status_and_logs(tmp_path: Path) -> None:
    """Status and logs should call public health plus admin project endpoints."""
    config_path = write_config(tmp_path)
    project = {"id": "p1", "name": "DayPlan", "slug": "dayplan", "enabled_tool_ids": []}
    FakeHTTPClient.queue = [
        json_response({"status": "ok"}),
        json_response({"status": "ready"}),
        text_response("general_augment_requests_total 1"),
        json_response({"items": [project]}),
        json_response(
            {"totals": {"agent_turns_count": 2, "messages_count": 3, "tool_calls_count": 1}}
        ),
        json_response({"items": [project]}),
        json_response({"items": [{"created_at": "now", "role": "assistant", "content": "hello"}]}),
    ]
    runner = CliRunner()

    status = runner.invoke(app, ["--config", str(config_path), "status", "--project", "dayplan"])
    logs = runner.invoke(app, ["--config", str(config_path), "logs", "--project", "dayplan"])

    assert status.exit_code == 0
    assert logs.exit_code == 0
    assert "Platform Status" in status.output
    assert "hello" in logs.output


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
                "metadata": {"trace_id": "trace_smoke", "request_id": "req_smoke"},
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
                "metadata": {
                    "general_augment_cost_usd": 0.004,
                    "general_augment_request_id": "req_1",
                    "general_augment_trace_id": "trace_1",
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
        "cost_usd": 0.004,
        "ready_status": "ok",
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
        "traces": [{"trace_id": "trace_1", "response_id": "resp_smoke"}],
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
                "metadata": {
                    "general_augment_request_id": "req_1",
                    "general_augment_trace_id": "trace_1",
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
    payload = json.loads(result.output)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["evidence"]["schema_version"] == "general-augment-smoke-evidence/v1"
    assert evidence["support_receipt"]["trace_id"] == "trace_1"
    assert evidence["support_receipt"]["response_id"] == "resp_smoke"
    assert evidence["support_bundle"] == support_bundle
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
                "api_key": "gaadmlive_secret",
                "masked_key": "gaadmlive_s...cret",
                "project_id": "proj/1",
                "scopes": ["admin"],
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
    assert payload["api_key"] == "gaadmlive_secret"
    assert payload["id"] == "key/1"


def test_auth_login_json_is_machine_readable_without_leaking_key(tmp_path: Path) -> None:
    """auth login --json should report verified auth metadata but never the raw key."""
    config_path = tmp_path / "config.yaml"
    FakeHTTPClient.queue = [
        json_response({"auth_method": "api_key", "project_id": "p1", "project_ids": ["p1"]}),
    ]

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "auth",
            "login",
            "--api-key",
            "super-secret-key",
            "--base-url",
            "http://api.test",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["authenticated"] is True
    assert payload["auth_method"] == "api_key"
    assert payload["verified"] is True
    assert "super-secret-key" not in result.output


def test_deploy_json_emits_project_payload(tmp_path: Path) -> None:
    """deploy --json should emit the raw project payload for automation."""
    config_path = write_config(tmp_path)
    agent_config = write_agent_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response({"items": []}),
        json_response({"id": "proj/1", "name": "DayPlan", "slug": "dayplan"}),
    ]

    result = CliRunner().invoke(
        app,
        ["--config", str(config_path), "deploy", str(agent_config), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "proj/1"
    assert payload["slug"] == "dayplan"


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


def test_installer_slug_resolves_to_uuid_before_typed_route(tmp_path: Path) -> None:
    """Installer setup must resolve a slug to its UUID so typed routes do not 422."""
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
    FakeHTTPClient.queue = [
        json_response(
            {
                "items": [
                    {
                        "id": "11111111-1111-4111-8111-111111111111",
                        "name": "Demo App",
                        "slug": "demo-app",
                    }
                ]
            }
        ),
        json_response({"provider": "browserbase", "status": "active"}),
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
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/api/v1/installer/projects"
    assert FakeHTTPClient.requests[1]["url"] == (
        "http://api.test/api/v1/installer/projects/"
        "11111111-1111-4111-8111-111111111111/capability-providers/browserbase"
    )


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


def test_verify_runs_project_acceptance_checks(tmp_path: Path) -> None:
    """Verify should stitch platform, project, test, logs, usage, and observability checks."""
    config_path = write_config(tmp_path)
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
        json_response({"response_text": "General Augment project works.", "metadata": {}}),
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
    assert FakeHTTPClient.requests[11]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/observability"
    )
    assert FakeHTTPClient.requests[12]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/channels/status"
    )
    assert FakeHTTPClient.requests[13]["url"].endswith("/api/v1/agent/memory/store")
    assert FakeHTTPClient.requests[13]["headers"] == {
        "Authorization": "Bearer secret",
        "X-Project-ID": "proj/1",
    }
    assert FakeHTTPClient.requests[13]["json"]["source"] == "genaug-cli-verify"
    assert FakeHTTPClient.requests[13]["json"]["metadata"]["scenario"] == "project-verify"
    assert FakeHTTPClient.requests[13]["json"]["metadata"]["verification_id"]
    assert FakeHTTPClient.requests[13]["json"]["idempotency_key"].startswith(
        "genaug-verify-proj/1-genaug-verify-user-"
    )
    assert (
        FakeHTTPClient.requests[13]["json"]["idempotency_key"]
        != "genaug-verify-proj/1-genaug-verify-user"
    )
    assert FakeHTTPClient.requests[14]["url"].endswith("/api/v1/agent/memory/search")
    assert FakeHTTPClient.requests[16]["method"] == "DELETE"
    assert FakeHTTPClient.requests[16]["url"].endswith("/api/v1/agent/memory/mem%2F1")
    assert FakeHTTPClient.requests[16]["params"] == {"user_id": "genaug-verify-user"}
    assert FakeHTTPClient.requests[17]["url"].endswith(
        "/api/v1/admin/projects/proj%2F1/audit/tool-calls"
    )


def test_verify_exercises_responses_when_cli_key_is_project_scoped(tmp_path: Path) -> None:
    """Verify should prove the configured project key can call `/v1/responses`."""
    config_path = write_config(tmp_path)
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
        json_response({"response_text": "General Augment project works.", "metadata": {}}),
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
        "runtime_policy_visible",
        "memory_tested",
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


def test_onboarding_verify_json_wraps_project_acceptance(tmp_path: Path) -> None:
    """The onboarding command should expose one JSON gate for coding agents."""

    config_path = write_config(tmp_path)
    queue_project_verification_success()

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "onboarding",
            "verify",
            "--project",
            "dayplan",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "secret" not in result.output
    payload = json.loads(result.output)
    assert payload["cli"]["version"] == "0.1.1"
    assert payload["api"] == {"build_sha": "abc123", "status": "ok", "version": "0.1.0"}
    assert payload["readiness_checklist"]["version"] == "general-augment-readiness/v1"
    assert payload["onboarding"]["verdict"] == "PASS"
    assert "Handle 402 and 429" in payload["onboarding"]["required_follow_up"][2]
    assert payload["checks"][-1]["name"] == "tool_call_audit"
    assert payload["runtime_policy"]["model_routing"]["channel_parity"] is True
    assert any(item["name"] == "runtime_policy_model_routing" for item in payload["checks"])
    assert any(item["name"] == "soul_visible" for item in payload["checks"])
    assert any(item["name"] == "skills_visible" for item in payload["checks"])
    assert FakeHTTPClient.requests[0]["url"] == "http://api.test/health/ready"


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
        json_response({"response_text": "General Augment project works.", "metadata": {}}),
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
def test_api_error_messages_are_helpful(tmp_path: Path, status_code: int, message: str) -> None:
    """API errors should produce actionable Rich output."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [json_response({"detail": "boom"}, status_code=status_code)]

    result = CliRunner().invoke(app, ["--config", str(config_path), "projects", "list"])

    assert result.exit_code != 0
    assert message in result.output


def test_api_rate_limit_error_includes_reason_and_retry_after(tmp_path: Path) -> None:
    """Rate-limit API errors should surface stable reasons and retry timing."""
    config_path = write_config(tmp_path)
    FakeHTTPClient.queue = [
        json_response(
            {
                "detail": {
                    "code": "rate_limited",
                    "reason": "messages_per_user_per_minute_exceeded",
                    "message": "Project per-user message rate limit exceeded.",
                }
            },
            status_code=429,
            headers={"Retry-After": "60"},
        )
    ]

    result = CliRunner().invoke(app, ["--config", str(config_path), "projects", "list"])

    assert result.exit_code != 0
    assert "messages_per_user_per_minute_exceeded" in result.output
    assert "Retry after" in result.output
    assert "60 seconds" in result.output


def write_config(tmp_path: Path) -> Path:
    """Write a CLI config fixture."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("base_url: http://api.test\napi_key: secret\n", encoding="utf-8")
    return config_path


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
        json_response({"response_text": "General Augment project works.", "metadata": {}}),
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
