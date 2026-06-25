"""Tool management commands."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.mcp_helpers import add_mcp_server
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage project tools.")
app.command("add-mcp")(add_mcp_server)

TOOL_DISCOVERY_MODES = {"auto", "always", "direct"}
APPROVAL_POLICY_MODES = {"tool_defaults", "risky_tools", "all_tools"}
DEFAULT_TOOL_DISCOVERY: dict[str, Any] = {
    "mode": "auto",
    "direct_schema_tool_limit": 10,
    "max_search_results": 5,
    "approval_policy": {"mode": "tool_defaults"},
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


@app.command("catalog")
def tool_catalog(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    source: str | None = typer.Option(
        None,
        "--source",
        help="Filter by source, such as builtin, mcp, generated_openapi, or local_connector.",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Filter by catalog status, such as available, disabled, or unavailable.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show the normalized tenant tool catalog across platform, MCP, OpenAPI, and connectors."""
    normalized_source = source.strip().casefold() if source else None
    normalized_status = status.strip().casefold() if status else None
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        catalog = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/tools/catalog",
        )
    items = _catalog_items(catalog)
    if normalized_source:
        items = [
            item
            for item in items
            if str(item.get("source", "")).casefold() == normalized_source
        ]
    if normalized_status:
        items = [
            item
            for item in items
            if str(item.get("status", "")).casefold() == normalized_status
        ]

    if json_output:
        json_payload = catalog if isinstance(catalog, dict) else {}
        print_json({**json_payload, "items": items})
        return

    rows = [
        [
            item.get("id", ""),
            item.get("source", ""),
            item.get("status", ""),
            item.get("risk_level", ""),
            item.get("approval_policy", ""),
            item.get("auth_requirement", ""),
        ]
        for item in items
        if isinstance(item, dict)
    ]
    table(
        "Tool Catalog",
        ["Tool", "Source", "Status", "Risk", "Approval", "Auth"],
        rows,
    )


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
    approval_policy: str | None = typer.Option(
        None,
        "--approval-policy",
        help="Approval policy: tool_defaults, risky_tools, or all_tools.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show or update the tenant tool discovery behavior."""
    updates: dict[str, Any] = {}
    if mode is not None:
        normalized_mode = mode.strip().casefold()
        if normalized_mode not in TOOL_DISCOVERY_MODES:
            raise typer.BadParameter("--mode must be one of: auto, always, direct.")
        updates["mode"] = normalized_mode
    if direct_schema_tool_limit is not None:
        updates["direct_schema_tool_limit"] = direct_schema_tool_limit
    if max_search_results is not None:
        updates["max_search_results"] = max_search_results
    if approval_policy is not None:
        normalized_policy = approval_policy.strip().casefold()
        if normalized_policy not in APPROVAL_POLICY_MODES:
            raise typer.BadParameter(
                "--approval-policy must be one of: tool_defaults, risky_tools, all_tools."
            )
        updates["approval_policy"] = {"mode": normalized_policy}

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
            ["Approval policy", _approval_policy_mode(current)],
        ],
    )


@app.command("explain-turn")
def explain_turn_tool_discovery(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    requested_tool: Annotated[
        list[str] | None,
        typer.Option(
            "--requested-tool",
            help="Tool id explicitly requested for the turn. Repeat for multiple tools.",
        ),
    ] = None,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Explain whether one turn would use direct schemas or dynamic tool discovery."""
    requested_tools = [tool for tool in requested_tool or [] if tool]
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        project_id = str(project_payload["id"])
        policy = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/runtime-policy",
        )
        catalog = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/tools/catalog",
        )
    explanation = _turn_tool_discovery_explanation(
        project_id,
        requested_tools=requested_tools,
        policy=policy if isinstance(policy, dict) else {},
        catalog=catalog if isinstance(catalog, dict) else {},
    )
    if json_output:
        print_json(explanation)
        return
    decision = explanation["decision"] if isinstance(explanation.get("decision"), dict) else {}
    table(
        "Tool Discovery Decision",
        ["Field", "Value"],
        [
            ["Project", explanation.get("project_id", project_id)],
            ["Mode", _nested_value(explanation, "tool_discovery", "mode")],
            ["Exposure", decision.get("exposure", "")],
            ["Reason", decision.get("reason", "")],
            ["Requested tools", ", ".join(requested_tools) if requested_tools else "none"],
            [
                "Direct platform tools",
                _nested_value(explanation, "hermes_exposure", "direct_platform_tool_count"),
            ],
            [
                "Search result limit",
                _nested_value(explanation, "hermes_exposure", "search_result_limit"),
            ],
            ["Catalog total", _nested_value(explanation, "catalog", "total")],
            ["Warnings", "; ".join(_string_list(decision.get("warnings"))) or "none"],
        ],
    )


def _string_list(value: object) -> list[str]:
    """Return a string list from JSON."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _catalog_items(payload: object) -> list[dict[str, object]]:
    """Return catalog item dictionaries from API JSON."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _tool_discovery(value: object) -> dict[str, Any]:
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
        "approval_policy": {"mode": _approval_policy_mode(value)},
    }


def _approval_policy_mode(value: object) -> str:
    """Return a normalized approval policy mode from project JSON."""
    policy = value.get("approval_policy") if isinstance(value, dict) else None
    raw_mode = policy.get("mode") if isinstance(policy, dict) else None
    mode = str(raw_mode or "tool_defaults").casefold()
    return mode if mode in APPROVAL_POLICY_MODES else "tool_defaults"


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


def _turn_tool_discovery_explanation(
    project_id: str,
    *,
    requested_tools: list[str],
    policy: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic turn-level tool exposure explanation."""
    hermes_exposure = _object_dict(policy.get("hermes_exposure"))
    tool_discovery = _tool_discovery(policy.get("tool_discovery"))
    catalog_items = _catalog_items(catalog)
    catalog_by_id = {str(item.get("id")): item for item in catalog_items}
    unavailable_requested = [
        tool_id
        for tool_id in requested_tools
        if str(catalog_by_id.get(tool_id, {}).get("status") or "unavailable") != "available"
    ]
    if requested_tools:
        exposure = "explicit_tool_subset"
        reason = "Explicit requested tools preserve request-scoped direct schemas."
    elif bool(hermes_exposure.get("uses_dynamic_discovery_by_default")):
        exposure = "dynamic_discovery"
        reason = "Catalog size or policy asks Hermes to search before loading schemas."
    else:
        exposure = "direct_schemas"
        reason = "Current catalog fits within the direct schema policy."
    warnings: list[str] = []
    if unavailable_requested:
        warnings.append("Some requested tools are unavailable or outside the project catalog.")
    if tool_discovery["mode"] == "direct" and int(tool_discovery["direct_schema_tool_limit"]) < len(
        [
            item
            for item in catalog_items
            if item.get("enabled") and item.get("status") == "available"
        ]
    ):
        warnings.append("Direct mode may load more schemas than the configured direct limit.")
    return {
        "schema_version": "genaug.tool_discovery_explanation.v1",
        "project_id": project_id,
        "requested_tools": requested_tools,
        "tool_discovery": tool_discovery,
        "hermes_exposure": {
            "uses_dynamic_discovery_by_default": bool(
                hermes_exposure.get("uses_dynamic_discovery_by_default")
            ),
            "direct_platform_tool_count": _int_value(
                hermes_exposure.get("direct_platform_tool_count")
            ),
            "search_result_limit": _int_value(hermes_exposure.get("search_result_limit")),
        },
        "catalog": {
            "total": _int_value(
                _object_dict(catalog.get("counts")).get("total"),
                len(catalog_items),
            ),
            "enabled": _int_value(
                _object_dict(catalog.get("counts")).get("enabled"),
                len([item for item in catalog_items if item.get("enabled")]),
            ),
            "available": len([item for item in catalog_items if item.get("status") == "available"]),
            "unavailable": _int_value(
                _object_dict(catalog.get("counts")).get("unavailable"),
                len([item for item in catalog_items if item.get("status") != "available"]),
            ),
        },
        "decision": {
            "exposure": exposure,
            "reason": reason,
            "warnings": warnings,
            "unavailable_requested_tools": unavailable_requested,
        },
    }


def _object_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_value(value: object, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _nested_value(payload: object, section: str, key: str) -> object:
    if not isinstance(payload, dict):
        return ""
    nested = payload.get(section)
    if not isinstance(nested, dict):
        return ""
    return nested.get(key, "")
