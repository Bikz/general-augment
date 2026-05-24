"""Self-serve onboarding artifact builders."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platform_cli.config import DEFAULT_BASE_URL, CLIConfig
from platform_cli.workspace_inspector import inspect_workspace

SCHEMA_VERSION = "general-augment-self-serve-setup/v1"
DEFAULT_DASHBOARD_URL = "https://app.generalaugment.com"
DEFAULT_ARTIFACT_DIR_NAME = ".genaug"


def build_setup_payload(
    *,
    workspace: Path,
    config: CLIConfig,
    requested_capabilities: list[str],
    project: str | None = None,
    bootstrap: dict[str, Any] | None = None,
    migration: dict[str, Any] | None = None,
    mode: str = "setup",
) -> dict[str, Any]:
    """Build a deterministic, redacted setup or migration artifact."""
    inspected = inspect_workspace(workspace)
    normalized_capabilities = normalize_capabilities(requested_capabilities)
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


def normalize_capabilities(capabilities: list[str]) -> list[str]:
    """Return requested capabilities in stable order without duplicates."""
    seen: set[str] = set()
    normalized: list[str] = []
    for capability in capabilities:
        value = capability.strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def provider_setup_recipes(capabilities: list[str]) -> list[dict[str, Any]]:
    """Map user capabilities to provider setup recipes."""
    recipes: list[dict[str, Any]] = []
    for capability in capabilities:
        if capability == "code":
            recipes.append(
                {
                    "capability": "code",
                    "provider": "anthropic-managed-agents",
                    "credential_custody": "general_augment",
                    "setup_command": "genaug providers setup --capability code",
                    "health_command": "genaug providers setup --capability code --health-check",
                }
            )
        elif capability == "browse":
            recipes.append(
                {
                    "capability": "browse",
                    "provider": "browserbase",
                    "credential_custody": "general_augment",
                    "setup_command": "genaug providers setup --capability browse",
                    "health_command": "genaug providers setup --capability browse --health-check",
                }
            )
        elif capability in {"search-x", "x-search", "x_search"}:
            recipes.append(
                {
                    "capability": "search-x",
                    "provider": "xai",
                    "credential_custody": "general_augment",
                    "setup_command": "genaug providers setup --capability search-x",
                    "health_command": "genaug providers setup --capability search-x --health-check",
                }
            )
        elif capability in {"video", "video-generation", "video_gen"}:
            recipes.append(
                {
                    "capability": "video",
                    "provider": "xai-video",
                    "credential_custody": "general_augment",
                    "setup_command": "genaug providers setup --capability video",
                    "health_command": "genaug providers setup --capability video --health-check",
                }
            )
    return recipes


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
            "commands": ["genaug projects list", "genaug projects create"],
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
        actions.append("genaug projects list")
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


def dashboard_project_url(project: str | None, *, base_url: str = DEFAULT_DASHBOARD_URL) -> str:
    """Build a dashboard URL for a project or the project picker."""
    if not project:
        return f"{base_url}/projects"
    return f"{base_url}/projects/{project}"
