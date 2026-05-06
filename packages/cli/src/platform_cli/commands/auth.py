"""Authentication commands."""

from __future__ import annotations

from typing import cast

import typer
from rich.prompt import Prompt

from platform_cli.config import clear_config, save_config
from platform_cli.output import panel, print_success
from platform_cli.runtime import Runtime

app = typer.Typer(help="Authenticate the CLI.")


@app.command("login")
def login(
    ctx: typer.Context,
    api_key: str | None = typer.Option(None, help="Admin API key."),
    base_url: str | None = typer.Option(None, help="Platform API base URL."),
    skip_verify: bool = typer.Option(
        False,
        "--skip-verify",
        help="Store the key without calling the platform API.",
    ),
) -> None:
    """Store an API key locally after verifying it can reach the API."""
    runtime = _runtime(ctx)
    key = api_key or Prompt.ask("API key", password=True)
    next_config = runtime.config.model_copy(
        update={
            "api_key": key,
            "base_url": (base_url or runtime.config.base_url).rstrip("/"),
        }
    )
    identity: dict[str, object] | None = None
    if not skip_verify:
        verify_runtime = Runtime(
            config=next_config,
            config_path=runtime.config_path,
            loaded_config_path=runtime.loaded_config_path,
        )
        with verify_runtime.client() as client:
            identity = client.admin("GET", "/me")
    path = save_config(next_config, runtime.config_path)
    print_success(f"Authenticated. Config saved to {path}")
    if identity is not None:
        print_success(
            f"Verified API access as {identity.get('auth_method', 'api_key')!s}; "
            f"projects: {_project_scope(identity)}"
        )


@app.command("logout")
def logout(ctx: typer.Context) -> None:
    """Remove local authentication."""
    runtime = _runtime(ctx)
    clear_config(runtime.config_path)
    if runtime.loaded_config_path != runtime.config_path:
        clear_config(runtime.loaded_config_path)
    print_success("Logged out.")


@app.command("whoami")
def whoami(ctx: typer.Context) -> None:
    """Show the current API identity."""
    runtime = _runtime(ctx)
    if not runtime.config.api_key:
        panel("Not authenticated", "Run genaug auth login to configure an API key.")
        return
    with runtime.client() as client:
        payload = client.admin("GET", "/me")
    panel(
        "Current identity",
        f"Base URL: {runtime.config.base_url}\n"
        f"Auth method: {payload.get('auth_method', 'unknown')}\n"
        f"Project IDs: {_project_scope(payload)}",
    )


def _runtime(ctx: typer.Context) -> Runtime:
    """Return the current runtime object."""
    return cast(Runtime, ctx.obj)


def _project_scope(identity: dict[str, object]) -> str:
    """Return a display string for project scope from /me."""
    raw_project_ids = identity.get("project_ids")
    if isinstance(raw_project_ids, list) and raw_project_ids:
        project_ids = raw_project_ids
    elif identity.get("project_id"):
        project_ids = [identity["project_id"]]
    else:
        project_ids = []
    return ", ".join(str(project_id) for project_id in project_ids) or "global"
