"""Local agent manifest validation command."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from platform_cli.openapi import LocalValidationResult, validate_local_agent_config
from platform_cli.output import print_error, print_json, print_success, print_warning, table


def validate(
    config_path: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            help="genaug-agent.yaml manifest to validate.",
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable validation output."),
    ] = False,
) -> None:
    """Validate a local genaug-agent.yaml without calling the hosted API."""
    result = validate_local_agent_config(config_path)
    payload = _validation_payload(result)
    if json_output:
        print_json(payload)
    else:
        _print_human_result(result)
    if result.errors:
        raise typer.Exit(1)


def _print_human_result(result: LocalValidationResult) -> None:
    """Print a readable validation report."""
    table(
        "Agent Config Validation",
        ["Field", "Value"],
        [
            ["Status", result.status],
            ["Project", result.project_name or "unknown"],
            ["Manifest", result.config_path],
            ["SOUL", result.soul_file or "not configured"],
            ["Skills", f"{result.skill_count} SKILL.md files"],
            ["Builtin tools", _joined(result.builtin_tools)],
            ["MCP servers", _joined(result.mcp_servers)],
            ["Tool discovery", _tool_discovery_summary(result.tool_discovery)],
        ],
    )
    if result.errors:
        table("Errors", ["Issue"], [[error] for error in result.errors])
        print_error("Agent config validation failed.")
    else:
        print_success("Agent config validation passed.")
    if result.warnings:
        table("Warnings", ["Issue"], [[warning] for warning in result.warnings])
        print_warning("Review warnings before production deploy.")


def _validation_payload(result: LocalValidationResult) -> dict[str, Any]:
    """Return machine-readable validation output."""
    return {
        "status": result.status,
        "config_path": str(result.config_path),
        "project_name": result.project_name,
        "errors": result.errors,
        "warnings": result.warnings,
        "soul_file": str(result.soul_file) if result.soul_file else None,
        "skills": {
            "directory": str(result.skills_dir) if result.skills_dir else None,
            "skill_md_count": result.skill_count,
        },
        "tools": {
            "builtin": result.builtin_tools,
            "mcp_servers": result.mcp_servers,
        },
        "tool_discovery": result.tool_discovery,
    }


def _joined(items: list[str]) -> str:
    """Return display text for a list."""
    return ", ".join(items) if items else "none"


def _tool_discovery_summary(value: dict[str, int | str]) -> str:
    """Return compact display text for tool discovery behavior."""
    return (
        f"{value['mode']} "
        f"(direct <= {value['direct_schema_tool_limit']}, search <= {value['max_search_results']})"
    )
