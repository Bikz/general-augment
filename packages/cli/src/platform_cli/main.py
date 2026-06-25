"""Typer entrypoint for the standalone CLI package."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from platform_cli import __version__
from platform_cli.branding import get_branding
from platform_cli.commands import (
    auth,
    connectors,
    dashboard,
    keys,
    migrate,
    providers,
    skills,
    tools,
)
from platform_cli.commands.doctor import doctor
from platform_cli.commands.init import init
from platform_cli.commands.integrate import integrate
from platform_cli.commands.setup import setup
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
app.add_typer(auth.app, name="auth")
app.add_typer(keys.app, name="keys")
app.add_typer(tools.app, name="tools")
app.add_typer(skills.app, name="skills")
app.add_typer(providers.app, name="providers")
app.add_typer(connectors.app, name="connectors")
app.add_typer(migrate.app, name="migrate")
app.add_typer(dashboard.app, name="dashboard")
app.command("integrate")(integrate)
app.command("init")(init)
app.command("setup")(setup)
app.command("doctor")(doctor)
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
