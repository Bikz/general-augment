"""Self-serve onboarding artifact builders."""

from __future__ import annotations

import json
import os
import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

from platform_cli.config import DEFAULT_BASE_URL, CLIConfig, save_config
from platform_cli.errors import CLIError
from platform_cli.workspace_inspector import inspect_workspace

SCHEMA_VERSION = "general-augment-self-serve-setup/v1"
DEFAULT_DASHBOARD_URL = "https://app.generalaugment.com"
DEFAULT_ARTIFACT_DIR_NAME = ".genaug"
LAUNCH_READINESS_RELEASE_GATE_CHANGE_TYPES = (
    "hermes_upgrade",
    "prompt_template",
    "tool_generator",
    "guardrail_change",
    "memory_policy",
    "browser_action",
    "model_replay",
)

PROVIDER_RECIPES: dict[str, dict[str, Any]] = {
    "anthropic-managed-agents": {
        "capability": "code",
        "provider": "anthropic-managed-agents",
        "credential_kind": "managed_agent_provider",
        "credential_custody": "general_augment",
        "setup_command": "genaug providers setup --provider anthropic-managed-agents",
        "health_command": (
            "genaug providers setup --provider anthropic-managed-agents --health-check"
        ),
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "browserbase": {
        "capability": "browse",
        "provider": "browserbase",
        "credential_kind": "external_mcp_provider",
        "credential_custody": "general_augment",
        "setup_command": "genaug providers setup --provider browserbase",
        "health_command": "genaug providers setup --provider browserbase --health-check",
        "api_key_env": "BROWSERBASE_API_KEY",
    },
    "codex-mcp": {
        "capability": "code",
        "provider": "codex-mcp",
        "credential_kind": "external_mcp_provider",
        "credential_custody": "general_augment",
        "setup_command": "genaug providers setup --provider codex-mcp",
        "health_command": "genaug providers setup --provider codex-mcp --health-check",
        "api_key_env": "OPENAI_API_KEY",
    },
    "xai": {
        "capability": "search-x",
        "provider": "xai",
        "credential_kind": "model_provider",
        "credential_custody": "general_augment",
        "setup_command": "genaug providers setup --provider xai",
        "health_command": "genaug model-providers health xai --project <project>",
        "api_key_env": "XAI_API_KEY",
        "api_mode": "codex_responses",
        "model_prefixes": ["xai/", "grok-"],
    },
    "fal": {
        "capability": "video",
        "provider": "fal",
        "credential_kind": "model_provider",
        "credential_custody": "general_augment",
        "setup_command": "genaug providers setup --provider fal",
        "health_command": "genaug model-providers health fal --project <project>",
        "api_key_env": "FAL_API_KEY",
        "base_url": "https://queue.fal.run",
        "api_mode": "codex_responses",
        "model_prefixes": ["fal/", "fal-ai/"],
    },
    "veo": {
        "capability": "video",
        "provider": "veo",
        "credential_kind": "model_provider",
        "credential_custody": "general_augment",
        "setup_command": "genaug providers setup --provider veo",
        "health_command": "genaug model-providers health veo --project <project>",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_mode": "chat_completions",
        "model_prefixes": ["veo-", "google/veo-", "models/veo-"],
    },
}

PROVIDER_ALIASES = {
    "anthropic": "anthropic-managed-agents",
    "anthropic-managed": "anthropic-managed-agents",
    "anthropic-managed-agent": "anthropic-managed-agents",
    "anthropic-managed-agents": "anthropic-managed-agents",
    "browse": "browserbase",
    "browse-sh": "browserbase",
    "browserbase": "browserbase",
    "codex": "codex-mcp",
    "codex-mcp": "codex-mcp",
    "openai-codex": "codex-mcp",
    "openai-codex-mcp": "codex-mcp",
    "x": "xai",
    "x-ai": "xai",
    "xai": "xai",
    "fal": "fal",
    "fal-ai": "fal",
    "veo": "veo",
    "google-veo": "veo",
    }

CAPABILITY_PROVIDER_OPTIONS: dict[str, tuple[str, ...]] = {
    "code": ("anthropic-managed-agents", "codex-mcp"),
    "coding": ("anthropic-managed-agents", "codex-mcp"),
    "browse": ("browserbase",),
    "browser": ("browserbase",),
    "search-x": ("xai",),
    "x-search": ("xai",),
    "x_search": ("xai",),
    "video": ("xai", "fal", "veo"),
    "video-generation": ("xai", "fal", "veo"),
    "video_gen": ("xai", "fal", "veo"),
}

CAPABILITY_ALIASES = {
    "agent-code": "code",
    "build": "code",
    "coder": "code",
    "coding": "code",
    "site-builder": "code",
    "website": "code",
    "website-builder": "code",
    "websites": "code",
    "browse-sh": "browse",
    "browse.sh": "browse",
    "browser": "browse",
    "browserbase": "browse",
    "hosted-browser": "browse",
    "search": "search-x",
    "twitter": "search-x",
    "x": "search-x",
    "x-search": "search-x",
    "x_search": "search-x",
    "xai": "search-x",
    "generate-video": "video",
    "video-generation": "video",
    "video_gen": "video",
    "videos": "video",
}


def build_setup_payload(
    *,
    workspace: Path,
    config: CLIConfig,
    requested_capabilities: list[str],
    project: str | None = None,
    bootstrap: dict[str, Any] | None = None,
    guided: dict[str, Any] | None = None,
    migration: dict[str, Any] | None = None,
    mode: str = "setup",
) -> dict[str, Any]:
    """Build a deterministic, redacted setup or migration artifact."""
    inspected = inspect_workspace(workspace)
    guided_payload = guided_setup_payload(guided) if guided is not None else None
    guided_capabilities = []
    if guided_payload is not None:
        capabilities = guided_payload["answers"].get("capabilities", [])
        if isinstance(capabilities, list):
            guided_capabilities = [str(item) for item in capabilities]
    normalized_capabilities = normalize_capabilities(requested_capabilities or guided_capabilities)
    provider_recipes = provider_setup_recipes(normalized_capabilities)
    connector_recipes = connector_setup_recipes(workspace)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "workspace": inspected["workspace"],
        "detected": inspected["detected"],
        "auth": auth_summary(config),
        "target": {
            "project_ref": project or config.active_project,
            "api_base_url": config.base_url or DEFAULT_BASE_URL,
            "dashboard_url": dashboard_project_url(project or config.active_project),
        },
        "requested_capabilities": normalized_capabilities,
        "providers": provider_recipes,
        "connectors": connector_recipes,
        "skills": [],
        "bootstrap": bootstrap,
        "guided": guided_payload,
        "plan": setup_steps(
            auth_configured=is_auth_configured(config),
            capabilities=normalized_capabilities,
            migration=migration,
        ),
        "migration": migration,
        "evidence": {
            "doctor": None,
            "smoke": None,
            "verify": None,
            "trace_id": None,
            "response_id": None,
        },
        "secrets": {
            "raw_values_present": False,
            "required_env": [
                "GENAUG_API_KEY",
                "GENAUG_PROJECT_ID",
                "GENAUG_API_BASE_URL",
                "GENAUG_OPENAI_BASE_URL",
            ],
            "redacted": True,
        },
        "safety": {
            "code_changes_applied": bool(migration and migration.get("apply")),
            "secrets_written": False,
            "raw_provider_credentials_stored_locally": False,
        },
        "next_actions": next_actions(
            authenticated=is_auth_configured(config),
            project=project or config.active_project,
            migration=migration,
        ),
    }
    return payload


def guided_setup_payload(raw_answers: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted guided setup summary for humans and coding agents."""
    answers = _redact_secret_values(raw_answers)
    capabilities = normalize_capabilities(
        [str(item) for item in _as_list(answers.get("capabilities"))]
    )
    job_type = str(answers.get("job_type") or "custom-agent").strip() or "custom-agent"
    setup_mode = str(answers.get("setup_mode") or "setup").strip().lower() or "setup"
    project_name = str(answers.get("project_name") or "<name>").strip() or "<name>"
    project_slug = str(answers.get("project_slug") or "<slug>").strip() or "<slug>"
    primary_channel = (
        str(answers.get("primary_channel") or "web").strip().lower() or "web"
    )
    migrate_openai = bool(answers.get("migrate_openai_responses"))
    open_pull_request = bool(answers.get("open_pull_request"))
    allow_production_deploy = bool(answers.get("allow_production_deploy"))
    run_smoke = bool(answers.get("run_smoke", True))
    open_dashboard = bool(answers.get("open_dashboard", True))
    provider_env_vars = _provider_env_vars(answers.get("provider_env_vars"))
    human_inputs_required = _human_inputs_required(answers.get("human_inputs_required"))
    project_name_arg = _command_arg(project_name, "<name>")
    project_slug_arg = _command_arg(project_slug, "<slug>")
    recommended = [
        "genaug auth login",
        (
            "genaug setup --bootstrap "
            f"--project-name {project_name_arg} --project-slug {project_slug_arg} --print-env"
        ),
    ]
    provider_recipes = provider_setup_recipes(capabilities)
    recommended.extend(
        (
            "genaug providers setup "
            f"--provider {recipe['provider']} --project <project> "
            f"--api-key-env {_env_var_for_recipe(recipe, provider_env_vars)} --health-check"
        )
        for recipe in provider_recipes
    )
    if provider_recipes:
        recommended.append("genaug providers readiness --project <project> --json")
    recommended.extend(
        (
            "genaug providers smoke "
            f"--provider {recipe['provider']} --project <project> "
            f"--api-key-env {_env_var_for_recipe(recipe, provider_env_vars)}"
        )
        for recipe in provider_recipes
    )
    recommended.extend(browser_action_authoring_commands(capabilities))
    recommended.append(f"genaug skills design --job-type {job_type} --project <project> --apply")
    if migrate_openai:
        recommended.append("genaug migrate openai-responses --dry-run --json")
        if open_pull_request:
            recommended.append(
                "genaug migrate openai-responses --apply --yes "
                "--branch genaug/openai-responses-migration --push --create-pr"
            )
    recommended.extend(channel_setup_commands(primary_channel))
    if run_smoke:
        recommended.append("genaug smoke --project <project> --json")
        recommended.append(
            "genaug evals create-smoke --output tests/fixtures/agent_evals/tenant_smoke.json"
        )
        recommended.append(
            "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json --gate --json"
        )
        recommended.append(
            "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json "
            "--mode hosted --project <project> --gate --wait --fail-on-fail --json"
        )
        recommended.append(launch_readiness_release_gate_command("<project>"))
    if open_dashboard:
        recommended.append("genaug dashboard open --project <project>")
    return {
        "schema_version": "general-augment-guided-setup/v1",
        "answers": {
            **answers,
            "setup_mode": setup_mode,
            "project_name": project_name,
            "project_slug": project_slug,
            "primary_channel": primary_channel,
            "capabilities": capabilities,
            "job_type": job_type,
            "provider_env_vars": provider_env_vars,
            "human_inputs_required": human_inputs_required,
            "allow_production_deploy": allow_production_deploy,
            "migrate_openai_responses": migrate_openai,
            "open_pull_request": open_pull_request,
            "run_smoke": run_smoke,
            "open_dashboard": open_dashboard,
        },
        "recommended_commands": recommended,
        "wizard": {
            "setup_mode": setup_mode,
            "project": {"name": project_name, "slug": project_slug},
            "provider_env_vars": provider_env_vars,
            "human_inputs_required": human_inputs_required,
            "launch_review": {
                "run_smoke": run_smoke,
                "open_dashboard": open_dashboard,
            },
            "operator_review": _guided_operator_review(
                setup_mode=setup_mode,
                provider_recipes=provider_recipes,
                provider_env_vars=provider_env_vars,
                migrate_openai=migrate_openai,
                open_pull_request=open_pull_request,
                allow_production_deploy=allow_production_deploy,
                run_smoke=run_smoke,
                open_dashboard=open_dashboard,
            ),
            "question_map": _guided_question_map(),
            "human_pause_points": _guided_human_pause_points(
                provider_recipes=provider_recipes,
                migrate_openai=migrate_openai,
                open_pull_request=open_pull_request,
            ),
            "review_checklist": _guided_review_checklist(
                recommended,
                capabilities=capabilities,
                primary_channel=primary_channel,
                job_type=job_type,
                migrate_openai=migrate_openai,
                open_pull_request=open_pull_request,
                run_smoke=run_smoke,
                open_dashboard=open_dashboard,
            ),
            "steps": [
                "Authenticate with browser installer auth.",
                "Create or select the project and mint the runtime key.",
                "Store provider keys in General Augment custody from env vars.",
                "Generate skills, prompt flows, and policy gates for review.",
                "Review or apply the migration diff, opening a PR when requested.",
                "Configure the selected user channel when it is not web-only.",
                "Run /v1/responses smoke and inspect dashboard traces.",
            ],
        },
        "policy": {
            "production_deploy_default": (
                "explicit_consent_required"
                if allow_production_deploy
                else "approval_required"
            ),
            "raw_provider_keys_in_repo": False,
            "risky_tools_require_consent": True,
        },
}


def guided_answers_template(workspace: Path) -> dict[str, Any]:
    """Return a secret-free guided answers questionnaire for an existing app."""

    inspected = inspect_workspace(workspace)
    detected = inspected["detected"]
    openai_count = int(detected["openai"]["responses_api_call_count"])
    project_name = _default_project_name(workspace)
    setup_mode = "migrate" if openai_count else "setup"
    default_capabilities = ["code", "browse"]
    provider_env_vars = {
        provider_id: str(recipe["api_key_env"])
        for provider_id, recipe in PROVIDER_RECIPES.items()
    }
    return {
        "schema_version": "general-augment-guided-answers-template/v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "workspace": inspected["workspace"],
        "detected": detected,
        "instructions": [
            "Fill answers, then run next_command.",
            "Use setup mode to configure General Augment without app code changes.",
            "Use migrate mode to generate/apply an OpenAI Responses migration plan.",
            "Do not paste raw provider keys; provide env var names only.",
        ],
        "capability_options": ["code", "browse", "search-x", "video"],
        "answers": {
            "setup_mode": setup_mode,
            "project_name": project_name,
            "project_slug": _slugify(project_name),
            "agent_goal": "",
            "primary_channel": "web",
            "capabilities": default_capabilities,
            "provider_env_vars": provider_env_vars,
            "job_type": "website-builder",
            "connector_plan": "custom-mcp",
            "skill_notes": "",
            "allow_production_deploy": False,
            "migrate_openai_responses": setup_mode == "migrate",
            "open_pull_request": setup_mode == "migrate",
            "run_smoke": True,
            "open_dashboard": True,
        },
        "question_map": _guided_question_map(),
        "human_pause_points": _guided_human_pause_points(
            provider_recipes=provider_setup_recipes(["code", "browse", "search-x", "video"]),
            migrate_openai=setup_mode == "migrate",
            open_pull_request=setup_mode == "migrate",
        ),
        "security": {
            "raw_provider_keys_allowed": False,
            "raw_secrets_in_template": False,
            "stores_credentials": False,
        },
    }


def _default_project_name(workspace: Path) -> str:
    """Return a readable project name derived from the workspace."""

    name = workspace.expanduser().resolve().name.strip()
    return name.replace("-", " ").replace("_", " ").title() or "General Augment Project"


def _slugify(value: str) -> str:
    """Return a stable project slug for generated setup answers."""

    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    parts = [part for part in slug.split("-") if part]
    return "-".join(parts)[:50] or "general-augment-project"


def _guided_operator_review(
    *,
    setup_mode: str,
    provider_recipes: list[dict[str, Any]],
    provider_env_vars: dict[str, str],
    migrate_openai: bool,
    open_pull_request: bool,
    allow_production_deploy: bool,
    run_smoke: bool,
    open_dashboard: bool,
) -> dict[str, Any]:
    """Return the compact operator review card for guided setup."""

    if migrate_openai and open_pull_request:
        summary = "Configure General Augment and prepare an OpenAI Responses migration PR."
        code_changes = "pull_request_planned"
    elif migrate_openai:
        summary = "Configure General Augment and prepare an OpenAI Responses migration diff."
        code_changes = "diff_planned"
    else:
        summary = "Configure General Augment without changing app code."
        code_changes = "none"
    proof: list[dict[str, str]] = []
    if run_smoke:
        proof.append(
            {
                "id": "responses_smoke",
                "label": "Responses smoke",
                "status": "planned",
                "command": "genaug smoke --project <project> --json",
            }
        )
    if open_dashboard:
        proof.append(
            {
                "id": "dashboard_trace_review",
                "label": "Dashboard trace review",
                "status": "planned",
                "command": "genaug dashboard open --project <project>",
            }
        )
    return {
        "mode": setup_mode,
        "summary": summary,
        "code_changes": code_changes,
        "provider_credentials": [
            {
                "provider": str(recipe["provider"]),
                "capability": str(recipe["capability"]),
                "env_var": env_var,
                "status": "set" if os.getenv(env_var) else "missing",
            }
            for recipe in provider_recipes
            for env_var in [_env_var_for_recipe(recipe, provider_env_vars)]
        ],
        "proof": proof,
        "safety": [
            {
                "id": "raw_provider_keys",
                "label": "Raw provider keys stay out of repo files and setup artifacts.",
                "status": "enforced",
            },
            {
                "id": "production_deploy",
                "label": (
                    "Production deploy, billing, and destructive tools remain approval gated."
                ),
                "status": (
                    "explicit_consent_required"
                    if allow_production_deploy
                    else "approval_required"
                ),
            },
            {
                "id": "code_changes",
                "label": "App code changes require migration apply or a migration PR.",
                "status": "explicit_consent_required",
            },
        ],
    }


def _guided_question_map() -> list[dict[str, Any]]:
    """Return the question groups used by the interactive setup wizard."""

    return [
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


def _guided_human_pause_points(
    *,
    provider_recipes: list[dict[str, Any]],
    migrate_openai: bool,
    open_pull_request: bool,
) -> list[dict[str, str]]:
    """Return explicit handoff points where a coding agent should pause for a human."""

    pause_points: list[dict[str, str]] = []
    if provider_recipes:
        pause_points.append(
            {
                "id": "provider_keys",
                "label": "Provider key sources",
                "reason": (
                    "Only collect env var names in the wizard; enter raw provider keys "
                    "through General Augment custody."
                ),
                "command": "genaug providers setup ... --health-check",
            }
        )
    pause_points.append(
        {
            "id": "production_deploy",
            "label": "Production deploy consent",
            "reason": (
                "Deploy, publish, billing, and destructive tools stay approval gated "
                "until explicitly enabled."
            ),
            "command": "Review policy gates in the dashboard before enabling deploy tools.",
        }
    )
    if migrate_openai:
        pause_points.append(
            {
                "id": "migration_apply",
                "label": "Migration apply and PR",
                "reason": (
                    "Review the generated diff before applying code changes "
                    "or opening a pull request."
                ),
                "command": (
                    "genaug migrate openai-responses --apply --yes "
                    "--branch genaug/openai-responses-migration"
                    + (" --push --create-pr" if open_pull_request else "")
                ),
            }
        )
    return pause_points


def _human_inputs_required(value: object) -> list[dict[str, str]]:
    """Return redacted human-input requirements from guided answers."""

    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider") or "")
        default_env_var = str(item.get("default_env_var") or "")
        label = str(item.get("label") or "Human input")
        reason = str(item.get("reason") or "Human confirmation required.")
        capability = str(item.get("capability") or "")
        item_id = str(item.get("id") or f"human_input:{len(items) + 1}")
        items.append(
            {
                "id": item_id,
                "label": label,
                "provider": provider,
                "capability": capability,
                "default_env_var": default_env_var,
                "reason": reason,
            }
        )
    return items


def _guided_review_checklist(
    recommended: list[str],
    *,
    capabilities: list[str],
    primary_channel: str,
    job_type: str,
    migrate_openai: bool,
    open_pull_request: bool,
    run_smoke: bool,
    open_dashboard: bool,
) -> list[dict[str, str]]:
    """Return a compact operator checklist derived from guided commands."""

    bootstrap_command = _first_command(recommended, "genaug setup --bootstrap")
    checklist = [
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
            "command": bootstrap_command,
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
    ]
    if "browse" in capabilities:
        checklist.append(
            {
                "id": "browser_action_scaffold",
                "label": "Browser action starter",
                "status": "next",
                "command": (
                    "genaug browser-runs scaffold-function --project <project> "
                    "--output browserbase-functions --json"
                ),
            }
        )
        checklist.append(
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
            }
        )
    checklist.append(
        {
            "id": "skills",
            "label": "Skills and prompt flow",
            "status": "next",
            "command": f"genaug skills design --job-type {job_type} --project <project> --apply",
        },
    )
    if migrate_openai:
        checklist.append(
            {
                "id": "migration",
                "label": (
                    "OpenAI Responses migration PR"
                    if open_pull_request
                    else "OpenAI Responses migration diff"
                ),
                "status": "planned",
                "command": (
                    _first_command(
                        recommended,
                        "genaug migrate openai-responses --apply",
                    )
                    if open_pull_request
                    else "genaug migrate openai-responses --dry-run --json"
                ),
            }
        )
    channel_commands = channel_setup_commands(primary_channel)
    if channel_commands:
        checklist.append(
            {
                "id": "channel_setup",
                "label": f"{primary_channel.title()} channel setup",
                "status": "needs_env",
                "command": channel_commands[0],
            }
        )
    if run_smoke:
        checklist.append(
            {
                "id": "smoke",
                "label": "Responses smoke",
                "status": "next",
                "command": "genaug smoke --project <project> --json",
            }
        )
        checklist.append(
            {
                "id": "smoke_eval",
                "label": "Tenant smoke eval gate",
                "status": "next",
                "command": (
                    "genaug evals create-smoke --output "
                    "tests/fixtures/agent_evals/tenant_smoke.json && "
                    "genaug evals run tests/fixtures/agent_evals/tenant_smoke.json "
                    "--mode hosted --project <project> --gate --wait --fail-on-fail --json && "
                    f"{launch_readiness_release_gate_command('<project>')}"
                ),
            }
        )
    if open_dashboard:
        checklist.append(
            {
                "id": "dashboard",
                "label": "Dashboard trace review",
                "status": "next",
                "command": "genaug dashboard open --project <project>",
            }
        )
    return checklist


def _first_command(recommended: list[str], prefix: str) -> str:
    """Return the first recommended command with a prefix."""

    for command in recommended:
        if command.startswith(prefix):
            return command
    return prefix


def launch_readiness_release_gate_command(project_ref: str) -> str:
    """Return the broad release gate command required by launch readiness."""

    change_args = " ".join(
        f"--change-type {change_type}"
        for change_type in LAUNCH_READINESS_RELEASE_GATE_CHANGE_TYPES
    )
    return f"genaug evals release-gate --project {project_ref} {change_args} --fail-on-fail --json"


def write_payload(payload: dict[str, Any], output: Path | None, workspace: Path) -> Path:
    """Write a setup artifact and return the destination path."""
    if output is not None:
        path = output.expanduser()
    else:
        path = workspace.expanduser().resolve() / DEFAULT_ARTIFACT_DIR_NAME / "setup-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def artifact_dir(workspace: Path) -> Path:
    """Return the standard self-serve artifact directory for a workspace."""
    return workspace.expanduser().resolve() / DEFAULT_ARTIFACT_DIR_NAME


def auth_summary(config: CLIConfig) -> dict[str, Any]:
    """Return local auth state without exposing keys."""
    installer = installer_auth_metadata(config)
    if installer is not None:
        return {
            "status": "configured",
            "method": "installer",
            "config_profile": config.profile,
        }
    return {
        "status": "configured" if config.api_key else "not_authenticated",
        "method": "api_key" if config.api_key else None,
        "config_profile": config.profile,
    }


def is_auth_configured(config: CLIConfig) -> bool:
    """Return whether setup can use local CLI auth."""
    return bool(config.api_key) or installer_auth_metadata(config) is not None


def installer_auth_metadata(config: CLIConfig) -> dict[str, Any] | None:
    """Return installer auth metadata when a local browser session is present."""
    installer = config.metadata.get("installer")
    if not isinstance(installer, dict) or not installer.get("access_token"):
        return None
    return installer


def installer_access_token(runtime: Any) -> str:
    """Return a usable installer token, rotating an expired stored session automatically."""
    installer = installer_auth_metadata(runtime.config)
    if installer is None:
        raise CLIError("Run genaug auth login before using installer operations.")
    access_token = str(installer.get("access_token") or "")
    expires_at = _installer_expiry(installer.get("expires_at"))
    if expires_at is None or expires_at > datetime.now(UTC) + timedelta(seconds=30):
        return access_token
    refresh_token = str(installer.get("refresh_token") or "")
    if not refresh_token:
        raise CLIError("Installer auth expired. Run genaug auth login again.")
    with runtime.client() as client:
        refreshed = client.installer(
            "POST",
            "/auth/token",
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    if not isinstance(refreshed, dict) or not refreshed.get("access_token"):
        raise CLIError("Installer token refresh returned an invalid response.")
    metadata = dict(runtime.config.metadata or {})
    metadata["installer"] = {
        **installer,
        "access_token": refreshed.get("access_token"),
        "refresh_token": refreshed.get("refresh_token"),
        "expires_at": refreshed.get("expires_at"),
        "scopes": refreshed.get("scopes", installer.get("scopes", [])),
        "project_id": refreshed.get("project_id", installer.get("project_id")),
    }
    next_config = runtime.config.model_copy(update={"metadata": metadata})
    save_config(next_config, runtime.config_path)
    # Runtime is frozen, but its Pydantic config remains mutable. Keep later
    # saves in this command from restoring the consumed refresh token.
    runtime.config.metadata = metadata
    return str(refreshed["access_token"])


def _installer_expiry(value: object) -> datetime | None:
    """Parse persisted ISO-8601 expiry metadata without breaking older profiles."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def resolve_installer_project_id(client: Any, *, token: str, project_ref: str) -> str:
    """Resolve a project id, slug, or name to its UUID via the installer listing.

    Installer routes type the project path param as a UUID, so a slug/name must be
    resolved before interpolation or the backend returns 422.
    """
    if _looks_like_uuid(project_ref):
        return project_ref
    payload = client.installer("GET", "/projects", token=token)
    items = payload.get("items", []) if isinstance(payload, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if project_ref in {
            str(item.get("id", "")),
            str(item.get("slug", "")),
            str(item.get("name", "")),
        }:
            return str(item.get("id") or project_ref)
    raise CLIError(f"Project not found: {project_ref}")


def _looks_like_uuid(value: str) -> bool:
    """Return whether a project ref is already a canonical UUID."""
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def normalize_capabilities(capabilities: list[str]) -> list[str]:
    """Return requested capabilities in stable order without duplicates."""
    seen: set[str] = set()
    normalized: list[str] = []
    for capability in capabilities:
        value = capability.strip().lower()
        value = CAPABILITY_ALIASES.get(value, value)
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def browser_action_authoring_commands(capabilities: list[str]) -> list[str]:
    """Return browser action authoring and deployment commands for browser-capable agents."""
    if "browse" not in capabilities:
        return []
    return [
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
    ]


def channel_setup_commands(primary_channel: str) -> list[str]:
    """Return launch handoff commands for the selected user channel."""
    channel = primary_channel.strip().lower()
    if channel == "telegram":
        return [
            (
                "genaug channels connect --project <project> --channel telegram "
                "--webhook-base-url <api-base-url>"
            ),
            "genaug channels status --project <project> --json",
            (
                "genaug channels test --project <project> --channel telegram "
                "--chat-id <telegram-chat-id> --json"
            ),
        ]
    if channel == "whatsapp":
        return [
            (
                "genaug channels connect --project <project> --channel whatsapp "
                "--phone-number-id <whatsapp-phone-number-id>"
            ),
            "genaug channels status --project <project> --json",
        ]
    if channel == "sms":
        return [
            (
                "genaug channels connect --project <project> --channel sms "
                "--twilio-number <twilio-sender-number>"
            ),
            "genaug channels status --project <project> --json",
        ]
    return []


def provider_setup_recipes(capabilities: list[str]) -> list[dict[str, Any]]:
    """Map user capabilities to provider setup recipes."""
    recipes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for capability in capabilities:
        for provider_id in CAPABILITY_PROVIDER_OPTIONS.get(capability, ()):
            if provider_id in seen:
                continue
            seen.add(provider_id)
            recipes.append(_recipe_for_provider(provider_id, capability=capability))
    return recipes


def provider_setup_recipes_for_providers(provider_ids: list[str]) -> list[dict[str, Any]]:
    """Return setup recipes for explicit provider ids."""
    recipes: list[dict[str, Any]] = []
    for provider_id in provider_ids:
        normalized = normalize_provider_id(provider_id)
        recipes.append(_recipe_for_provider(normalized))
    return recipes


def normalize_provider_id(provider_id: str) -> str:
    """Normalize CLI provider aliases to stable provider ids."""
    normalized = provider_id.strip().casefold().replace("_", "-")
    if normalized in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[normalized]
    if normalized in PROVIDER_RECIPES:
        return normalized
    raise ValueError(f"Unsupported provider: {provider_id}")


def _recipe_for_provider(provider_id: str, *, capability: str | None = None) -> dict[str, Any]:
    """Return a copy of one provider setup recipe."""
    recipe = dict(PROVIDER_RECIPES[provider_id])
    if capability is not None:
        recipe["capability"] = capability
    return recipe


def connector_setup_recipes(workspace: Path) -> list[dict[str, str]]:
    """Return connector setup recipes inferred from the local workspace."""
    detected = inspect_workspace(workspace)["detected"]
    recipes: list[dict[str, str]] = []
    if detected["webhooks"] or detected["tools"]:
        recipes.append(
            {
                "connector": "custom-mcp",
                "setup_command": "genaug mcp add <name> --url https://your-app.example.com/mcp",
                "health_command": "genaug mcp test <name> --project <project>",
            }
        )
    if not recipes:
        recipes.append(
            {
                "connector": "custom-mcp",
                "setup_command": "genaug mcp add <name> --url https://your-app.example.com/mcp",
                "health_command": "genaug mcp test <name> --project <project>",
            }
        )
    return recipes


def skill_design_recipe(job_type: str) -> dict[str, Any]:
    """Return one starter skill design artifact for a requested job type."""
    name = (
        "Website Builder"
        if job_type == "website-builder"
        else job_type.replace("-", " ").title()
    )
    return {
        "name": name,
        "job_type": job_type,
        "questions": [
            "What should the agent be allowed to build?",
            "Which source systems or brand guidelines should it use?",
            "When should it ask for approval before preview or deploy?",
        ],
        "artifacts": [
            "skills/website-builder/SKILL.md",
            "prompt-flows/website-builder.yaml",
            "policies/website-builder.yaml",
        ],
        "boundaries": [
            "Preview before production deploy.",
            "No raw provider credentials in prompts, traces, or repo files.",
            "Hermes authors the final user-facing response.",
        ],
    }


def setup_steps(
    *,
    auth_configured: bool,
    capabilities: list[str],
    migration: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return ordered setup plan steps."""
    steps: list[dict[str, Any]] = [
        {
            "id": "authenticate_cli",
            "status": "planned" if not auth_configured else "satisfied",
            "risk": "low",
            "requires_auth": False,
            "files": [],
            "commands": ["genaug auth login"],
            "reason": "Link the local CLI to a General Augment account or API key.",
        },
        {
            "id": "select_or_create_project",
            "status": "blocked" if not auth_configured else "planned",
            "risk": "low",
            "requires_auth": True,
            "files": [],
            "commands": ["genaug projects list --json", "genaug projects create"],
            "reason": "Attach setup evidence and runtime keys to one tenant project.",
        },
    ]
    if capabilities:
        steps.append(
            {
                "id": "configure_capabilities",
                "status": "blocked" if not auth_configured else "planned",
                "risk": "medium",
                "requires_auth": True,
                "files": [],
                "commands": ["genaug providers setup", "genaug connectors setup"],
                "reason": (
                    "Store provider credentials in General Augment custody and run "
                    "health checks."
                ),
            }
        )
    if migration is not None:
        steps.append(
            {
                "id": "migrate_openai_responses",
                "status": "applied" if migration.get("apply") else "planned",
                "risk": "medium",
                "requires_auth": False,
                "files": migration.get("diff_files", []),
                "commands": ["genaug migrate openai-responses --apply --yes"],
                "reason": "Route OpenAI-compatible Responses calls through General Augment.",
            }
        )
    steps.append(
        {
            "id": "smoke_and_review",
            "status": "blocked" if not auth_configured else "planned",
            "risk": "low",
            "requires_auth": True,
            "files": [],
            "commands": [
                "genaug smoke",
                "genaug onboarding verify --json",
                "genaug dashboard open",
            ],
            "reason": "Prove Hermes sees configured tools and capture support evidence.",
        }
    )
    return steps


def next_actions(
    *,
    authenticated: bool,
    project: str | None,
    migration: dict[str, Any] | None,
) -> list[str]:
    """Return copyable next actions for humans or coding agents."""
    actions: list[str] = []
    if not authenticated:
        actions.append("genaug auth login")
    if not project:
        actions.append("genaug projects list --json")
    actions.append("genaug providers setup")
    actions.append("genaug connectors setup")
    actions.append("genaug skills design")
    if migration is not None and not migration.get("apply"):
        actions.append(
            "Review the generated diff before running "
            "genaug migrate openai-responses --apply."
        )
    actions.append("genaug smoke")
    actions.append("genaug dashboard open")
    return actions


def dashboard_base_url(base_url: str | None = None) -> str:
    """Return the configured dashboard origin without a trailing slash."""
    return (base_url or os.getenv("GENAUG_DASHBOARD_URL") or DEFAULT_DASHBOARD_URL).rstrip("/")


def dashboard_project_url(project: str | None, *, base_url: str | None = None) -> str:
    """Build a dashboard URL for a project or the project picker.

    The Next.js dashboard renders a project at /dashboard/projects/<id>, so this
    is the single source of truth every CLI command uses for project links.
    """
    base = dashboard_base_url(base_url)
    if not project:
        return f"{base}/dashboard/projects"
    return f"{base}/dashboard/projects/{quote(project, safe='')}"


def dashboard_project_section_url(
    project: str,
    section: str,
    *,
    base_url: str | None = None,
) -> str:
    """Build a canonical project-scoped dashboard section URL."""
    root = dashboard_project_url(project, base_url=base_url)
    return f"{root}/{quote(section.strip('/'), safe='/')}"


def dashboard_launch_url(
    project: str,
    session: str,
    *,
    base_url: str | None = None,
) -> str:
    """Build the exact review URL for one launch session."""
    root = dashboard_project_url(project, base_url=base_url)
    return f"{root}/launch/{quote(session, safe='')}"


def dashboard_observability_url(
    *,
    project: str | None = None,
    filters: dict[str, str] | None = None,
    base_url: str | None = None,
) -> str:
    """Build the global observability route with optional project/evidence filters."""
    params = {key: value for key, value in (filters or {}).items() if value}
    if project:
        params.setdefault("project_id", project)
    root = f"{dashboard_base_url(base_url)}/dashboard/observability"
    return f"{root}?{urlencode(params)}" if params else root


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _provider_env_vars(value: object) -> dict[str, str]:
    """Return canonical provider/capability -> env var names from guided answers."""
    if not isinstance(value, dict):
        return {}
    env_vars: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().casefold().replace("_", "-")
        if key in PROVIDER_ALIASES:
            key = PROVIDER_ALIASES[key]
        else:
            normalized = normalize_capabilities([str(raw_key)])
            if not normalized:
                continue
            key = normalized[0]
        env_name = str(raw_value).strip()
        if env_name:
            env_vars[key] = env_name
    return env_vars


def _env_var_for_recipe(recipe: dict[str, Any], env_vars: dict[str, str]) -> str:
    """Resolve a provider env var, accepting provider keys and legacy capability keys."""
    provider = str(recipe.get("provider") or "")
    capability = str(recipe.get("capability") or "")
    default = str(recipe.get("api_key_env") or "").strip() or "<ENV>"
    return env_vars.get(provider) or env_vars.get(capability) or default


def _command_arg(value: str, placeholder: str) -> str:
    """Return a copyable shell argument while preserving placeholder readability."""
    if value == placeholder:
        return placeholder
    return shlex.quote(value)


def _redact_secret_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_secret_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secret_values(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b(?:sk|xai|ghp|gho|pypi)-[A-Za-z0-9._-]+\b", "[REDACTED]", value)
    return value
