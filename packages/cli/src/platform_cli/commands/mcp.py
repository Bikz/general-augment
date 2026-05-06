"""MCP server management commands."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage tenant MCP servers.")


@app.command("list")
def list_mcp_servers(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List MCP servers configured for one project."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/mcp-servers",
        )
    if json_output:
        print_json(payload)
        return
    items = payload.get("items", []) if isinstance(payload, dict) else []
    rows = [_server_row(item) for item in items if isinstance(item, dict)]
    table("MCP servers", ["Name", "Transport", "Enabled", "Endpoint", "Tools"], rows)


@app.command("add")
def add_mcp_server(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="MCP server name.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
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
    enabled: Annotated[
        bool,
        typer.Option("--enabled/--disabled", help="Whether Hermes may use this server."),
    ] = True,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Add one HTTP or stdio MCP server to a project."""
    if bool(url) == bool(command):
        raise typer.BadParameter("Provide exactly one transport: --url or --command.")
    payload: dict[str, Any] = {"name": name, "enabled": enabled}
    if url:
        payload["url"] = url
    if command:
        payload["command"] = command
    if arg:
        payload["args"] = list(arg)
    headers = _key_value_pairs(header or [], option="--header")
    if headers:
        payload["headers"] = headers
    env_values = _key_value_pairs(env or [], option="--env")
    if env_values:
        payload["env"] = env_values
    tools = _tools_filter(include_tool or [], exclude_tool or [])
    if tools:
        payload["tools"] = tools
    if timeout is not None:
        payload["timeout"] = timeout
    if connect_timeout is not None:
        payload["connect_timeout"] = connect_timeout
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/mcp-servers",
            json=payload,
        )
    if json_output:
        print_json(response)
        return
    table(
        "Added MCP server",
        ["Field", "Value"],
        [
            ["Project", project],
            ["Name", _value(response, "name") or name],
            ["Transport", _transport(response if isinstance(response, dict) else payload)],
            ["Enabled", _value(response, "enabled")],
            ["Tools", _tools_label(response if isinstance(response, dict) else payload)],
        ],
    )


@app.command("test")
def test_mcp_server(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="MCP server name.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Test one configured MCP server."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/mcp-servers/"
                f"{encode_path_segment(name)}/test"
            ),
        )
    if json_output:
        print_json(response)
        return
    table(
        "MCP server test",
        ["Field", "Value"],
        [
            ["Name", _value(response, "name")],
            ["OK", _value(response, "ok")],
            ["Transport", _value(response, "transport")],
            ["Detail", _value(response, "detail")],
        ],
    )


@app.command("delete")
def delete_mcp_server(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="MCP server name.")],
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Delete one configured MCP server."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "DELETE",
            (
                f"/projects/{encode_path_segment(str(project_payload['id']))}/mcp-servers/"
                f"{encode_path_segment(name)}"
            ),
        )
    if json_output:
        print_json(response)
        return
    deleted_name = response.get("name", name) if isinstance(response, dict) else name
    print_success(f"Deleted MCP server {deleted_name}.")


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


def _server_row(server: dict[str, Any]) -> list[object]:
    """Return a table row for one MCP server."""
    return [
        server.get("name", ""),
        _transport(server),
        "yes" if server.get("enabled", True) else "no",
        server.get("url") or server.get("command") or "",
        _tools_label(server),
    ]


def _transport(server: dict[str, Any]) -> str:
    """Return MCP transport label."""
    if server.get("url"):
        return "http"
    if server.get("command"):
        return "stdio"
    return "unknown"


def _tools_label(server: dict[str, Any]) -> str:
    """Return a compact tool filter label."""
    tools = server.get("tools")
    if not isinstance(tools, dict):
        return "all"
    include = _string_list(tools.get("include"))
    exclude = _string_list(tools.get("exclude"))
    labels: list[str] = []
    if include:
        labels.append("include: " + ", ".join(include))
    if exclude:
        labels.append("exclude: " + ", ".join(exclude))
    return "; ".join(labels) if labels else "all"


def _string_list(value: object) -> list[str]:
    """Return a string list from JSON."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _value(payload: object, key: str) -> object:
    """Safely read a value from a response mapping."""
    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
