"""Connector setup helpers for self-serve onboarding."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlsplit

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import print_json, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import connector_setup_recipes, installer_auth_metadata

app = typer.Typer(help="Plan app connector setup.")


@app.command("setup")
def setup_connectors(
    ctx: typer.Context,
    workspace: Annotated[Path, typer.Option(help="App workspace to inspect.")] = Path("."),
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="MCP connector name for optional write-through."),
    ] = None,
    url: Annotated[str | None, typer.Option(help="HTTP MCP endpoint URL.")] = None,
    command: Annotated[str | None, typer.Option(help="Stdio MCP command.")] = None,
    arg: Annotated[
        list[str] | None,
        typer.Option("--arg", help="Stdio command argument. Repeatable."),
    ] = None,
    header: Annotated[
        list[str] | None,
        typer.Option("--header", help="HTTP header as key=value. Repeatable."),
    ] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Stdio environment value as key=value. Repeatable."),
    ] = None,
    include_tool: Annotated[
        list[str] | None,
        typer.Option("--include-tool", help="MCP tool name to expose. Repeatable."),
    ] = None,
    exclude_tool: Annotated[
        list[str] | None,
        typer.Option("--exclude-tool", help="MCP tool name to hide. Repeatable."),
    ] = None,
    timeout: Annotated[int | None, typer.Option(min=1, help="Runtime timeout seconds.")] = None,
    connect_timeout: Annotated[
        int | None,
        typer.Option("--connect-timeout", min=1, help="Connection timeout seconds."),
    ] = None,
    health_check: Annotated[
        bool,
        typer.Option("--health-check", help="Run a hosted MCP connector health check."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Produce connector setup steps without storing raw secrets locally."""
    runtime: Runtime = ctx.obj
    recipes = connector_setup_recipes(workspace)
    applied = None
    if name or url or command:
        applied = _configure_mcp_connector(
            runtime,
            project=project,
            name=name,
            url=url,
            command=command,
            arg=arg or [],
            header=header or [],
            env=env or [],
            include_tool=include_tool or [],
            exclude_tool=exclude_tool or [],
            timeout=timeout,
            connect_timeout=connect_timeout,
            health_check=health_check,
        )
    payload = {
        "schema_version": "general-augment-connector-setup/v1",
        "connectors": recipes,
        "applied": applied,
        "health_check_requested": health_check,
        "security": {
            "secret_safe_templates": True,
            "stored_raw_mcp_urls_with_keys": False,
        },
    }
    if json_output:
        print_json(payload)
        return
    table(
        "Connector setup",
        ["Connector", "Setup", "Health"],
        [
            [item["connector"], item["setup_command"], item["health_command"]]
            for item in payload["connectors"]
        ],
    )


def _configure_mcp_connector(
    runtime: Runtime,
    *,
    project: str | None,
    name: str | None,
    url: str | None,
    command: str | None,
    arg: list[str],
    header: list[str],
    env: list[str],
    include_tool: list[str],
    exclude_tool: list[str],
    timeout: int | None,
    connect_timeout: int | None,
    health_check: bool,
) -> dict[str, object]:
    """Write one MCP connector through admin or installer auth."""
    if not name:
        raise typer.BadParameter("Pass --name when configuring a connector.")
    if bool(url) == bool(command):
        raise typer.BadParameter("Provide exactly one transport: --url or --command.")
    if url:
        _validate_mcp_url(url)
    project_ref = project or runtime.config.active_project
    if not project_ref:
        raise CLIError("Pass --project or run genaug setup --bootstrap first.")
    connector_payload: dict[str, Any] = {"name": name, "enabled": True}
    if url:
        connector_payload["url"] = url
    if command:
        connector_payload["command"] = command
    if arg:
        connector_payload["args"] = list(arg)
    headers = _key_value_pairs(header, option="--header")
    if headers:
        connector_payload["headers"] = headers
    env_values = _key_value_pairs(env, option="--env")
    if env_values:
        connector_payload["env"] = env_values
    tools = _tools_filter(include_tool, exclude_tool)
    if tools:
        connector_payload["tools"] = tools
    if timeout is not None:
        connector_payload["timeout"] = timeout
    if connect_timeout is not None:
        connector_payload["connect_timeout"] = connect_timeout

    installer = installer_auth_metadata(runtime.config)
    with runtime.client() as client:
        if installer is not None:
            token = str(installer["access_token"])
            project_id = str(project_ref)
            server = client.installer(
                "POST",
                f"/projects/{encode_path_segment(project_id)}/mcp-servers",
                token=token,
                json=connector_payload,
            )
            health = (
                client.installer(
                    "POST",
                    (
                        f"/projects/{encode_path_segment(project_id)}/mcp-servers/"
                        f"{encode_path_segment(name)}/test"
                    ),
                    token=token,
                )
                if health_check
                else None
            )
        else:
            project_payload = resolve_project(client, str(project_ref))
            project_id = str(project_payload["id"])
            server = client.admin(
                "POST",
                f"/projects/{encode_path_segment(project_id)}/mcp-servers",
                json=connector_payload,
            )
            health = (
                client.admin(
                    "POST",
                    (
                        f"/projects/{encode_path_segment(project_id)}/mcp-servers/"
                        f"{encode_path_segment(name)}/test"
                    ),
                )
                if health_check
                else None
            )
    return {"project_id": project_id, "server": server, "health": health}


def _key_value_pairs(values: list[str], *, option: str) -> dict[str, str]:
    """Parse repeated key=value CLI flags."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter(f"{option} values must use key=value.")
        parsed[key.strip()] = value
    return parsed


def _tools_filter(include: list[str], exclude: list[str]) -> dict[str, list[str]]:
    """Return MCP tool include/exclude filters."""
    tools: dict[str, list[str]] = {}
    if include:
        tools["include"] = include
    if exclude:
        tools["exclude"] = exclude
    return tools


def _validate_mcp_url(value: str) -> None:
    """Reject raw credential query parameters in MCP URLs."""
    sensitive = {
        "api_key",
        "apikey",
        "browserbaseapikey",
        "key",
        "modelapikey",
        "token",
        "access_token",
    }
    try:
        query_items = parse_qsl(urlsplit(value).query, keep_blank_values=True)
    except ValueError as exc:
        raise typer.BadParameter("MCP --url must be a valid URL.") from exc
    for raw_key, raw_value in query_items:
        key = raw_key.casefold().replace("-", "_")
        compact_key = key.replace("_", "")
        if key not in sensitive and compact_key not in sensitive:
            continue
        secret = raw_value.strip()
        if not secret or secret.startswith("${{ providers.") or secret.startswith("${{ secrets."):
            continue
        raise typer.BadParameter(
            f"MCP --url query parameter {raw_key} must use a credential placeholder, "
            "such as ${{ providers.browserbase.api_key }}."
        )
