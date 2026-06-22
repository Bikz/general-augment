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


def _migrate(workspace: Path, *extra: str) -> object:
    return CliRunner().invoke(
        app,
        ["migrate", "openai-responses", "--workspace", str(workspace), "--json", *extra],
    )


def test_migrate_python_openai_app_is_migrated(tmp_path: Path) -> None:
    """A Python OpenAI app should get base_url + key env redirected (was a silent no-op)."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.py"
    source.write_text(
        "import os\n"
        "from openai import OpenAI\n\n"
        'client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])\n\n'
        "def run(text):\n"
        '    return client.responses.create(model="gpt-5.1", input=text)\n',
        encoding="utf-8",
    )

    result = _migrate(workspace, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["migration"]["changed"] is True
    assert "agent.py" in payload["migration"]["diff_files"]
    updated = source.read_text(encoding="utf-8")
    assert 'os.environ["GENAUG_API_KEY"]' in updated
    assert "GENAUG_OPENAI_BASE_URL" in updated
    assert "base_url=" in updated
    # The result must still be valid Python.
    import ast

    ast.parse(updated)


def test_migrate_js_standard_env_var(tmp_path: Path) -> None:
    """Standard JS OpenAI client gets baseURL injected and key swapped."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.ts"
    source.write_text(
        "import OpenAI from 'openai';\n"
        "const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n",
        encoding="utf-8",
    )

    result = _migrate(workspace, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    updated = source.read_text(encoding="utf-8")
    assert "process.env.GENAUG_API_KEY" in updated
    assert "baseURL: process.env.GENAUG_OPENAI_BASE_URL" in updated


def test_migrate_js_nonstandard_key_not_redirected(tmp_path: Path) -> None:
    """A non-standard key must NOT be silently sent to GA; warn + leave a TODO instead."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.ts"
    original = (
        "import OpenAI from 'openai';\n"
        "const client = new OpenAI({ apiKey: process.env.AZURE_OPENAI_KEY });\n"
    )
    source.write_text(original, encoding="utf-8")

    result = _migrate(workspace, "--apply", "--yes")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    updated = source.read_text(encoding="utf-8")
    # Foreign key is left as-is, not repointed to GA.
    assert "process.env.AZURE_OPENAI_KEY" in updated
    assert "process.env.GENAUG_API_KEY" not in updated
    assert "TODO(genaug)" in updated
    assert any("non-standard" in w for w in payload["migration"]["warnings"])


def test_migrate_js_positional_constructor_warns_and_skips(tmp_path: Path) -> None:
    """Positional `new OpenAI(key)` cannot get a baseURL; warn-and-skip, do not half-migrate."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.ts"
    original = (
        "import OpenAI from 'openai';\n"
        "const client = new OpenAI(process.env.OPENAI_API_KEY);\n"
    )
    source.write_text(original, encoding="utf-8")

    result = _migrate(workspace)

    payload = json.loads(result.output)
    # Source is untouched (no baseURL injected, key not swapped).
    assert source.read_text(encoding="utf-8") == original
    assert any("positional" in w for w in payload["migration"]["warnings"])
    # Nothing migrated in code, only env.example planned -> still surfaces as changed=env.
    assert "agent.ts" not in payload["migration"]["diff_files"]


def test_migrate_env_var_without_client_is_not_clobbered(tmp_path: Path) -> None:
    """A file that mentions OPENAI_API_KEY but constructs no client is left alone."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    # No OpenAI client anywhere -> nothing to migrate.
    source = workspace / "config.ts"
    original = "export const key = process.env.OPENAI_API_KEY;\n"
    source.write_text(original, encoding="utf-8")

    result = _migrate(workspace)

    payload = json.loads(result.output)
    assert source.read_text(encoding="utf-8") == original
    # No client constructed anywhere -> nothing changed, honest non-success exit.
    assert payload["migration"]["changed"] is False
    assert result.exit_code != 0


def test_migrate_dry_run_writes_nothing_to_app_tree_and_diff_includes_env(
    tmp_path: Path,
) -> None:
    """Dry-run must not touch the app tree, and the preview diff must include env changes."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "agent.ts"
    original = (
        "import OpenAI from 'openai';\n"
        "const client = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
    )
    source.write_text(original, encoding="utf-8")
    before = sorted(p.name for p in workspace.iterdir())

    result = _migrate(workspace, "--dry-run")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # App source untouched.
    assert source.read_text(encoding="utf-8") == original
    # No .env / .env.example written into the app tree on dry-run.
    assert not (workspace / ".env.example").exists()
    # Only the .genaug artifact dir is added beyond the original files.
    after = sorted(p.name for p in workspace.iterdir())
    assert set(after) - set(before) <= {".genaug"}
    # Preview diff must show BOTH the code change and the env change.
    diff = Path(payload["migration"]["diff_path"]).read_text(encoding="utf-8")
    assert "GENAUG_OPENAI_BASE_URL" in diff
    assert ".env.example" in diff
    assert "GENAUG_PROJECT_ID" in diff


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
    assert (
        json.loads(dashboard.output)["url"]
        == "https://app.generalaugment.com/dashboard/projects/demo-agent"
    )
