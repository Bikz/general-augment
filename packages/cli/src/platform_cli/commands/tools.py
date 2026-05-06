"""Tool management commands."""

from __future__ import annotations

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage project tools.")

TOOL_DISCOVERY_MODES = {"auto", "always", "direct"}
DEFAULT_TOOL_DISCOVERY: dict[str, int | str] = {
    "mode": "auto",
    "direct_schema_tool_limit": 10,
    "max_search_results": 5,
}


@app.command("list")
def list_tools(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
) -> None:
    """List tools with enabled and approval state."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        tools_payload = client.admin("GET", "/tools")
    enabled = set(_string_list(project_payload.get("enabled_tool_ids")))
    tools = tools_payload if isinstance(tools_payload, list) else tools_payload.get("items", [])
    rows = [
        [
            item.get("id", ""),
            "enabled" if item.get("id") in enabled else "disabled",
            item.get("risk_level", ""),
            "yes" if item.get("requires_approval") else "no",
        ]
        for item in tools
        if isinstance(item, dict)
    ]
    table("Tools", ["Tool", "State", "Risk", "Approval"], rows)


@app.command("toggle")
def toggle_tool(
    ctx: typer.Context,
    tool_id: str = typer.Argument(...),
    project: str = typer.Option(..., help="Project id, slug, or name."),
    enable: bool | None = typer.Option(None, "--enable/--disable", help="Force state."),
) -> None:
    """Enable or disable a built-in tool."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        enabled = set(_string_list(project_payload.get("enabled_tool_ids")))
        next_enabled = tool_id not in enabled if enable is None else enable
        if next_enabled:
            enabled.add(tool_id)
            state = "enabled"
        else:
            enabled.discard(tool_id)
            state = "disabled"
        client.admin(
            "PUT",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/tools",
            json={"tool_ids": sorted(enabled)},
        )
    print_success(f"{tool_id} {state}.")


@app.command("discovery")
def configure_tool_discovery(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="Tool discovery mode: auto, always, or direct.",
    ),
    direct_schema_tool_limit: int | None = typer.Option(
        None,
        "--direct-schema-tool-limit",
        min=1,
        help="Direct schema limit before catalog search is used.",
    ),
    max_search_results: int | None = typer.Option(
        None,
        "--max-search-results",
        min=1,
        max=10,
        help="Maximum tool schemas returned by one discovery search.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show or update the tenant tool discovery behavior."""
    updates: dict[str, int | str] = {}
    if mode is not None:
        normalized_mode = mode.strip().casefold()
        if normalized_mode not in TOOL_DISCOVERY_MODES:
            raise typer.BadParameter("--mode must be one of: auto, always, direct.")
        updates["mode"] = normalized_mode
    if direct_schema_tool_limit is not None:
        updates["direct_schema_tool_limit"] = direct_schema_tool_limit
    if max_search_results is not None:
        updates["max_search_results"] = max_search_results

    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        current = _tool_discovery(project_payload.get("tool_discovery"))
        if updates:
            next_config = {**current, **updates}
            project_payload = client.admin(
                "PATCH",
                f"/projects/{encode_path_segment(str(project_payload['id']))}",
                json={"tool_discovery": next_config},
            )
            current = _tool_discovery(project_payload.get("tool_discovery"))

    if json_output:
        print_json(current)
        return
    if updates:
        print_success("Tool discovery behavior saved.")
    table(
        "Tool Discovery",
        ["Field", "Value"],
        [
            ["Mode", current["mode"]],
            ["Direct schema limit", current["direct_schema_tool_limit"]],
            ["Search result limit", current["max_search_results"]],
        ],
    )


def _string_list(value: object) -> list[str]:
    """Return a string list from JSON."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _tool_discovery(value: object) -> dict[str, int | str]:
    """Return a normalized tool discovery config from project JSON."""
    if not isinstance(value, dict):
        return dict(DEFAULT_TOOL_DISCOVERY)
    mode = str(value.get("mode") or DEFAULT_TOOL_DISCOVERY["mode"]).casefold()
    if mode not in TOOL_DISCOVERY_MODES:
        mode = str(DEFAULT_TOOL_DISCOVERY["mode"])
    return {
        "mode": mode,
        "direct_schema_tool_limit": _positive_int(
            value.get("direct_schema_tool_limit"),
            default=int(DEFAULT_TOOL_DISCOVERY["direct_schema_tool_limit"]),
        ),
        "max_search_results": min(
            _positive_int(
                value.get("max_search_results"),
                default=int(DEFAULT_TOOL_DISCOVERY["max_search_results"]),
            ),
            10,
        ),
    }


def _positive_int(value: object, *, default: int) -> int:
    """Return a positive integer from API JSON."""
    if value is None:
        return default
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return default
    else:
        return default
    return parsed if parsed >= 1 else default
