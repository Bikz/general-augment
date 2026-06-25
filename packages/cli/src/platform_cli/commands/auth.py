"""Authentication commands."""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import queue
import secrets
import socketserver
import threading
import webbrowser
from dataclasses import dataclass
from typing import cast
from urllib.parse import parse_qs, urlparse

import typer
from rich.prompt import Prompt

from platform_cli.config import clear_config, save_config
from platform_cli.errors import CLIError
from platform_cli.output import panel, print_json, print_success, print_warning
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
    callback: bool = typer.Option(
        True,
        "--callback/--no-callback",
        help="Listen on a local loopback callback for browser auth.",
    ),
    callback_timeout: float = typer.Option(
        300.0,
        help="Seconds to wait for the browser auth callback before asking for a code.",
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
            use_callback=callback,
            callback_timeout=callback_timeout,
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
    use_callback: bool,
    callback_timeout: float,
) -> None:
    """Run the browser/PKCE installer auth path."""
    verifier = code_verifier or secrets.token_urlsafe(48)
    challenge = _pkce_challenge(verifier)
    callback: _LocalCallbackServer | None = None
    redirect_uri = "http://127.0.0.1:8765/callback"
    if use_callback and authorization_code is None:
        try:
            callback = _start_local_callback_server()
            redirect_uri = callback.redirect_uri
        except OSError as exc:
            print_warning(
                "Could not start local browser callback; falling back to authorization code "
                f"paste. ({exc})"
            )
    bootstrap_config = runtime.config.model_copy(update={"base_url": base_url, "api_key": None})
    bootstrap_runtime = Runtime(
        config=bootstrap_config,
        config_path=runtime.config_path,
        loaded_config_path=runtime.loaded_config_path,
    )
    try:
        with bootstrap_runtime.client() as client:
            start = client.installer(
                "POST",
                "/auth/browser/start",
                json={
                    "client_name": "genaug-cli",
                    "redirect_uri": redirect_uri,
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
            code = authorization_code or _browser_callback_code(
                callback,
                timeout=callback_timeout,
            )
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
    finally:
        if callback is not None:
            callback.close()
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
def whoami(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show the current API identity."""
    runtime = _runtime(ctx)
    installer = runtime.config.metadata.get("installer", {})
    if not runtime.config.api_key and isinstance(installer, dict) and installer.get("access_token"):
        with runtime.client() as client:
            payload = client.installer("GET", "/me", token=str(installer["access_token"]))
        if json_output:
            print_json(_identity_payload(runtime, payload, authenticated=True))
            return
        panel(
            "Current identity",
            f"Base URL: {runtime.config.base_url}\n"
            f"Auth method: {payload.get('auth_method', 'installer')}\n"
            f"Project IDs: {_project_scope(payload)}",
        )
        return
    if not runtime.config.api_key:
        if json_output:
            print_json(
                {
                    "authenticated": False,
                    "base_url": runtime.config.base_url,
                    "auth_method": None,
                    "project_ids": [],
                    "project_scope": "none",
                    "next_action": "genaug auth login",
                }
            )
            return
        panel("Not authenticated", "Run genaug auth login to authenticate.")
        return
    with runtime.client() as client:
        payload = client.admin("GET", "/me")
    if json_output:
        print_json(_identity_payload(runtime, payload, authenticated=True))
        return
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


def _identity_payload(
    runtime: Runtime,
    identity: dict[str, object],
    *,
    authenticated: bool,
) -> dict[str, object]:
    raw_project_ids = identity.get("project_ids")
    project_ids = (
        [str(project_id) for project_id in raw_project_ids]
        if isinstance(raw_project_ids, list)
        else []
    )
    if not project_ids and identity.get("project_id"):
        project_ids = [str(identity["project_id"])]
    return {
        "authenticated": authenticated,
        "base_url": runtime.config.base_url,
        "auth_method": str(identity.get("auth_method") or "unknown"),
        "project_id": identity.get("project_id"),
        "project_ids": project_ids,
        "project_scope": _project_scope(identity),
        "identity": identity,
    }


def _pkce_challenge(verifier: str) -> str:
    """Return a PKCE S256 challenge for a verifier."""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _browser_callback_code(callback: _LocalCallbackServer | None, *, timeout: float) -> str:
    """Return an auth code from loopback callback or terminal paste fallback."""
    if callback is None:
        return Prompt.ask("Authorization code")
    try:
        typer.echo("Waiting for browser approval to return to the CLI...")
        return callback.wait(timeout)
    except CLIError as exc:
        print_warning(f"{exc} Paste the authorization code instead.")
        return Prompt.ask("Authorization code")


@dataclass
class _LocalCallbackServer:
    """Tiny loopback callback server for browser installer auth."""

    redirect_uri: str
    server: socketserver.TCPServer
    thread: threading.Thread
    codes: queue.Queue[str]

    def wait(self, timeout: float) -> str:
        """Wait for the browser redirect to provide an authorization code."""
        try:
            return self.codes.get(timeout=timeout)
        except queue.Empty as exc:
            raise CLIError(
                f"Timed out waiting {timeout:g}s for browser authorization callback."
            ) from exc

    def close(self) -> None:
        """Stop the local callback server."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


def _start_local_callback_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> _LocalCallbackServer:
    """Start a loopback HTTP server that captures one installer authorization code."""
    codes: queue.Queue[str] = queue.Queue(maxsize=1)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/callback":
                self.send_error(404)
                return
            values = parse_qs(parsed.query)
            code = values.get("code", [""])[0]
            error = values.get("error", [""])[0]
            if error:
                _write_callback_response(self, 400, f"General Augment auth failed: {error}")
                return
            if not code:
                _write_callback_response(self, 400, "Missing General Augment authorization code.")
                return
            try:
                codes.put_nowait(code)
            except queue.Full:
                pass
            _write_callback_response(
                self,
                200,
                "General Augment CLI authentication complete. You can return to the terminal.",
            )

        def log_message(self, *_: object) -> None:
            return

    class LoopbackTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        server = LoopbackTCPServer((host, port), Handler)
    except OSError:
        if port == 0:
            raise
        server = LoopbackTCPServer((host, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_host, actual_port = cast(tuple[str, int], server.server_address)
    return _LocalCallbackServer(
        redirect_uri=f"http://{actual_host}:{actual_port}/callback",
        server=server,
        thread=thread,
        codes=codes,
    )


def _write_callback_response(
    handler: http.server.BaseHTTPRequestHandler,
    status: int,
    message: str,
) -> None:
    """Write a small browser response for the loopback auth callback."""
    body = (
        "<!doctype html><meta charset=\"utf-8\">"
        "<title>General Augment CLI</title>"
        f"<body><h1>General Augment CLI</h1><p>{html.escape(message)}</p></body>"
    ).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
