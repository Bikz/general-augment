"""Starter agent scaffold command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from platform_cli.commands.setup import setup as setup_command
from platform_cli.errors import CLIError
from platform_cli.openapi import scaffold_basic_agent
from platform_cli.output import print_json, print_success, print_warning, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import build_setup_payload, write_payload


def init(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(help="Agent/project name for scaffold mode, such as dayplan."),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option(help="Existing app workspace to inspect when NAME is omitted."),
    ] = Path("."),
    capability: Annotated[
        list[str] | None,
        typer.Option("--capability", help="Capability to configure when NAME is omitted."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write setup plan JSON when NAME is omitted."),
    ] = None,
    handoff_output: Annotated[
        Path | None,
        typer.Option(
            "--handoff-output",
            help="Write guided setup handoff Markdown when NAME is omitted.",
        ),
    ] = None,
    write_handoff: Annotated[
        bool,
        typer.Option(
            "--handoff/--no-handoff",
            help="Write a guided setup handoff when NAME is omitted.",
        ),
    ] = True,
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when NAME is omitted."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print setup plan JSON when NAME is omitted."),
    ] = False,
    bootstrap: Annotated[
        bool,
        typer.Option(
            "--bootstrap",
            help="Use browser installer auth to select/create a project when NAME is omitted.",
        ),
    ] = False,
    project_name: Annotated[
        str | None,
        typer.Option(help="Project name to create when bootstrapping an existing app."),
    ] = None,
    project_slug: Annotated[
        str | None,
        typer.Option(help="Project slug to create when bootstrapping an existing app."),
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
        typer.Option("--print-env", help="Print the generated runtime env block once."),
    ] = False,
    login: Annotated[
        bool,
        typer.Option(
            "--login",
            help="Run browser installer auth before --bootstrap when NAME is omitted.",
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
            help="Ask setup questions when NAME is omitted.",
        ),
    ] = False,
    answers_file: Annotated[
        Path | None,
        typer.Option(
            "--answers-file",
            help="Load guided setup answers from a secret-free JSON file when NAME is omitted.",
        ),
    ] = None,
    configure_providers: Annotated[
        bool,
        typer.Option(
            "--configure-providers",
            help=(
                "Store guided provider credentials from env vars and run health checks "
                "when NAME is omitted."
            ),
        ),
    ] = False,
    output_dir: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
    display_name: Annotated[str | None, typer.Option(help="Tenant-facing display name.")] = None,
    description: Annotated[
        str | None,
        typer.Option(help="Agent purpose shown in SOUL.md and the handoff prompt."),
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Builtin tool ID to enable, for example web_search."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing starter files.")] = False,
) -> None:
    """Create a starter agent scaffold, or inspect an existing app when NAME is omitted."""
    existing_app_setup_requested = (
        guided
        or answers_file is not None
        or project is not None
        or handoff_output is not None
        or not write_handoff
        or bootstrap
        or project_name is not None
        or project_slug is not None
        or runtime_key_name != "Self-serve app backend"
        or skip_runtime_key
        or print_env
        or login
        or authorization_code is not None
        or code_verifier is not None
        or callback is not True
        or callback_timeout != 300.0
        or open_browser is not True
        or configure_providers
    )
    if name is None:
        if existing_app_setup_requested:
            setup_command(
                ctx,
                workspace=workspace,
                project=project,
                capability=capability,
                output=output,
                handoff_output=handoff_output,
                write_handoff=write_handoff,
                bootstrap=bootstrap,
                project_name=project_name,
                project_slug=project_slug,
                runtime_key_name=runtime_key_name,
                skip_runtime_key=skip_runtime_key,
                print_env=print_env,
                login=login,
                open_browser=open_browser,
                authorization_code=authorization_code,
                code_verifier=code_verifier,
                callback=callback,
                callback_timeout=callback_timeout,
                guided=guided,
                answers_file=answers_file,
                configure_providers=configure_providers,
                json_output=json_output,
            )
            return
        runtime: Runtime = ctx.obj
        payload = build_setup_payload(
            workspace=workspace,
            config=runtime.config,
            requested_capabilities=capability or [],
        )
        artifact_path = write_payload(payload, output, workspace)
        payload["artifact_path"] = str(artifact_path)
        artifact_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if json_output:
            print_json(payload)
            return
        table(
            "General Augment setup plan",
            ["Field", "Value"],
            [
                ["Workspace", payload["workspace"]["root"]],
                ["Frameworks", ", ".join(payload["detected"]["frameworks"])],
                ["Auth", payload["auth"]["status"]],
                ["Artifact", artifact_path],
            ],
        )
        print_success("Setup plan written without changing app code or storing secrets.")
        return
    if existing_app_setup_requested:
        raise CLIError(
            "Existing-app setup options like --interactive, --bootstrap, and --login "
            "are only available when NAME is omitted."
        )
    try:
        result = scaffold_basic_agent(
            name=name,
            output_dir=output_dir,
            display_name=display_name,
            description=description,
            builtin_tools=tool,
            force=force,
        )
    except FileExistsError as exc:
        raise CLIError(str(exc)) from exc
    rows: list[list[object]] = [
        ["Manifest", result.config_path],
        ["Personality", result.soul_path],
        ["Skills", result.skills_dir],
        ["Tools", result.tools_dir],
        ["Handoff", result.agent_prompt_path],
    ]
    table("Starter agent scaffold", ["File", "Path"], rows)
    print_success(f"Generated starter agent in {result.root}")
    if result.builtin_tools:
        print_success(f"Enabled builtin tools: {', '.join(result.builtin_tools)}")
    else:
        print_warning("No builtin tools enabled yet. Use --tool or genaug tools toggle later.")
    typer.echo(f"Next: genaug dev {result.config_path} --message \"What can you help me with?\"")
    typer.echo(f"Then: genaug deploy {result.config_path}")
