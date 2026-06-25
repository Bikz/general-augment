"""Self-serve General Augment setup command."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from platform_cli.client import PlatformClient, encode_path_segment
from platform_cli.commands.auth import _browser_login
from platform_cli.config import load_config, save_config
from platform_cli.errors import APIError, CLIError
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import (
    build_setup_payload,
    guided_answers_template,
    installer_auth_metadata,
    normalize_capabilities,
    provider_setup_recipes,
    write_payload,
)
from platform_cli.workspace_inspector import inspect_workspace


def setup(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option(help="App workspace to inspect."),
    ] = Path("."),
    project: Annotated[str | None, typer.Option(help="Project id, slug, or name.")] = None,
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability to configure, repeatable."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the setup plan JSON to this path."),
    ] = None,
    handoff_output: Annotated[
        Path | None,
        typer.Option(
            "--handoff-output",
            help="Write a Markdown guided-setup handoff to this path.",
        ),
    ] = None,
    write_handoff: Annotated[
        bool,
        typer.Option(
            "--handoff/--no-handoff",
            help="Write a Markdown handoff when guided setup runs in human output mode.",
        ),
    ] = True,
    bootstrap: Annotated[
        bool,
        typer.Option(
            "--bootstrap",
            help="Use browser installer auth to select/create a project and mint a runtime key.",
        ),
    ] = False,
    project_name: Annotated[
        str | None,
        typer.Option(help="Project name to create when bootstrapping."),
    ] = None,
    project_slug: Annotated[
        str | None,
        typer.Option(help="Project slug to create when bootstrapping."),
    ] = None,
    runtime_key_name: Annotated[
        str,
        typer.Option(help="Display name for the bootstrapped runtime key."),
    ] = "Self-serve app backend",
    skip_runtime_key: Annotated[
        bool,
        typer.Option("--skip-runtime-key", help="Do not mint a project runtime key."),
    ] = False,
    print_env: Annotated[
        bool,
        typer.Option(
            "--print-env",
            help="Print the generated runtime env block once; never writes it to artifacts.",
        ),
    ] = False,
    login: Annotated[
        bool,
        typer.Option(
            "--login",
            help="Run browser installer auth before --bootstrap when needed.",
        ),
    ] = False,
    open_browser: Annotated[
        bool,
        typer.Option(
            "--browser/--no-browser",
            help="Open the browser for inline installer auth when --login is used.",
        ),
    ] = True,
    authorization_code: Annotated[
        str | None,
        typer.Option(help="Installer authorization code for inline --login."),
    ] = None,
    code_verifier: Annotated[
        str | None,
        typer.Option(help="PKCE verifier for automated inline-login tests."),
    ] = None,
    callback: Annotated[
        bool,
        typer.Option(
            "--callback/--no-callback",
            help="Listen on a local loopback callback for inline --login.",
        ),
    ] = True,
    callback_timeout: Annotated[
        float,
        typer.Option(help="Seconds to wait for inline browser auth callback."),
    ] = 300.0,
    guided: Annotated[
        bool,
        typer.Option(
            "--guided",
            "--interactive",
            help="Ask setup questions or load guided answers.",
        ),
    ] = False,
    answers_file: Annotated[
        Path | None,
        typer.Option(
            "--answers-file",
            help="Load guided setup answers from a secret-free JSON file.",
        ),
    ] = None,
    answers_template: Annotated[
        Path | None,
        typer.Option(
            "--answers-template",
            help="Write a secret-free guided answers JSON template and exit.",
        ),
    ] = None,
    configure_providers: Annotated[
        bool,
        typer.Option(
            "--configure-providers",
            help=(
                "Store guided provider credentials from env vars in General Augment "
                "custody and run health checks."
            ),
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Inspect an app and produce a non-destructive General Augment setup plan."""
    runtime: Runtime = ctx.obj
    if json_output and print_env:
        raise CLIError("--print-env is only available in human output mode.")
    if json_output and login:
        raise CLIError("--login is only available in human output mode.")
    if login and not bootstrap:
        raise CLIError("--login requires --bootstrap.")
    if json_output and guided and answers_file is None and answers_template is None:
        raise CLIError("--answers-file is required with --guided --json.")
    if answers_template is not None and answers_file is not None:
        raise CLIError("Use either --answers-template or --answers-file, not both.")
    if answers_template is not None:
        _write_guided_answers_template(
            workspace=workspace,
            output=answers_template,
            json_output=json_output,
        )
        return
    guided_answers = _guided_answers(answers_file, workspace) if guided or answers_file else None
    if login:
        runtime = _run_inline_browser_login(
            runtime,
            open_browser=open_browser,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
            callback=callback,
            callback_timeout=callback_timeout,
        )
    bootstrap_payload: dict[str, object] | None = None
    runtime_env: dict[str, str] | None = None
    selected_project: str | None = project
    setup_config = runtime.config
    if bootstrap:
        bootstrap_payload, runtime_env = _bootstrap_setup(
            runtime,
            workspace=workspace,
            project=project,
            project_name=project_name,
            project_slug=project_slug,
            runtime_key_name=runtime_key_name,
            skip_runtime_key=skip_runtime_key,
        )
        project_payload = bootstrap_payload.get("project")
        if isinstance(project_payload, dict):
            selected_project = str(project_payload.get("id") or selected_project or "")
            config_update: dict[str, object] = {"active_project": selected_project}
            # Persist the minted runtime key so smoke/verify/doctor authenticate
            # without a manual `export`. The config file is chmod 600 and the raw
            # key is never written to artifacts or echoed unless --print-env asks.
            if runtime_env and runtime_env.get("GENAUG_API_KEY"):
                config_update["api_key"] = runtime_env["GENAUG_API_KEY"]
            setup_config = runtime.config.model_copy(update=config_update)
            save_config(setup_config, runtime.config_path)
    payload = build_setup_payload(
        workspace=workspace,
        config=setup_config,
        requested_capabilities=capability or [],
        project=selected_project,
        bootstrap=bootstrap_payload,
        guided=guided_answers,
    )
    guided_payload = payload.get("guided")
    if configure_providers:
        provider_setup = _configure_guided_provider_setup(
            runtime,
            payload=payload,
            project=selected_project or setup_config.active_project,
        )
        payload["provider_setup"] = provider_setup
        if isinstance(guided_payload, dict):
            guided_payload["provider_setup"] = provider_setup
    artifact_path = write_payload(payload, output, workspace)
    payload["artifact_path"] = str(artifact_path)
    handoff_path = _guided_handoff_path(
        workspace=workspace,
        handoff_output=handoff_output,
        guided_payload=guided_payload,
        json_output=json_output,
        write_handoff=write_handoff,
    )
    if handoff_path is not None and isinstance(guided_payload, dict):
        wizard = guided_payload.get("wizard")
        if isinstance(wizard, dict):
            wizard["handoff_path"] = str(handoff_path)
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if handoff_path is not None:
        _write_guided_handoff(handoff_path, payload, artifact_path=artifact_path)
    if json_output:
        print_json(payload)
        return
    table(
        "General Augment setup plan",
        ["Field", "Value"],
        [
            ["Workspace", payload["workspace"]["root"]],
            ["Frameworks", ", ".join(payload["detected"]["frameworks"])],
            [
                "OpenAI Responses call sites",
                payload["detected"]["openai"]["responses_api_call_count"],
            ],
            ["Auth", payload["auth"]["status"]],
            ["Artifact", artifact_path],
        ],
    )
    print_success("Setup plan written without changing app code or storing secrets.")
    if isinstance(guided_payload, dict):
        _print_guided_summary(guided_payload)
        guided_provider_setup = guided_payload.get("provider_setup")
        if isinstance(guided_provider_setup, dict):
            _print_provider_setup_summary(guided_provider_setup)
        if handoff_path is not None:
            print_success(f"Setup handoff written to {handoff_path}")
    if print_env and runtime_env:
        print_success("Runtime env generated. It is shown once and was not written to disk.")
        typer.echo(_format_env_block(runtime_env))


def _write_guided_answers_template(
    *,
    workspace: Path,
    output: Path,
    json_output: bool,
) -> None:
    """Write the agent-friendly guided setup answers template."""

    destination = output.expanduser()
    payload = guided_answers_template(workspace)
    payload["artifact_path"] = str(destination)
    payload["next_command"] = f"genaug setup --guided --answers-file {destination} --json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if json_output:
        print_json(payload)
        return
    table(
        "Guided setup answers template",
        ["Field", "Value"],
        [
            ["Template", destination],
            ["Mode", payload["answers"]["setup_mode"]],
            [
                "Detected OpenAI Responses",
                payload["detected"]["openai"]["responses_api_call_count"],
            ],
            ["Next command", payload["next_command"]],
        ],
    )
    print_success("Guided answers template written without storing secrets.")


def _run_inline_browser_login(
    runtime: Runtime,
    *,
    open_browser: bool,
    authorization_code: str | None,
    code_verifier: str | None,
    callback: bool,
    callback_timeout: float,
) -> Runtime:
    """Run installer auth inline and return a runtime loaded from the saved config."""

    _browser_login(
        runtime,
        base_url=runtime.config.base_url.rstrip("/"),
        open_browser=open_browser,
        authorization_code=authorization_code,
        code_verifier=code_verifier,
        use_callback=callback,
        callback_timeout=callback_timeout,
    )
    return Runtime(
        config=load_config(runtime.config_path),
        config_path=runtime.config_path,
        loaded_config_path=runtime.loaded_config_path,
    )


def _configure_guided_provider_setup(
    runtime: Runtime,
    *,
    payload: dict[str, Any],
    project: str | None,
) -> dict[str, Any]:
    """Configure guided provider credentials from env vars and run health checks."""

    recipes = payload.get("providers")
    recipes = recipes if isinstance(recipes, list) else []
    guided = payload.get("guided") if isinstance(payload.get("guided"), dict) else {}
    answers = guided.get("answers") if isinstance(guided, dict) else {}
    answers = answers if isinstance(answers, dict) else {}
    provider_env_vars = answers.get("provider_env_vars")
    provider_env_vars = provider_env_vars if isinstance(provider_env_vars, dict) else {}
    items = [
        _configure_guided_provider(
            runtime,
            recipe,
            project=project,
            provider_env_vars=provider_env_vars,
        )
        for recipe in recipes
        if isinstance(recipe, dict)
    ]
    if not items:
        status = "skipped"
    elif all(item["status"] == "passed" for item in items):
        status = "passed"
    else:
        status = "blocked"
    return {
        "schema_version": "general-augment-guided-provider-setup/v1",
        "status": status,
        "providers": items,
        "security": {
            "credential_custody": "general_augment",
            "raw_secrets_in_output": False,
            "raw_provider_payloads_in_output": False,
        },
    }


def _configure_guided_provider(
    runtime: Runtime,
    recipe: dict[str, Any],
    *,
    project: str | None,
    provider_env_vars: dict[str, Any],
) -> dict[str, Any]:
    """Configure one guided provider, returning only redacted evidence."""

    from platform_cli.commands.providers import (
        _configure_provider,
        _health_blocker,
        _safe_provider_health,
    )
    from platform_cli.self_serve import _env_var_for_recipe

    provider = str(recipe.get("provider") or "")
    env_var = _env_var_for_recipe(recipe, {str(k): str(v) for k, v in provider_env_vars.items()})
    base_item = {
        "provider": provider,
        "capability": str(recipe.get("capability") or ""),
        "credential_kind": str(recipe.get("credential_kind") or ""),
        "env_var": env_var,
    }
    if not project:
        return {
            **base_item,
            "status": "blocked",
            "checks": [{"name": "project", "status": "blocked"}],
            "evidence": {},
            "blockers": ["Pass --project or run genaug setup --bootstrap first."],
        }
    secret = os.getenv(env_var)
    if not secret:
        return {
            **base_item,
            "status": "blocked",
            "checks": [{"name": "env_var", "status": "blocked"}],
            "evidence": {},
            "blockers": [f"Environment variable {env_var} is not set."],
        }
    try:
        configured = _configure_provider(
            runtime,
            recipe,
            project=project,
            api_key=secret,
            base_url=None,
            health_check=True,
        )
    except APIError as exc:
        return {
            **base_item,
            "status": "blocked",
            "checks": [{"name": "platform_api", "status": "blocked"}],
            "evidence": {"platform_api": {"status_code": exc.status_code}},
            "blockers": [
                _redacted_error(
                    f"Provider setup platform API returned {exc.status_code}: {exc}"
                )
            ],
        }
    except CLIError as exc:
        return {
            **base_item,
            "status": "blocked",
            "checks": [{"name": "provider_setup", "status": "blocked"}],
            "evidence": {},
            "blockers": [_redacted_error(str(exc))],
        }
    credential = configured.get("credential") if isinstance(configured, dict) else {}
    health = configured.get("health") if isinstance(configured, dict) else {}
    health_passed = isinstance(health, dict) and health.get("status") == "available"
    blockers = [] if health_passed else [_health_blocker(health)]
    return {
        **base_item,
        "status": "passed" if health_passed else "blocked",
        "checks": [
            {"name": "credential_custody", "status": "passed"},
            {"name": "provider_health", "status": "passed" if health_passed else "blocked"},
        ],
        "evidence": {
            "credential": _safe_provider_credential(credential),
            "provider_health": _safe_provider_health(health),
        },
        "blockers": blockers,
    }


def _safe_provider_credential(payload: object) -> dict[str, object]:
    """Return only credential metadata that is safe for setup artifacts."""

    if not isinstance(payload, dict):
        return {}
    allowed = {
        "api_mode",
        "base_url_configured",
        "created_at",
        "credential_kind",
        "label",
        "last_validated_at",
        "model_prefixes",
        "provider",
        "status",
        "updated_at",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _redacted_error(message: str) -> str:
    """Bound and redact provider setup errors before writing evidence."""

    redacted = message
    for pattern in (
        r"\b(?:sk|xai|ghp|gho|pypi)-[A-Za-z0-9._-]+\b",
        r"\b(?:bb|fal|ga)[A-Za-z0-9._-]{16,}\b",
    ):
        redacted = re.sub(pattern, "[REDACTED]", redacted)
    return redacted if len(redacted) <= 300 else f"{redacted[:297]}..."


def _guided_answers(answers_file: Path | None, workspace: Path) -> dict[str, Any]:
    """Collect guided setup answers without asking for provider secrets."""
    if answers_file is not None:
        try:
            loaded = json.loads(answers_file.expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            raise CLIError(f"Could not read guided answers file: {answers_file}") from exc
        except json.JSONDecodeError as exc:
            raise CLIError(f"Guided answers file is not valid JSON: {answers_file}") from exc
        if not isinstance(loaded, dict):
            raise CLIError("Guided answers file must contain a JSON object.")
        return loaded
    inspected = inspect_workspace(workspace)
    detected = inspected["detected"]
    openai_count = int(detected["openai"]["responses_api_call_count"])
    default_project_name = _default_project_name(workspace)
    default_setup_mode = "migrate" if openai_count else "setup"
    typer.echo("General Augment interactive setup")
    typer.echo("This wizard writes a setup plan first; it will not edit app code or save secrets.")
    typer.echo(
        "Do not paste API keys here; provide env var names and add keys "
        "via providers setup."
    )
    typer.echo(
        "Detected: "
        f"{', '.join(detected['frameworks']) or 'unknown framework'}, "
        f"{openai_count} OpenAI Responses call site(s)."
    )
    typer.echo("\nStep 1 of 6: project and app intent")
    setup_mode = _prompt_choice(
        "Setup mode",
        choices={"setup", "migrate"},
        default=default_setup_mode,
    )
    project_name = typer.prompt("Project name", default=default_project_name)
    project_slug = typer.prompt("Project slug", default=_slugify(project_name))
    agent_goal = typer.prompt("What should this app's agent help users do?")
    primary_channel = typer.prompt("Primary user channel", default="web")

    typer.echo("\nStep 2 of 6: capabilities")
    capabilities = _capability_answers(
        default_code=True,
        default_browse=True,
        default_search=False,
        default_video=False,
    )

    typer.echo("\nStep 3 of 6: provider custody")
    provider_env_vars, human_inputs_required = _provider_env_var_answers(capabilities)

    typer.echo("\nStep 4 of 6: skills and connectors")
    job_type = typer.prompt("Starter skill/job type", default="website-builder")
    connector_plan = typer.prompt(
        "Connectors or MCP servers to prepare",
        default="custom-mcp",
    )
    skill_notes = typer.prompt(
        "Skill, brand, or policy notes (optional)",
        default="",
        show_default=False,
    )

    typer.echo("\nStep 5 of 6: safety boundaries")
    allow_production_deploy = typer.confirm(
        "Allow production deploy tools during first setup?",
        default=False,
    )
    migrate_openai = typer.confirm(
        "Plan an OpenAI Responses migration for this repo?",
        default=setup_mode == "migrate" or openai_count > 0,
    )
    open_pull_request = (
        typer.confirm("Open a PR when applying the migration?", default=True)
        if migrate_openai
        else False
    )

    typer.echo("\nStep 6 of 6: proof")
    run_smoke = typer.confirm("Run a /v1/responses smoke after setup?", default=True)
    open_dashboard = typer.confirm("Open dashboard traces for review?", default=True)
    return {
        "setup_mode": setup_mode,
        "project_name": project_name,
        "project_slug": project_slug,
        "agent_goal": agent_goal,
        "primary_channel": primary_channel,
        "capabilities": capabilities,
        "job_type": job_type,
        "provider_env_vars": provider_env_vars,
        "human_inputs_required": human_inputs_required,
        "provider_plan": (
            "General Augment will recommend providers from the selected capabilities "
            "and use the supplied env var names for provider custody commands."
        ),
        "connector_plan": connector_plan,
        "skill_notes": skill_notes,
        "allow_production_deploy": allow_production_deploy,
        "migrate_openai_responses": migrate_openai,
        "open_pull_request": open_pull_request,
        "run_smoke": run_smoke,
        "open_dashboard": open_dashboard,
    }


def _guided_handoff_path(
    *,
    workspace: Path,
    handoff_output: Path | None,
    guided_payload: object,
    json_output: bool,
    write_handoff: bool,
) -> Path | None:
    """Return where to write a guided handoff, if requested."""

    if not isinstance(guided_payload, dict):
        return None
    if not write_handoff and handoff_output is None:
        return None
    if json_output and handoff_output is None:
        return None
    if handoff_output is not None:
        return handoff_output.expanduser()
    return workspace.expanduser().resolve() / ".genaug" / "setup-handoff.md"


def _write_guided_handoff(
    path: Path,
    payload: dict[str, Any],
    *,
    artifact_path: Path,
) -> None:
    """Write a Markdown handoff for a human operator or local coding agent."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _render_guided_handoff(payload, artifact_path=artifact_path),
        encoding="utf-8",
    )


def _render_guided_handoff(payload: dict[str, Any], *, artifact_path: Path) -> str:
    """Render a deterministic guided setup handoff."""

    guided = payload.get("guided") if isinstance(payload.get("guided"), dict) else {}
    answers = guided.get("answers") if isinstance(guided, dict) else {}
    wizard = guided.get("wizard") if isinstance(guided, dict) else {}
    commands = guided.get("recommended_commands") if isinstance(guided, dict) else []
    policy = guided.get("policy") if isinstance(guided, dict) else {}
    answers = answers if isinstance(answers, dict) else {}
    wizard = wizard if isinstance(wizard, dict) else {}
    commands = commands if isinstance(commands, list) else []
    policy = policy if isinstance(policy, dict) else {}
    operator_review = wizard.get("operator_review")
    operator_review = operator_review if isinstance(operator_review, dict) else {}
    raw_capabilities = answers.get("capabilities")
    capabilities = raw_capabilities if isinstance(raw_capabilities, list) else []
    raw_provider_env_vars = answers.get("provider_env_vars")
    provider_env_vars = raw_provider_env_vars if isinstance(raw_provider_env_vars, dict) else {}
    human_inputs_required = wizard.get("human_inputs_required")
    human_inputs_required = (
        human_inputs_required if isinstance(human_inputs_required, list) else []
    )
    production_deploy_default = policy.get(
        "production_deploy_default",
        "approval_required",
    )
    lines = [
        "# General Augment Setup Handoff",
        "",
        f"Generated: {payload.get('generated_at', '')}",
        f"Setup artifact: {artifact_path}",
        "",
        "## Summary",
        "",
        f"- Mode: {answers.get('setup_mode', 'setup')}",
        f"- Project: {answers.get('project_name', '')}",
        f"- Project slug: {answers.get('project_slug', '')}",
        f"- Primary channel: {answers.get('primary_channel', '')}",
        f"- Capabilities: {', '.join(str(item) for item in capabilities)}",
        f"- Starter skill/job type: {answers.get('job_type', '')}",
        f"- Production deploy tools: {production_deploy_default}",
        "",
        "## Provider Key Custody",
        "",
    ]
    if provider_env_vars:
        lines.extend(
            f"- {provider}: read from `{env_var}` and store in General Augment custody."
            for provider, env_var in provider_env_vars.items()
        )
    else:
        lines.append("- No provider keys selected yet.")
    lines.extend(
        [
            "- Do not paste raw provider keys into this handoff, app code, env examples, "
            "or MCP URLs.",
            "",
            "## Human Inputs Required",
            "",
        ]
    )
    if human_inputs_required:
        for item in human_inputs_required:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('label', '')}: confirm `{item.get('default_env_var', '')}`."
            )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Setup Review",
            "",
            f"- Summary: {operator_review.get('summary', '')}",
            f"- Code changes: {operator_review.get('code_changes', 'none')}",
            "",
            "## Human Pause Points",
            "",
        ]
    )
    pause_points = wizard.get("human_pause_points")
    if isinstance(pause_points, list) and pause_points:
        for item in pause_points:
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"- {item.get('label', '')}",
                    f"  Reason: {item.get('reason', '')}",
                    f"  Command: `{item.get('command', '')}`",
                ]
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Launch Checklist", ""])
    checklist = wizard.get("review_checklist")
    if isinstance(checklist, list) and checklist:
        for item in checklist:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [ ] {item.get('label', '')}: `{item.get('command', '')}`"
            )
    else:
        lines.append("- [ ] Review the setup artifact and run `genaug smoke --json`.")
    lines.extend(["", "## Recommended Commands", ""])
    for command in commands:
        lines.extend(["```bash", str(command), "```", ""])
    lines.extend(
        [
            "## Review Boundary",
            "",
            "- Setup and provider commands may create hosted project configuration.",
            "- Migration commands only edit app code when `--apply` is used.",
            "- Production deploy, publish, billing, and destructive tools remain approval gated.",
            "- Dashboard review should confirm traces, usage, provider health, and "
            "launch smoke evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def _print_guided_summary(guided_payload: dict[str, Any]) -> None:
    """Print the interactive wizard handoff in a human-scannable shape."""
    answers = guided_payload.get("answers", {})
    commands = guided_payload.get("recommended_commands", [])
    if not isinstance(answers, dict):
        answers = {}
    if not isinstance(commands, list):
        commands = []
    capabilities = answers.get("capabilities", [])
    if not isinstance(capabilities, list):
        capabilities = []
    typer.echo("\nGuided setup summary")
    table(
        "Wizard choices",
        ["Field", "Value"],
        [
            ["Mode", answers.get("setup_mode", "setup")],
            ["Project", answers.get("project_name", "")],
            ["Capabilities", ", ".join(str(item) for item in capabilities)],
            ["Job type", answers.get("job_type", "")],
            [
                "Migration PR",
                "yes" if answers.get("open_pull_request") else "no",
            ],
            [
                "Production deploy tools",
                "consent required"
                if answers.get("allow_production_deploy")
                else "approval gated",
            ],
        ],
    )
    provider_env_vars = answers.get("provider_env_vars")
    if isinstance(provider_env_vars, dict) and provider_env_vars:
        table(
            "Provider key custody",
            ["Provider", "Env var"],
            [[provider, env_var] for provider, env_var in provider_env_vars.items()],
        )
    wizard = guided_payload.get("wizard")
    operator_review = wizard.get("operator_review") if isinstance(wizard, dict) else {}
    if isinstance(operator_review, dict) and operator_review:
        table(
            "Setup review",
            ["Field", "Value"],
            [
                ["Summary", operator_review.get("summary", "")],
                ["Code changes", operator_review.get("code_changes", "none")],
                [
                    "Provider env vars",
                    _provider_env_readiness(operator_review.get("provider_credentials")),
                ],
                [
                    "Proof",
                    _proof_summary(operator_review.get("proof")),
                ],
            ],
        )
    question_map = wizard.get("question_map") if isinstance(wizard, dict) else []
    if isinstance(question_map, list) and question_map:
        table(
            "Question map",
            ["Step", "Answer owner", "Captured answers"],
            [
                [
                    item.get("label", ""),
                    item.get("who_can_answer", ""),
                    ", ".join(str(key) for key in item.get("answer_keys", [])),
                ]
                for item in question_map
                if isinstance(item, dict)
            ],
        )
    pause_points = wizard.get("human_pause_points") if isinstance(wizard, dict) else []
    human_inputs_required = (
        wizard.get("human_inputs_required") if isinstance(wizard, dict) else []
    )
    if isinstance(human_inputs_required, list) and human_inputs_required:
        table(
            "Human inputs required",
            ["Input", "Default", "Reason"],
            [
                [
                    item.get("label", ""),
                    item.get("default_env_var", ""),
                    item.get("reason", ""),
                ]
                for item in human_inputs_required
                if isinstance(item, dict)
            ],
        )
    if isinstance(pause_points, list) and pause_points:
        table(
            "Human pause points",
            ["Step", "Reason", "Command"],
            [
                [
                    item.get("label", ""),
                    item.get("reason", ""),
                    item.get("command", ""),
                ]
                for item in pause_points
                if isinstance(item, dict)
            ],
        )
    checklist = wizard.get("review_checklist") if isinstance(wizard, dict) else []
    if isinstance(checklist, list) and checklist:
        table(
            "Launch checklist",
            ["Step", "Status", "Command"],
            [
                [
                    item.get("label", ""),
                    item.get("status", ""),
                    item.get("command", ""),
                ]
                for item in checklist
                if isinstance(item, dict)
            ],
        )
    typer.echo("Recommended next commands:")
    for index, command in enumerate(commands, start=1):
        typer.echo(f"{index}. {command}")


def _print_provider_setup_summary(provider_setup: dict[str, Any]) -> None:
    """Print redacted provider setup execution evidence."""

    providers = provider_setup.get("providers")
    if not isinstance(providers, list):
        return
    table(
        "Provider setup health",
        ["Provider", "Status", "Blockers"],
        [
            [
                item.get("provider", ""),
                item.get("status", ""),
                "; ".join(str(blocker) for blocker in item.get("blockers", [])) or "none",
            ]
            for item in providers
            if isinstance(item, dict)
        ],
    )


def _provider_env_readiness(value: object) -> str:
    """Return a compact provider env readiness summary for human output."""

    if not isinstance(value, list) or not value:
        return "none"
    counts = {"set": 0, "missing": 0}
    for item in value:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "missing")
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{status}: {count}" for status, count in counts.items() if count)


def _proof_summary(value: object) -> str:
    """Return a compact proof-plan summary for human output."""

    if not isinstance(value, list) or not value:
        return "none"
    labels = [str(item.get("label")) for item in value if isinstance(item, dict)]
    return ", ".join(label for label in labels if label)


def _prompt_choice(prompt: str, *, choices: set[str], default: str) -> str:
    """Prompt until the operator chooses a supported value."""
    while True:
        answer = cast(str, typer.prompt(prompt, default=default)).strip().lower()
        if answer in choices:
            return answer
        typer.echo(f"Choose one of: {', '.join(sorted(choices))}.")


def _capability_answers(
    *,
    default_code: bool,
    default_browse: bool,
    default_search: bool,
    default_video: bool,
) -> list[str]:
    """Collect capability choices in one coding-agent-friendly prompt."""

    default_capabilities = [
        capability
        for capability, enabled in (
            ("code", default_code),
            ("browse", default_browse),
            ("search-x", default_search),
            ("video", default_video),
        )
        if enabled
    ]
    default_text = ",".join(default_capabilities)
    typer.echo(
        "Enter comma-separated capabilities. Supported: code, browse, search-x, video. "
        "Aliases like website-builder, browser, and x are accepted."
    )
    while True:
        answer = cast(
            str,
            typer.prompt(
                "Capabilities to enable",
                default=default_text,
                show_default=bool(default_text),
            ),
        )
        capabilities, unsupported = _parse_capability_answer(answer)
        if not unsupported:
            return capabilities
        typer.echo(
            "Unsupported capabilities: "
            + ", ".join(unsupported)
            + ". Use code, browse, search-x, video, all, or none."
        )


def _parse_capability_answer(answer: str) -> tuple[list[str], list[str]]:
    """Parse the interactive capability answer into canonical capability ids."""

    normalized_answer = answer.strip().casefold()
    if normalized_answer in {"", "default"}:
        return [], []
    if normalized_answer in {"all", "everything"}:
        return ["code", "browse", "search-x", "video"], []
    if normalized_answer in {"none", "no", "skip"}:
        return [], []
    raw_capabilities = [
        item.strip()
        for item in re.split(r"[,/]+", answer)
        if item.strip()
    ]
    capabilities = normalize_capabilities(raw_capabilities)
    supported = {"code", "browse", "search-x", "video"}
    unsupported = [item for item in capabilities if item not in supported]
    return [item for item in capabilities if item in supported], unsupported


def _provider_env_var_answers(
    capabilities: list[str],
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Collect provider env var names only; never collect secret values."""
    env_vars: dict[str, str] = {}
    human_inputs_required: list[dict[str, str]] = []
    for recipe in provider_setup_recipes(capabilities):
        provider = str(recipe.get("provider") or "")
        capability = str(recipe.get("capability") or "")
        default = str(recipe.get("api_key_env") or _default_provider_env_var(capability))
        answer = typer.prompt(
            f"Env var that will hold the {provider} provider key",
            default=default,
        ).strip()
        if _is_human_pause_answer(answer):
            env_vars[provider] = default
            human_inputs_required.append(
                {
                    "id": f"provider_env_var:{provider}",
                    "label": f"{provider.title()} provider env var",
                    "provider": provider,
                    "capability": capability,
                    "default_env_var": default,
                    "reason": (
                        "Coding agent requested human confirmation for the provider key source."
                    ),
                }
            )
            continue
        if answer:
            env_vars[provider] = answer
    return env_vars, human_inputs_required


def _is_human_pause_answer(answer: str) -> bool:
    """Return whether an interactive answer asks the coding agent to pause."""

    return answer.strip().casefold() in {
        "ask-human",
        "ask human",
        "human",
        "pause",
        "ask",
    }


def _default_provider_env_var(capability: str) -> str:
    """Return a conventional provider key env var for a capability."""
    return {
        "code": "ANTHROPIC_API_KEY",
        "browse": "BROWSERBASE_API_KEY",
        "search-x": "XAI_API_KEY",
        "x-search": "XAI_API_KEY",
        "x_search": "XAI_API_KEY",
        "video": "XAI_API_KEY",
        "video-generation": "XAI_API_KEY",
        "video_gen": "XAI_API_KEY",
    }.get(capability, "PROVIDER_API_KEY")


def _bootstrap_setup(
    runtime: Runtime,
    *,
    workspace: Path,
    project: str | None,
    project_name: str | None,
    project_slug: str | None,
    runtime_key_name: str,
    skip_runtime_key: bool,
) -> tuple[dict[str, object], dict[str, str] | None]:
    """Bootstrap remote tenant resources through installer auth."""
    installer = installer_auth_metadata(runtime.config)
    if installer is None:
        raise CLIError("Run genaug auth login before genaug setup --bootstrap.")
    token = str(installer["access_token"])
    with runtime.client() as client:
        projects_payload = client.installer("GET", "/projects", token=token)
        project_payload = _select_or_create_project(
            client,
            token=token,
            workspace=workspace,
            projects_payload=projects_payload,
            project=project,
            project_name=project_name,
            project_slug=project_slug,
        )
        runtime_key_payload = None
        if not skip_runtime_key:
            runtime_key_payload = client.installer(
                "POST",
                f"/projects/{encode_path_segment(str(project_payload['id']))}/runtime-keys",
                token=token,
                json={"name": runtime_key_name, "scopes": ["responses:create"]},
            )
    redacted = {
        "applied": True,
        "project": _redacted_project(project_payload),
        "runtime_key": _redacted_runtime_key(runtime_key_payload)
        if runtime_key_payload is not None
        else None,
    }
    return redacted, _runtime_env(
        runtime.config.base_url,
        project_payload,
        runtime_key_payload,
    )


def _select_or_create_project(
    client: PlatformClient,
    *,
    token: str,
    workspace: Path,
    projects_payload: object,
    project: str | None,
    project_name: str | None,
    project_slug: str | None,
) -> dict[str, object]:
    """Select an existing installer project or create one."""
    items = _project_items(projects_payload)
    if project:
        for item in items:
            if project in {
                str(item.get("id", "")),
                str(item.get("slug", "")),
                str(item.get("name", "")),
            }:
                return item
    elif len(items) == 1 and not project_name and not project_slug:
        return items[0]
    elif len(items) > 1 and not project_name and not project_slug:
        raise CLIError("Multiple projects are available. Pass --project or --project-slug.")

    name = project_name or project or _default_project_name(workspace)
    slug = project_slug or _slugify(name)
    return cast(
        dict[str, object],
        client.installer(
            "POST",
            "/projects",
            token=token,
            json={"name": name, "slug": slug, "system_prompt": "You are a helpful agent."},
        ),
    )


def _project_items(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items", [])
    return [item for item in items if isinstance(item, dict)]


def _redacted_project(payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": payload.get("id", ""),
        "name": payload.get("name", ""),
        "slug": payload.get("slug", ""),
    }


def _redacted_runtime_key(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    return {
        "id": payload.get("id", ""),
        "name": payload.get("name", ""),
        "masked_key": payload.get("masked_key", ""),
        "project_id": payload.get("project_id", ""),
        "scopes": payload.get("scopes", []),
    }


def _runtime_env(
    base_url: str,
    project_payload: dict[str, object],
    runtime_key_payload: object,
) -> dict[str, str] | None:
    if not isinstance(runtime_key_payload, dict) or not runtime_key_payload.get("api_key"):
        return None
    api_base = base_url.rstrip("/")
    return {
        "GENAUG_API_KEY": str(runtime_key_payload["api_key"]),
        "GENAUG_PROJECT_ID": str(project_payload.get("id", "")),
        "GENAUG_API_BASE_URL": api_base,
        "GENAUG_OPENAI_BASE_URL": f"{api_base}/v1",
    }


def _format_env_block(values: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in values.items())


def _default_project_name(workspace: Path) -> str:
    name = workspace.expanduser().resolve().name.strip()
    return name.replace("-", " ").replace("_", " ").title() or "General Augment Project"


def _slugify(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
    parts = [part for part in slug.split("-") if part]
    return "-".join(parts)[:50] or "general-augment-project"
