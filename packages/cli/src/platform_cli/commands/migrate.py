"""Safe app migration commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from platform_cli.errors import CLIError
from platform_cli.openai_responses_migration import plan_openai_responses_migration
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime
from platform_cli.self_serve import artifact_dir, build_setup_payload, write_payload

app = typer.Typer(help="Safely migrate an existing app to General Augment.")


@app.command("openai-responses")
def migrate_openai_responses(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option(help="App workspace to inspect and optionally patch."),
    ] = Path("."),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Generate a diff without applying changes."),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply known-safe migration edits after confirmation."),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip confirmation when used with --apply."),
    ] = False,
    project: Annotated[str | None, typer.Option(help="Project id, slug, or name.")] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the migration plan JSON to this path."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Migrate OpenAI Responses-compatible calls to the General Augment endpoint."""
    runtime: Runtime = ctx.obj
    if apply and dry_run:
        raise CLIError("Use either --dry-run or --apply, not both.")
    should_apply = bool(apply)
    if should_apply and not yes and not typer.confirm("Apply General Augment migration edits?"):
        raise typer.Exit(1)
    migration = plan_openai_responses_migration(
        workspace,
        apply=should_apply,
        artifact_dir=artifact_dir(workspace),
    )
    payload = build_setup_payload(
        workspace=workspace,
        config=runtime.config,
        requested_capabilities=[],
        project=project,
        migration=migration,
        mode="migrate",
    )
    artifact_path = write_payload(payload, output, workspace)
    payload["artifact_path"] = str(artifact_path)
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if json_output:
        print_json(payload)
        return
    table(
        "OpenAI Responses migration",
        ["Field", "Value"],
        [
            ["Mode", "apply" if should_apply else "dry-run"],
            ["Diff", migration["diff_path"]],
            ["Files", ", ".join(migration["diff_files"]) or "none"],
            ["Artifact", artifact_path],
        ],
    )
    print_success(
        "Migration edits applied."
        if should_apply
        else "Migration diff generated without changing app code."
    )
