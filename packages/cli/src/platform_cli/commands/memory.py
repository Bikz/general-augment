"""Memory management commands."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage tenant user memory.")

MEMORY_FACT_TYPES = {"preference", "fact", "entity", "summary"}


@app.command("store")
def store_memory(
    ctx: typer.Context,
    fact: Annotated[str, typer.Argument(help="Durable memory fact to store.")],
    user: Annotated[str, typer.Option("--user", help="Tenant app user id.")],
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when using a management key."),
    ] = None,
    fact_type: Annotated[
        str,
        typer.Option("--fact-type", help="Memory fact type: preference, fact, entity, summary."),
    ] = "fact",
    importance: Annotated[
        float,
        typer.Option("--importance", min=0.0, max=1.0, help="Memory importance score."),
    ] = 0.8,
    source: Annotated[
        str,
        typer.Option(help="Non-secret source label for this memory write."),
    ] = "genaug-cli-memory",
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Metadata as key=value. Repeatable."),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(help="Replay-safe key for retryable memory writes."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Store one explicit memory fact for a tenant app user."""
    normalized_fact_type = _fact_type(fact_type)
    runtime: Runtime = ctx.obj
    payload = {
        "user_id": user,
        "fact": fact,
        "fact_type": normalized_fact_type,
        "importance_score": importance,
        "source": source,
        "metadata": _metadata_pairs(metadata or []),
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    with runtime.client() as client:
        project_payload, headers = _project_context(client, project)
        response = client.app(
            "POST",
            "/api/v1/agent/memory/store",
            json=payload,
            headers=headers,
        )
    if json_output:
        print_json(response)
        return
    table(
        "Stored memory",
        ["Field", "Value"],
        [
            ["Project", _project_label(project_payload)],
            ["User", user],
            ["Memory ID", _value(response, "memory_id")],
            ["Type", normalized_fact_type],
            ["Source", _value(response, "source") or source],
        ],
    )


@app.command("search")
def search_memory(
    ctx: typer.Context,
    user: Annotated[str, typer.Option("--user", help="Tenant app user id.")],
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when using a management key."),
    ] = None,
    query: Annotated[
        str,
        typer.Option("--query", "-q", help="Semantic memory query. Empty returns recent facts."),
    ] = "",
    limit: Annotated[int, typer.Option(min=1, max=50, help="Maximum facts to return.")] = 10,
    min_similarity: Annotated[
        float,
        typer.Option("--min-similarity", min=0.0, max=1.0, help="Minimum semantic similarity."),
    ] = 0.7,
    fact_type: Annotated[
        str | None,
        typer.Option("--fact-type", help="Optional fact type filter."),
    ] = None,
    min_importance: Annotated[
        float | None,
        typer.Option("--min-importance", min=0.0, max=1.0, help="Optional importance filter."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(help="Optional source filter."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Search one tenant user's memory facts."""
    payload: dict[str, Any] = {
        "user_id": user,
        "query": query,
        "limit": limit,
        "min_similarity": min_similarity,
    }
    if fact_type is not None:
        payload["fact_type"] = _fact_type(fact_type)
    if min_importance is not None:
        payload["min_importance"] = min_importance
    if source:
        payload["source"] = source
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        _, headers = _project_context(client, project)
        response = client.app(
            "POST",
            "/api/v1/agent/memory/search",
            json=payload,
            headers=headers,
        )
    if json_output:
        print_json(response)
        return
    facts = response.get("facts", []) if isinstance(response, dict) else []
    rows = [
        [
            fact.get("id") or fact.get("memory_id") or "",
            fact.get("fact_type", ""),
            fact.get("content", ""),
            fact.get("importance_score", ""),
            fact.get("similarity", ""),
            fact.get("source", ""),
        ]
        for fact in facts
        if isinstance(fact, dict)
    ]
    table(
        f"Memory for {user}",
        ["ID", "Type", "Content", "Importance", "Similarity", "Source"],
        rows,
    )


@app.command("profile")
def memory_profile(
    ctx: typer.Context,
    user: Annotated[str, typer.Option("--user", help="Tenant app user id.")],
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when using a management key."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Show one tenant user's memory profile and recent facts."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        _, headers = _project_context(client, project)
        response = client.app(
            "GET",
            f"/api/v1/agent/memory/profile/{encode_path_segment(user)}",
            headers=headers,
        )
    if json_output:
        print_json(response)
        return
    profile = response.get("profile", {}) if isinstance(response, dict) else {}
    recent = response.get("recent_facts", []) if isinstance(response, dict) else []
    table(
        f"Memory profile for {user}",
        ["Field", "Value"],
        [
            ["General Augment user", _value(response, "general_augment_user_id")],
            ["Total facts", _value(response, "total_facts")],
            ["Profile keys", ", ".join(sorted(profile)) if isinstance(profile, dict) else ""],
            ["Recent facts", len(recent) if isinstance(recent, list) else 0],
        ],
    )


@app.command("delete")
def delete_memory(
    ctx: typer.Context,
    memory_id: Annotated[str, typer.Argument(help="Memory fact id to delete.")],
    user: Annotated[str, typer.Option("--user", help="Tenant app user id.")],
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when using a management key."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Delete one memory fact for one tenant app user."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        _, headers = _project_context(client, project)
        response = client.app(
            "DELETE",
            f"/api/v1/agent/memory/{encode_path_segment(memory_id)}",
            params={"user_id": user},
            headers=headers,
        )
    if json_output:
        print_json(response)
        return
    status = response.get("status", "deleted") if isinstance(response, dict) else "deleted"
    print_success(f"Memory {memory_id} {status}.")


@app.command("purge-user")
def purge_user_memory(
    ctx: typer.Context,
    user: Annotated[str, typer.Option("--user", help="Tenant app user id.")],
    project: Annotated[
        str | None,
        typer.Option(help="Project id, slug, or name when using a management key."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm purging all scoped memory for this app user."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Purge all memory facts for one tenant app user."""
    if not yes and not typer.confirm(f"Purge all memory for app user {user}?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        _, headers = _project_context(client, project)
        response = client.app(
            "DELETE",
            f"/api/v1/agent/memory/user/{encode_path_segment(user)}",
            headers=headers,
        )
    if json_output:
        print_json(response)
        return
    deleted_count = response.get("deleted_count", 0) if isinstance(response, dict) else 0
    print_success(f"Purged {deleted_count} memory fact(s) for {user}.")


def _project_context(
    client: Any,
    project: str | None,
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Return project metadata and app-facing project context headers."""
    if not project:
        return None, {}
    project_payload = resolve_project(client, project)
    return project_payload, {"X-Project-ID": str(project_payload["id"])}


def _project_label(project: dict[str, Any] | None) -> str:
    """Return a compact display label for project context."""
    if not project:
        return "configured project"
    return str(project.get("slug") or project.get("name") or project.get("id") or "")


def _metadata_pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated key=value metadata flags."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("--metadata values must use key=value.")
        parsed[key.strip()] = value
    return parsed


def _fact_type(value: str) -> str:
    """Validate memory fact type for the public memory API."""
    normalized = value.strip().lower()
    if normalized not in MEMORY_FACT_TYPES:
        raise typer.BadParameter(
            "--fact-type must be one of: " + ", ".join(sorted(MEMORY_FACT_TYPES))
        )
    return normalized


def _value(payload: object, key: str) -> object:
    """Safely read a value from a response mapping."""
    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
