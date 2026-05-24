"""Self-serve setup and migration CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from platform_cli.main import app


def test_setup_inspects_workspace_and_writes_secret_free_plan(tmp_path: Path) -> None:
    """Setup should produce an inspectable plan without changing app code or leaking secrets."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.0.0", "openai": "6.0.0"}}),
        encoding="utf-8",
    )
    route = workspace / "app" / "api" / "agent"
    route.mkdir(parents=True)
    (route / "route.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export async function POST() {\n"
        "  return client.responses.create({ model: 'gpt-5.1', input: 'hi' });\n"
        "}\n",
        encoding="utf-8",
    )
    (workspace / ".env").write_text("OPENAI_API_KEY=sk-should-not-appear\n", encoding="utf-8")
    (workspace / "prompts").mkdir()
    (workspace / "prompts" / "system.md").write_text("You are helpful.", encoding="utf-8")
    plan_path = tmp_path / "setup-plan.json"

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--capability",
            "code",
            "--capability",
            "browse",
            "--output",
            str(plan_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "sk-should-not-appear" not in result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "setup"
    assert payload["safety"] == {
        "code_changes_applied": False,
        "secrets_written": False,
        "raw_provider_credentials_stored_locally": False,
    }
    assert payload["auth"]["status"] == "not_authenticated"
    assert payload["detected"]["frameworks"] == ["nextjs"]
    assert payload["detected"]["openai"]["responses_api_call_count"] == 1
    assert payload["detected"]["env_files"][0]["secret_values_redacted"] is True
    assert payload["requested_capabilities"] == ["code", "browse"]
    assert "genaug auth login" in "\n".join(payload["next_actions"])
    assert plan_path.exists()
    assert json.loads(plan_path.read_text(encoding="utf-8")) == payload
    assert "GENAUG_API_KEY" not in (workspace / ".env").read_text(encoding="utf-8")


def test_init_without_name_starts_self_serve_setup_plan(tmp_path: Path) -> None:
    """Bare init should support the new app-inspection onboarding path."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"dependencies": {"next": "16.0.0", "openai": "6.0.0"}}),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["init", "--workspace", str(workspace), "--capability", "browse", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "setup"
    assert payload["detected"]["frameworks"] == ["nextjs"]
    assert payload["requested_capabilities"] == ["browse"]
    assert payload["safety"]["code_changes_applied"] is False


def test_migrate_openai_responses_dry_run_generates_diff_without_mutating(
    tmp_path: Path,
) -> None:
    """Migration dry-run should show a patch plan while leaving app files untouched."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.ts"
    original = (
        "import OpenAI from 'openai';\n\n"
        "const openai = new OpenAI({\n"
        "  apiKey: process.env.OPENAI_API_KEY,\n"
        "});\n\n"
        "export async function runAgent(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n"
    )
    source.write_text(original, encoding="utf-8")
    plan_path = tmp_path / "migration-plan.json"

    result = CliRunner().invoke(
        app,
        [
            "migrate",
            "openai-responses",
            "--workspace",
            str(workspace),
            "--dry-run",
            "--output",
            str(plan_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "migrate"
    assert payload["migration"]["source"] == "openai-responses"
    assert payload["migration"]["apply"] is False
    assert payload["migration"]["diff_files"]
    diff_path = Path(payload["migration"]["diff_path"])
    assert diff_path.exists()
    diff = diff_path.read_text(encoding="utf-8")
    assert "GENAUG_API_KEY" in diff
    assert "GENAUG_OPENAI_BASE_URL" in diff
    assert "api.generalaugment.com/v1" in diff
    assert source.read_text(encoding="utf-8") == original
    assert json.loads(plan_path.read_text(encoding="utf-8")) == payload


def test_migrate_openai_responses_apply_requires_explicit_yes(tmp_path: Path) -> None:
    """Migration apply should be explicit and patch only known-safe client config."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.ts"
    source.write_text(
        "import OpenAI from 'openai';\n\n"
        "const openai = new OpenAI({\n"
        "  apiKey: process.env.OPENAI_API_KEY,\n"
        "});\n",
        encoding="utf-8",
    )

    refused = CliRunner().invoke(
        app,
        [
            "migrate",
            "openai-responses",
            "--workspace",
            str(workspace),
            "--apply",
            "--json",
        ],
        input="n\n",
    )
    assert refused.exit_code != 0
    assert "GENAUG_API_KEY" not in source.read_text(encoding="utf-8")

    applied = CliRunner().invoke(
        app,
        [
            "migrate",
            "openai-responses",
            "--workspace",
            str(workspace),
            "--apply",
            "--yes",
            "--json",
        ],
    )

    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.output)
    assert payload["migration"]["apply"] is True
    updated = source.read_text(encoding="utf-8")
    assert "process.env.GENAUG_API_KEY" in updated
    assert (
        'baseURL: process.env.GENAUG_OPENAI_BASE_URL ?? "https://api.generalaugment.com/v1"'
        in updated
    )
    assert (workspace / ".env.example").exists()
    env_example = (workspace / ".env.example").read_text(encoding="utf-8")
    assert "GENAUG_PROJECT_ID" in env_example
    assert "GENAUG_API_BASE_URL" in env_example


def test_setup_subcommands_are_agent_friendly_json(tmp_path: Path) -> None:
    """Provider, connector, skill, and dashboard helpers should expose machine-readable setup."""
    workspace = tmp_path / "app"
    workspace.mkdir()

    providers = CliRunner().invoke(
        app,
        ["providers", "setup", "--capability", "code", "--capability", "search-x", "--json"],
    )
    connectors = CliRunner().invoke(
        app,
        ["connectors", "setup", "--workspace", str(workspace), "--json"],
    )
    skills = CliRunner().invoke(
        app,
        [
            "skills",
            "design",
            "--workspace",
            str(workspace),
            "--job-type",
            "website-builder",
            "--json",
        ],
    )
    dashboard = CliRunner().invoke(
        app,
        ["dashboard", "open", "--project", "demo-agent", "--no-browser", "--json"],
    )

    assert providers.exit_code == 0, providers.output
    assert connectors.exit_code == 0, connectors.output
    assert skills.exit_code == 0, skills.output
    assert dashboard.exit_code == 0, dashboard.output
    assert json.loads(providers.output)["providers"][0]["credential_custody"] == "general_augment"
    assert json.loads(connectors.output)["connectors"][0]["setup_command"].startswith(
        "genaug mcp add"
    )
    assert json.loads(skills.output)["skill"]["name"] == "Website Builder"
    assert json.loads(dashboard.output)["url"] == "https://app.generalaugment.com/projects/demo-agent"
