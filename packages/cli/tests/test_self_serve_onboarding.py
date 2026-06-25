"""Self-serve setup and migration CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import Result
from pytest import MonkeyPatch
from typer.testing import CliRunner

from platform_cli.commands import migrate as migrate_command
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


def test_setup_guided_answers_are_redacted_and_shape_next_steps(tmp_path: Path) -> None:
    """Guided setup should capture operator intent without storing pasted secret values."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "agent_goal": "Let Telegram users create polished websites.",
                "primary_channel": "telegram",
                "capabilities": ["code", "browse", "search-x"],
                "job_type": "website-builder",
                "provider_plan": "Use Browserbase and Anthropic, key sk-should-not-appear",
                "allow_production_deploy": False,
                "migrate_openai_responses": True,
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--guided",
            "--answers-file",
            str(answers_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "sk-should-not-appear" not in result.output
    payload = json.loads(result.output)
    assert payload["requested_capabilities"] == ["code", "browse", "search-x"]
    assert payload["guided"]["schema_version"] == "general-augment-guided-setup/v1"
    assert payload["guided"]["answers"]["agent_goal"] == (
        "Let Telegram users create polished websites."
    )
    assert payload["guided"]["answers"]["provider_plan"] == (
        "Use Browserbase and Anthropic, key [REDACTED]"
    )
    assert payload["guided"]["answers"]["primary_channel"] == "telegram"
    assert payload["guided"]["answers"]["allow_production_deploy"] is False
    assert payload["guided"]["recommended_commands"] == [
        "genaug auth login",
        "genaug setup --bootstrap --project-name <name> --project-slug <slug> --print-env",
        (
            "genaug providers setup --provider anthropic-managed-agents --project <project> "
            "--api-key-env ANTHROPIC_API_KEY --health-check"
        ),
        (
            "genaug providers setup --provider codex-mcp --project <project> "
            "--api-key-env OPENAI_API_KEY --health-check"
        ),
        (
            "genaug providers setup --provider browserbase --project <project> "
            "--api-key-env BROWSERBASE_API_KEY --health-check"
        ),
        (
            "genaug providers setup --provider xai --project <project> "
            "--api-key-env XAI_API_KEY --health-check"
        ),
        "genaug providers readiness --project <project> --json",
        (
            "genaug providers smoke --provider anthropic-managed-agents "
            "--project <project> --api-key-env ANTHROPIC_API_KEY"
        ),
        (
            "genaug providers smoke --provider codex-mcp "
            "--project <project> --api-key-env OPENAI_API_KEY"
        ),
        (
            "genaug providers smoke --provider browserbase "
            "--project <project> --api-key-env BROWSERBASE_API_KEY"
        ),
        "genaug providers smoke --provider xai --project <project> --api-key-env XAI_API_KEY",
        (
            "genaug browser-runs scaffold-function --project <project> "
            "--output browserbase-functions --json"
        ),
        (
            "genaug browser-runs register-action --project <project> "
            "--name <browser-action-name> --function-id <browserbase-function-id> "
            "--status active --json"
        ),
        (
            "genaug browser-runs update-action-deployment --project <project> "
            "--name <browser-action-name> --deployment-status published "
            "--deployment-version-id <browserbase-function-version-id> "
            "--deployment-source-ref <git-sha> --json"
        ),
        "genaug browser-runs list-actions --project <project> --json",
        (
            "genaug browser-runs execute --project <project> "
            "--function-ref <browser-action-name> --task <task> --json"
        ),
        "genaug skills design --job-type website-builder --project <project> --apply",
        "genaug migrate openai-responses --dry-run --json",
        (
            "genaug channels connect --project <project> --channel telegram "
            "--webhook-base-url <api-base-url>"
        ),
        "genaug channels status --project <project> --json",
        (
            "genaug channels test --project <project> --channel telegram "
            "--chat-id <telegram-chat-id> --json"
        ),
        "genaug smoke --project <project> --json",
        "genaug evals create-smoke --output tests/fixtures/agent_evals/tenant_smoke.json",
        "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json --gate --json",
        (
            "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json "
            "--mode hosted --project <project> --gate --wait --fail-on-fail --json"
        ),
        (
            "genaug evals release-gate --project <project> --change-type hermes_upgrade "
            "--change-type prompt_template --change-type tool_generator "
            "--change-type guardrail_change --change-type memory_policy "
            "--change-type browser_action --change-type model_replay --fail-on-fail --json"
        ),
        "genaug dashboard open --project <project>",
    ]
    assert payload["guided"]["policy"]["production_deploy_default"] == "approval_required"


def test_setup_guided_json_requires_answers_file(tmp_path: Path) -> None:
    """Interactive guided prompts should not contaminate machine-readable JSON output."""
    workspace = tmp_path / "app"
    workspace.mkdir()

    result = CliRunner().invoke(
        app,
        ["setup", "--workspace", str(workspace), "--guided", "--json"],
    )

    assert result.exit_code != 0
    assert result.output.strip()
    assert not result.output.lstrip().startswith("{")


def test_setup_can_write_agent_friendly_guided_answers_template(tmp_path: Path) -> None:
    """Setup should produce a secret-free questionnaire before interactive migration."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export function run(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n",
        encoding="utf-8",
    )
    template_path = tmp_path / "genaug-answers.template.json"

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--answers-template",
            str(template_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    saved = json.loads(template_path.read_text(encoding="utf-8"))
    assert saved == payload
    assert payload["schema_version"] == "general-augment-guided-answers-template/v1"
    assert payload["detected"]["openai"]["responses_api_call_count"] == 1
    assert payload["answers"]["setup_mode"] == "migrate"
    assert payload["answers"]["capabilities"] == ["code", "browse"]
    assert payload["answers"]["provider_env_vars"]["browserbase"] == "BROWSERBASE_API_KEY"
    assert payload["capability_options"] == ["code", "browse", "search-x", "video"]
    assert payload["next_command"].endswith(
        f"setup --guided --answers-file {template_path} --json"
    )
    assert payload["human_pause_points"][0]["id"] == "provider_keys"
    assert payload["security"] == {
        "raw_provider_keys_allowed": False,
        "raw_secrets_in_template": False,
        "stores_credentials": False,
    }
    rendered = json.dumps(payload)
    assert "sk-" not in rendered
    assert "OPENAI_API_KEY=" not in rendered


def test_setup_interactive_wizard_shapes_launch_commands(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The guided wizard should collect intent, env var names, and PR/smoke choices."""
    # Isolate from the ambient environment: the wizard reports a provider as "set" when its
    # env var is present, so a developer shell with these keys would otherwise flip statuses.
    _provider_vars = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "BROWSERBASE_API_KEY", "XAI_API_KEY")
    for _provider_var in _provider_vars:
        monkeypatch.delenv(_provider_var, raising=False)
    workspace = tmp_path / "telegram-sites"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export function run(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "setup-plan.json"

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--interactive",
            "--output",
            str(plan_path),
        ],
        input=(
            "\n"  # setup mode defaults to migrate because OpenAI Responses was detected
            "Telegram Sites\n"
            "\n"  # project slug from project name
            "Let Telegram users create polished websites.\n"
            "telegram\n"
            "code,browse,x\n"
            "ANTHROPIC_API_KEY\n"
            "OPENAI_API_KEY\n"
            "BROWSERBASE_API_KEY\n"
            "XAI_API_KEY\n"
            "website-builder\n"
            "browserbase,github\n"
            "Use the customer's brand notes and preview before deploy.\n"
            "n\n"  # no production deploy tools on first setup
            "\n"  # migration default yes
            "\n"  # PR default yes
            "\n"  # smoke default yes
            "\n"  # dashboard default yes
        ),
    )

    assert result.exit_code == 0, result.output
    assert "General Augment interactive setup" in result.output
    assert "This wizard writes a setup plan first; it will not edit app code or save secrets." in (
        result.output
    )
    assert "Launch checklist" in result.output
    assert "Question map" in result.output
    assert "Human pause points" in result.output
    assert "Setup review" in result.output
    assert "Guided setup summary" in result.output
    assert "Recommended next commands" in result.output
    assert "genaug providers smoke --provider browserbase" in result.output
    assert "GENAUG_API_KEY" not in result.output
    assert plan_path.exists()
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    guided = payload["guided"]
    assert payload["requested_capabilities"] == ["code", "browse", "search-x"]
    assert guided["answers"]["setup_mode"] == "migrate"
    assert guided["answers"]["project_name"] == "Telegram Sites"
    assert guided["answers"]["project_slug"] == "telegram-sites"
    assert guided["answers"]["provider_env_vars"] == {
        "anthropic-managed-agents": "ANTHROPIC_API_KEY",
        "codex-mcp": "OPENAI_API_KEY",
        "browserbase": "BROWSERBASE_API_KEY",
        "xai": "XAI_API_KEY",
    }
    assert guided["answers"]["open_pull_request"] is True
    assert guided["wizard"]["operator_review"] == {
        "mode": "migrate",
        "summary": (
            "Configure General Augment and prepare an OpenAI Responses migration PR."
        ),
        "code_changes": "pull_request_planned",
        "provider_credentials": [
            {
                "provider": "anthropic-managed-agents",
                "capability": "code",
                "env_var": "ANTHROPIC_API_KEY",
                "status": "missing",
            },
            {
                "provider": "codex-mcp",
                "capability": "code",
                "env_var": "OPENAI_API_KEY",
                "status": "missing",
            },
            {
                "provider": "browserbase",
                "capability": "browse",
                "env_var": "BROWSERBASE_API_KEY",
                "status": "missing",
            },
            {
                "provider": "xai",
                "capability": "search-x",
                "env_var": "XAI_API_KEY",
                "status": "missing",
            },
        ],
        "proof": [
            {
                "id": "responses_smoke",
                "label": "Responses smoke",
                "status": "planned",
                "command": "genaug smoke --project <project> --json",
            },
            {
                "id": "dashboard_trace_review",
                "label": "Dashboard trace review",
                "status": "planned",
                "command": "genaug dashboard open --project <project>",
            },
        ],
        "safety": [
            {
                "id": "raw_provider_keys",
                "label": "Raw provider keys stay out of repo files and setup artifacts.",
                "status": "enforced",
            },
            {
                "id": "production_deploy",
                "label": "Production deploy, billing, and destructive tools remain approval gated.",
                "status": "approval_required",
            },
            {
                "id": "code_changes",
                "label": "App code changes require migration apply or a migration PR.",
                "status": "explicit_consent_required",
            },
        ],
    }
    assert guided["wizard"]["question_map"] == [
        {
            "id": "project_and_intent",
            "label": "Project and app intent",
            "answer_keys": [
                "setup_mode",
                "project_name",
                "project_slug",
                "agent_goal",
                "primary_channel",
            ],
            "who_can_answer": "coding_agent_or_human",
        },
        {
            "id": "capabilities",
            "label": "Capabilities",
            "answer_keys": ["capabilities"],
            "who_can_answer": "coding_agent_or_human",
        },
        {
            "id": "provider_custody",
            "label": "Provider custody",
            "answer_keys": ["provider_env_vars"],
            "who_can_answer": "human_for_secret_sources",
        },
        {
            "id": "skills_and_connectors",
            "label": "Skills and connectors",
            "answer_keys": ["job_type", "connector_plan", "skill_notes"],
            "who_can_answer": "coding_agent_or_human",
        },
        {
            "id": "safety_boundaries",
            "label": "Safety boundaries",
            "answer_keys": [
                "allow_production_deploy",
                "migrate_openai_responses",
                "open_pull_request",
            ],
            "who_can_answer": "human_for_risky_tools",
        },
        {
            "id": "proof",
            "label": "Proof",
            "answer_keys": ["run_smoke", "open_dashboard"],
            "who_can_answer": "coding_agent_or_human",
        },
    ]
    assert guided["wizard"]["human_pause_points"] == [
        {
            "id": "provider_keys",
            "label": "Provider key sources",
            "reason": (
                "Only collect env var names in the wizard; enter raw provider keys "
                "through General Augment custody."
            ),
            "command": "genaug providers setup ... --health-check",
        },
        {
            "id": "production_deploy",
            "label": "Production deploy consent",
            "reason": (
                "Deploy, publish, billing, and destructive tools stay approval gated "
                "until explicitly enabled."
            ),
            "command": "Review policy gates in the dashboard before enabling deploy tools.",
        },
        {
            "id": "migration_apply",
            "label": "Migration apply and PR",
            "reason": (
                "Review the generated diff before applying code changes "
                "or opening a pull request."
            ),
            "command": (
                "genaug migrate openai-responses --apply --yes "
                "--branch genaug/openai-responses-migration --push --create-pr"
            ),
        },
    ]
    assert guided["wizard"]["review_checklist"] == [
        {
            "id": "browser_auth",
            "label": "Browser auth",
            "status": "next",
            "command": "genaug auth login",
        },
        {
            "id": "project_bootstrap",
            "label": "Project and runtime key",
            "status": "next",
            "command": (
                "genaug setup --bootstrap --project-name 'Telegram Sites' "
                "--project-slug telegram-sites --print-env"
            ),
        },
        {
            "id": "provider_custody",
            "label": "Provider credentials and health",
            "status": "needs_env",
            "command": "genaug providers setup ... --health-check",
        },
        {
            "id": "provider_smokes",
            "label": "Provider launch evidence",
            "status": "needs_env",
            "command": "genaug providers smoke ...",
        },
        {
            "id": "browser_action_scaffold",
            "label": "Browser action starter",
            "status": "next",
            "command": (
                "genaug browser-runs scaffold-function --project <project> "
                "--output browserbase-functions --json"
            ),
        },
        {
            "id": "browser_action_deployment",
            "label": "Browser action deployment evidence",
            "status": "needs_provider",
            "command": (
                "genaug browser-runs update-action-deployment --project <project> "
                "--name <browser-action-name> --deployment-status published "
                "--deployment-version-id <browserbase-function-version-id> "
                "--deployment-source-ref <git-sha> --json"
            ),
        },
        {
            "id": "skills",
            "label": "Skills and prompt flow",
            "status": "next",
            "command": (
                "genaug skills design --job-type website-builder "
                "--project <project> --apply"
            ),
        },
        {
            "id": "migration",
            "label": "OpenAI Responses migration PR",
            "status": "planned",
            "command": (
                "genaug migrate openai-responses --apply --yes "
                "--branch genaug/openai-responses-migration --push --create-pr"
            ),
        },
        {
            "id": "channel_setup",
            "label": "Telegram channel setup",
            "status": "needs_env",
            "command": (
                "genaug channels connect --project <project> --channel telegram "
                "--webhook-base-url <api-base-url>"
            ),
        },
        {
            "id": "smoke",
            "label": "Responses smoke",
            "status": "next",
            "command": "genaug smoke --project <project> --json",
        },
        {
            "id": "smoke_eval",
            "label": "Tenant smoke eval gate",
            "status": "next",
            "command": (
                "genaug evals create-smoke --output "
                "tests/fixtures/agent_evals/tenant_smoke.json && "
                "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json "
                "--mode hosted --project <project> --gate --wait --fail-on-fail --json && "
                "genaug evals release-gate --project <project> --change-type hermes_upgrade "
                "--change-type prompt_template --change-type tool_generator "
                "--change-type guardrail_change --change-type memory_policy "
                "--change-type browser_action --change-type model_replay --fail-on-fail --json"
            ),
        },
        {
            "id": "dashboard",
            "label": "Dashboard trace review",
            "status": "next",
            "command": "genaug dashboard open --project <project>",
        },
    ]
    assert guided["recommended_commands"] == [
        "genaug auth login",
        (
            "genaug setup --bootstrap --project-name 'Telegram Sites' "
            "--project-slug telegram-sites --print-env"
        ),
        (
            "genaug providers setup --provider anthropic-managed-agents --project <project> "
            "--api-key-env ANTHROPIC_API_KEY --health-check"
        ),
        (
            "genaug providers setup --provider codex-mcp --project <project> "
            "--api-key-env OPENAI_API_KEY --health-check"
        ),
        (
            "genaug providers setup --provider browserbase --project <project> "
            "--api-key-env BROWSERBASE_API_KEY --health-check"
        ),
        (
            "genaug providers setup --provider xai --project <project> "
            "--api-key-env XAI_API_KEY --health-check"
        ),
        "genaug providers readiness --project <project> --json",
        (
            "genaug providers smoke --provider anthropic-managed-agents "
            "--project <project> --api-key-env ANTHROPIC_API_KEY"
        ),
        (
            "genaug providers smoke --provider codex-mcp "
            "--project <project> --api-key-env OPENAI_API_KEY"
        ),
        (
            "genaug providers smoke --provider browserbase "
            "--project <project> --api-key-env BROWSERBASE_API_KEY"
        ),
        "genaug providers smoke --provider xai --project <project> --api-key-env XAI_API_KEY",
        (
            "genaug browser-runs scaffold-function --project <project> "
            "--output browserbase-functions --json"
        ),
        (
            "genaug browser-runs register-action --project <project> "
            "--name <browser-action-name> --function-id <browserbase-function-id> "
            "--status active --json"
        ),
        (
            "genaug browser-runs update-action-deployment --project <project> "
            "--name <browser-action-name> --deployment-status published "
            "--deployment-version-id <browserbase-function-version-id> "
            "--deployment-source-ref <git-sha> --json"
        ),
        "genaug browser-runs list-actions --project <project> --json",
        (
            "genaug browser-runs execute --project <project> "
            "--function-ref <browser-action-name> --task <task> --json"
        ),
        "genaug skills design --job-type website-builder --project <project> --apply",
        "genaug migrate openai-responses --dry-run --json",
        (
            "genaug migrate openai-responses --apply --yes "
            "--branch genaug/openai-responses-migration --push --create-pr"
        ),
        (
            "genaug channels connect --project <project> --channel telegram "
            "--webhook-base-url <api-base-url>"
        ),
        "genaug channels status --project <project> --json",
        (
            "genaug channels test --project <project> --channel telegram "
            "--chat-id <telegram-chat-id> --json"
        ),
        "genaug smoke --project <project> --json",
        "genaug evals create-smoke --output tests/fixtures/agent_evals/tenant_smoke.json",
        "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json --gate --json",
        (
            "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json "
            "--mode hosted --project <project> --gate --wait --fail-on-fail --json"
        ),
        (
            "genaug evals release-gate --project <project> --change-type hermes_upgrade "
            "--change-type prompt_template --change-type tool_generator "
            "--change-type guardrail_change --change-type memory_policy "
            "--change-type browser_action --change-type model_replay --fail-on-fail --json"
        ),
        "genaug dashboard open --project <project>",
    ]


def test_setup_interactive_wizard_accepts_comma_capability_answer(tmp_path: Path) -> None:
    """Coding agents should be able to answer the capability step in one line."""
    workspace = tmp_path / "telegram-sites"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export function run(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "setup-plan.json"

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--interactive",
            "--output",
            str(plan_path),
            "--no-handoff",
        ],
        input=(
            "\n"
            "Telegram Sites\n"
            "\n"
            "Let Telegram users create polished websites.\n"
            "telegram\n"
            "code,browser,x\n"
            "ANTHROPIC_API_KEY\n"
            "OPENAI_API_KEY\n"
            "BROWSERBASE_API_KEY\n"
            "XAI_API_KEY\n"
            "website-builder\n"
            "browserbase,github\n"
            "Use the customer's brand notes and preview before deploy.\n"
            "n\n"
            "\n"
            "\n"
            "\n"
            "\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "invalid input" not in result.output.lower()
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert payload["requested_capabilities"] == ["code", "browse", "search-x"]
    assert payload["guided"]["answers"]["capabilities"] == ["code", "browse", "search-x"]


def test_setup_interactive_wizard_records_provider_key_human_pause(
    tmp_path: Path,
) -> None:
    """Coding agents can pause on unknown provider env vars without inventing secrets."""
    workspace = tmp_path / "telegram-sites"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export function run(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "setup-plan.json"

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--interactive",
            "--output",
            str(plan_path),
            "--no-handoff",
        ],
        input=(
            "\n"
            "Telegram Sites\n"
            "\n"
            "Let Telegram users create polished websites.\n"
            "telegram\n"
            "code,browse\n"
            "ask-human\n"
            "OPENAI_API_KEY\n"
            "ask-human\n"
            "website-builder\n"
            "browserbase,github\n"
            "Preview before deploy.\n"
            "n\n"
            "\n"
            "\n"
            "\n"
            "\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Human inputs required" in result.output
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    guided = payload["guided"]
    assert guided["answers"]["provider_env_vars"] == {
        "anthropic-managed-agents": "ANTHROPIC_API_KEY",
        "codex-mcp": "OPENAI_API_KEY",
        "browserbase": "BROWSERBASE_API_KEY",
    }
    assert guided["answers"]["human_inputs_required"] == [
        {
            "id": "provider_env_var:anthropic-managed-agents",
            "label": "Anthropic-Managed-Agents provider env var",
            "provider": "anthropic-managed-agents",
            "capability": "code",
            "default_env_var": "ANTHROPIC_API_KEY",
            "reason": "Coding agent requested human confirmation for the provider key source.",
        },
        {
            "id": "provider_env_var:browserbase",
            "label": "Browserbase provider env var",
            "provider": "browserbase",
            "capability": "browse",
            "default_env_var": "BROWSERBASE_API_KEY",
            "reason": "Coding agent requested human confirmation for the provider key source.",
        },
    ]
    assert guided["wizard"]["human_inputs_required"] == guided["answers"][
        "human_inputs_required"
    ]
    rendered = json.dumps(guided)
    assert "ask-human" not in rendered
    assert "sk-should-not-appear" not in rendered


def test_setup_interactive_wizard_writes_markdown_handoff(tmp_path: Path) -> None:
    """The guided wizard should leave a polished operator/coding-agent handoff file."""
    workspace = tmp_path / "telegram-sites"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export function run(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n",
        encoding="utf-8",
    )
    handoff_path = tmp_path / "setup-handoff.md"

    result = CliRunner().invoke(
        app,
        [
            "setup",
            "--workspace",
            str(workspace),
            "--interactive",
            "--handoff-output",
            str(handoff_path),
        ],
        input=(
            "\n"
            "Telegram Sites\n"
            "\n"
            "Let Telegram users create polished websites.\n"
            "telegram\n"
            "code,browse,x\n"
            "ANTHROPIC_API_KEY\n"
            "OPENAI_API_KEY\n"
            "BROWSERBASE_API_KEY\n"
            "XAI_API_KEY\n"
            "website-builder\n"
            "browserbase,github\n"
            "Preview before deploy. sk-should-not-appear\n"
            "n\n"
            "\n"
            "\n"
            "\n"
            "\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "Setup handoff written" in result.output
    assert handoff_path.exists()
    text = handoff_path.read_text(encoding="utf-8")
    assert "# General Augment Setup Handoff" in text
    assert "Mode: migrate" in text
    assert "Project: Telegram Sites" in text
    assert "Capabilities: code, browse, search-x" in text
    assert "## Human Pause Points" in text
    assert "Provider key sources" in text
    assert "## Launch Checklist" in text
    assert "- [ ] Browser auth" in text
    assert "## Recommended Commands" in text
    assert "genaug providers setup --provider browserbase" in text
    assert "genaug providers readiness --project <project> --json" in text
    assert "genaug browser-runs register-action --project <project>" in text
    assert "genaug browser-runs update-action-deployment --project <project>" in text
    assert "genaug browser-runs execute --project <project>" in text
    assert "genaug migrate openai-responses --apply --yes" in text
    assert "genaug evals create-smoke --output tests/fixtures/agent_evals/tenant_smoke.json" in text
    assert "genaug dashboard open --project <project>" in text
    assert "sk-should-not-appear" not in text
    assert "GENAUG_API_KEY=" not in text


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


def test_init_interactive_runs_guided_setup_wizard(tmp_path: Path) -> None:
    """Bare init should be the friendly entrypoint for the guided setup wizard."""
    workspace = tmp_path / "telegram-sites"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n"
        "const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });\n"
        "export function run(input: string) {\n"
        "  return openai.responses.create({ model: 'gpt-5.1', input });\n"
        "}\n",
        encoding="utf-8",
    )
    plan_path = tmp_path / "setup-plan.json"

    result = CliRunner().invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--interactive",
            "--output",
            str(plan_path),
        ],
        input=(
            "\n"
            "Telegram Sites\n"
            "\n"
            "Let Telegram users create polished websites.\n"
            "telegram\n"
            "code,browse\n"
            "ANTHROPIC_API_KEY\n"
            "OPENAI_API_KEY\n"
            "BROWSERBASE_API_KEY\n"
            "website-builder\n"
            "browserbase\n"
            "\n"
            "n\n"
            "\n"
            "\n"
            "\n"
            "\n"
        ),
    )

    assert result.exit_code == 0, result.output
    assert "General Augment interactive setup" in result.output
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    assert payload["guided"]["answers"]["setup_mode"] == "migrate"
    assert payload["requested_capabilities"] == ["code", "browse"]
    assert (
        "genaug providers smoke --provider browserbase --project <project> "
        "--api-key-env BROWSERBASE_API_KEY"
    ) in payload["guided"]["recommended_commands"]


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


def test_migrate_openai_responses_can_open_pull_request(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Migration apply should be able to branch, commit, push, and open a PR on request."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    (workspace / "agent.ts").write_text(
        "import OpenAI from 'openai';\n\n"
        "const openai = new OpenAI({\n"
        "  apiKey: process.env.OPENAI_API_KEY,\n"
        "});\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path) -> str:
        calls.append(command)
        if command[:3] == ["gh", "pr", "create"]:
            return "https://github.com/example/app/pull/17"
        return ""

    monkeypatch.setattr(migrate_command, "_run_command", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "migrate",
            "openai-responses",
            "--workspace",
            str(workspace),
            "--apply",
            "--yes",
            "--branch",
            "genaug/openai-responses-migration",
            "--commit-message",
            "Migrate OpenAI Responses calls to General Augment",
            "--push",
            "--create-pr",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pull_request"] == {
        "requested": True,
        "branch": "genaug/openai-responses-migration",
        "commit_message": "Migrate OpenAI Responses calls to General Augment",
        "pushed": True,
        "url": "https://github.com/example/app/pull/17",
    }
    assert ["git", "switch", "-c", "genaug/openai-responses-migration"] in calls
    assert ["git", "add", "agent.ts", ".env.example"] in calls
    assert [
        "git",
        "commit",
        "-m",
        "Migrate OpenAI Responses calls to General Augment",
    ] in calls
    assert ["git", "push", "-u", "origin", "genaug/openai-responses-migration"] in calls
    assert [
        "gh",
        "pr",
        "create",
        "--fill",
        "--title",
        "Migrate OpenAI Responses calls to General Augment",
        "--body",
        "Generated by `genaug migrate openai-responses`.",
    ] in calls


def _migrate(workspace: Path, *extra: str) -> Result:
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
    assert "agent.ts" not in payload["migration"]["diff_files"]


def test_migrate_env_var_without_client_is_not_clobbered(tmp_path: Path) -> None:
    """A file that mentions OPENAI_API_KEY but constructs no client must exit non-zero."""
    workspace = tmp_path / "app"
    workspace.mkdir()
    source = workspace / "config.ts"
    original = "export const key = process.env.OPENAI_API_KEY;\n"
    source.write_text(original, encoding="utf-8")

    result = _migrate(workspace)

    payload = json.loads(result.output)
    assert source.read_text(encoding="utf-8") == original
    # No client constructed anywhere -> nothing changed, honest non-success exit.
    assert payload["migration"]["changed"] is False
    assert result.exit_code != 0


def test_setup_subcommands_are_agent_friendly_json(tmp_path: Path) -> None:
    """Provider, connector, skill, and dashboard helpers should expose machine-readable setup."""
    workspace = tmp_path / "app"
    workspace.mkdir()

    providers = CliRunner().invoke(
        app,
        [
            "providers",
            "setup",
            "--capability",
            "code",
            "--provider",
            "codex-mcp",
            "--provider",
            "fal",
            "--json",
        ],
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
    assert [item["provider"] for item in json.loads(providers.output)["providers"]] == [
        "codex-mcp",
        "fal",
    ]
    assert json.loads(providers.output)["providers"][1]["credential_kind"] == "model_provider"
    assert json.loads(connectors.output)["connectors"][0]["setup_command"].startswith(
        "genaug mcp add"
    )
    assert json.loads(skills.output)["skill"]["name"] == "Website Builder"
    assert (
        json.loads(dashboard.output)["url"]
        == "https://app.generalaugment.com/dashboard/projects/demo-agent"
    )


def test_providers_setup_capability_plan_surfaces_all_supported_code_and_video_options() -> None:
    """Capability planning should expose supported provider options, not a hidden default."""
    result = CliRunner().invoke(
        app,
        [
            "providers",
            "setup",
            "--capability",
            "code",
            "--capability",
            "video",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    providers = json.loads(result.output)["providers"]
    assert [item["provider"] for item in providers] == [
        "anthropic-managed-agents",
        "codex-mcp",
        "xai",
        "fal",
        "veo",
    ]
    assert [item["credential_kind"] for item in providers] == [
        "managed_agent_provider",
        "external_mcp_provider",
        "model_provider",
        "model_provider",
        "model_provider",
    ]
    assert [item["api_key_env"] for item in providers] == [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "XAI_API_KEY",
        "FAL_API_KEY",
        "GEMINI_API_KEY",
    ]
