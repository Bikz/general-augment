"""Authentication commands."""

from __future__ import annotations

import base64
import hashlib
import secrets
import webbrowser
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
    open_browser: bool = typer.Option(
        True,
        "--browser/--no-browser",
        help="Open the browser for installer auth when --api-key is not provided.",
    ),
    authorization_code: str | None = typer.Option(
        None,
        help="Installer authorization code from the dashboard approval page.",
    ),
    code_verifier: str | None = typer.Option(
        None,
        help="PKCE verifier for automated installer-auth tests.",
    ),
    skip_verify: bool = typer.Option(
        False,
        "--skip-verify",
        help="Store the key without calling the platform API.",
    ),
) -> None:
    """Authenticate with browser installer auth or a provided API key."""
    runtime = _runtime(ctx)
    if api_key is None:
        _browser_login(
            runtime,
            base_url=(base_url or runtime.config.base_url).rstrip("/"),
            open_browser=open_browser,
            authorization_code=authorization_code,
            code_verifier=code_verifier,
        )
        return
    key = api_key
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


def _browser_login(
    runtime: Runtime,
    *,
    base_url: str,
    open_browser: bool,
    authorization_code: str | None,
    code_verifier: str | None,
) -> None:
    """Run the browser/PKCE installer auth path."""
    verifier = code_verifier or secrets.token_urlsafe(48)
    challenge = _pkce_challenge(verifier)
    bootstrap_config = runtime.config.model_copy(update={"base_url": base_url, "api_key": None})
    bootstrap_runtime = Runtime(
        config=bootstrap_config,
        config_path=runtime.config_path,
        loaded_config_path=runtime.loaded_config_path,
    )
    with bootstrap_runtime.client() as client:
        start = client.installer(
            "POST",
            "/auth/browser/start",
            json={
                "client_name": "genaug-cli",
                "redirect_uri": "http://127.0.0.1:8765/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scopes": [
                    "projects:read",
                    "projects:write",
                    "runtime_keys:create",
                    "setup:write",
                ],
            },
        )
        authorize_url = str(start.get("authorize_url", ""))
        print_success("Browser authorization started.")
        typer.echo(f"Open: {authorize_url}")
        if open_browser and authorize_url:
            webbrowser.open(authorize_url)
        code = authorization_code or Prompt.ask("Authorization code")
        token = client.installer(
            "POST",
            "/auth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
            },
        )
        identity = client.installer("GET", "/me", token=str(token.get("access_token", "")))
    metadata = dict(runtime.config.metadata or {})
    metadata["installer"] = {
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "expires_at": token.get("expires_at"),
        "scopes": token.get("scopes", []),
        "project_id": token.get("project_id"),
        "auth_method": "installer",
        "clerk_user_id": identity.get("clerk_user_id"),
        "clerk_email": identity.get("clerk_email"),
    }
    next_config = runtime.config.model_copy(
        update={
            "api_key": None,
            "base_url": base_url,
            "active_project": token.get("project_id") or runtime.config.active_project,
            "metadata": metadata,
        }
    )
    path = save_config(next_config, runtime.config_path)
    print_success(f"Authenticated with browser installer auth. Config saved to {path}")
    print_success(f"Installer projects: {_project_scope(identity)}")


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
    installer = runtime.config.metadata.get("installer", {})
    if not runtime.config.api_key and isinstance(installer, dict) and installer.get("access_token"):
        with runtime.client() as client:
            payload = client.installer("GET", "/me", token=str(installer["access_token"]))
        panel(
            "Current identity",
            f"Base URL: {runtime.config.base_url}\n"
            f"Auth method: {payload.get('auth_method', 'installer')}\n"
            f"Project IDs: {_project_scope(payload)}",
        )
        return
    if not runtime.config.api_key:
        panel("Not authenticated", "Run genaug auth login to authenticate.")
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


def _pkce_challenge(verifier: str) -> str:
    """Return a PKCE S256 challenge for a verifier."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
