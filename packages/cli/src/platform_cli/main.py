"""Typer entrypoint for the standalone CLI package."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from platform_cli import __version__
from platform_cli.branding import get_branding
from platform_cli.commands import (
    approvals,
    auth,
    billing,
    channels,
    identity,
    keys,
    mcp,
    memory,
    model_providers,
    observability,
    onboarding,
    projects,
    skills,
    tools,
    users,
)
from platform_cli.commands.deploy import deploy
from platform_cli.commands.dev import dev
from platform_cli.commands.doctor import doctor
from platform_cli.commands.init import init
from platform_cli.commands.integrate import integrate
from platform_cli.commands.logs import logs
from platform_cli.commands.mock import mock
from platform_cli.commands.smoke import smoke
from platform_cli.commands.status import status
from platform_cli.commands.validate import validate
from platform_cli.commands.verify import verify
from platform_cli.config import apply_runtime_overrides, load_config, resolve_config_paths
from platform_cli.errors import CLIError
from platform_cli.output import print_error
from platform_cli.runtime import Runtime

branding = get_branding()
app = typer.Typer(
    help=f"{branding.product_name} developer CLI.",
    no_args_is_help=True,
    invoke_without_command=True,
)
app.add_typer(approvals.app, name="approvals")
app.add_typer(auth.app, name="auth")
app.add_typer(billing.app, name="billing")
app.add_typer(projects.app, name="projects")
app.add_typer(keys.app, name="keys")
app.add_typer(tools.app, name="tools")
app.add_typer(skills.app, name="skills")
app.add_typer(mcp.app, name="mcp")
app.add_typer(model_providers.app, name="model-providers")
app.add_typer(memory.app, name="memory")
app.add_typer(users.app, name="users")
app.add_typer(identity.app, name="identity")
app.add_typer(observability.app, name="observability")
app.add_typer(channels.app, name="channels")
app.add_typer(onboarding.app, name="onboarding")
app.command("integrate")(integrate)
app.command("init")(init)
app.command("deploy")(deploy)
app.command("dev")(dev)
app.command("doctor")(doctor)
app.command("mock")(mock)
app.command("logs")(logs)
app.command("status")(status)
app.command("smoke")(smoke)
app.command("validate")(validate)
app.command("verify")(verify)


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the CLI version and exit.",
            is_eager=True,
        ),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="Path to CLI config file."),
    ] = None,
    base_url: Annotated[
        str | None,
        typer.Option("--base-url", help="Override platform API base URL."),
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Override API key for one command."),
    ] = None,
) -> None:
    """Load CLI config and runtime overrides."""
    if version:
        typer.echo(f"genaug {__version__}")
        raise typer.Exit()
    config_paths = resolve_config_paths(config)
    loaded = apply_runtime_overrides(
        load_config(config_paths.load_path),
        base_url=base_url,
        api_key=api_key,
    )
    ctx.obj = Runtime(
        config=loaded,
        config_path=config_paths.save_path,
        loaded_config_path=config_paths.load_path,
    )


def run() -> None:
    """Invoke the CLI with friendly top-level error handling."""
    try:
        app()
    except CLIError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    run()
