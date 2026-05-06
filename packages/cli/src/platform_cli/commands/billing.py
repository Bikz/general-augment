"""Billing lifecycle commands."""

from __future__ import annotations

from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, print_warning, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage hosted billing lifecycle actions.")


@app.command("checkout")
def create_checkout_session(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    tier: Annotated[str, typer.Option(help="Paid target tier: pro or team.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create a hosted Stripe Checkout session URL for a paid tier."""
    target_tier = _target_tier(tier)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/billing/checkout-session",
            json={"target_tier": target_tier},
        )
    if json_output:
        print_json(response)
        return
    url = _value(response, "url")
    print_success(
        f"Created {target_tier} checkout session for {project_payload.get('slug', project)}."
    )
    table(
        "Billing checkout",
        ["Field", "Value"],
        [
            ["Target tier", target_tier],
            ["URL", url],
            ["Next step", "Open the hosted URL and confirm the Stripe webhook event syncs."],
        ],
    )


@app.command("portal")
def create_portal_session(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create a hosted Stripe Customer Portal session URL."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/billing/portal-session",
        )
    if json_output:
        print_json(response)
        return
    url = _value(response, "url")
    print_success(f"Created customer portal session for {project_payload.get('slug', project)}.")
    table(
        "Billing portal",
        ["Field", "Value"],
        [
            ["URL", url],
            ["Next step", "Open the hosted URL for card, invoice, cancellation, or plan actions."],
        ],
    )


@app.command("events")
def list_billing_events(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List recent Stripe billing lifecycle events stored by General Augment."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/billing/events",
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    rows = [_event_row(item) for item in items if isinstance(item, dict)]
    if not rows:
        print_warning("No billing events are stored for this project yet.")
    table(
        f"Billing events for {project_payload.get('slug', project)}",
        ["Event", "Status", "Tier", "Invoice", "Processed at"],
        rows,
    )


def _target_tier(value: str) -> str:
    """Normalize and validate a checkout target tier."""

    target = value.casefold()
    if target not in {"pro", "team"}:
        raise typer.BadParameter("Paid target tier must be 'pro' or 'team'.")
    return target


def _event_row(item: dict[str, object]) -> list[object]:
    """Return one billing event table row."""

    return [
        item.get("event_type", ""),
        item.get("status", "") or "",
        item.get("target_pricing_tier", "") or "",
        item.get("stripe_invoice_id", "") or "",
        item.get("processed_at", "") or item.get("created_at", "") or "",
    ]


def _value(payload: object, key: str) -> object:
    """Safely read one response value."""

    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
