"""Billing lifecycle commands."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.errors import CLIError
from platform_cli.output import print_json, print_success, print_warning, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage hosted billing lifecycle actions.")


@app.command("status")
def billing_status(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Show credit balance, funding mode, and auto top-up state."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        project_id = str(project_payload["id"])
        credit_balance = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/billing/credits",
        )
        auto_top_up = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/billing/credits/auto-top-up",
        )
        auto_top_up_attempts = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/billing/credits/auto-top-up/attempts",
        )
    payload = {
        "project": project_payload,
        "credit_balance": credit_balance,
        "auto_top_up": auto_top_up,
        "auto_top_up_attempts": auto_top_up_attempts,
    }
    if json_output:
        print_json(payload)
        return
    latest_attempt = _first_item(auto_top_up_attempts)
    table(
        f"Billing status for {project_payload.get('slug', project)}",
        ["Field", "Value"],
        [
            [
                "Plan",
                project_payload.get("plan") or project_payload.get("pricing_tier") or "",
            ],
            ["Funding mode", _funding_mode(project_payload)],
            ["Active balance USD", _value(credit_balance, "active_balance_usd")],
            ["Credit grants", _count(credit_balance, "grants")],
            ["Reservations", _count(credit_balance, "reservations")],
            ["Ledger rows", _count(credit_balance, "ledger_entries")],
            ["Auto top-up", _auto_top_up_status(auto_top_up)],
            ["Auto top-up threshold", _value(auto_top_up, "threshold_usd")],
            ["Auto top-up amount", _value(auto_top_up, "top_up_amount_usd")],
            ["Monthly cap", _value(auto_top_up, "monthly_cap_usd")],
            ["Payment method", _value(auto_top_up, "payment_method_status")],
            ["Last auto top-up attempt", _attempt_summary(latest_attempt)],
        ],
    )


@app.command("checkout")
def create_checkout_session(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    tier: Annotated[str, typer.Option(help="Paid target tier: build, pro, or team.")],
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


@app.command("top-up")
def create_credit_top_up_checkout_session(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    amount_usd: Annotated[str, typer.Option(help="Usage credit top-up amount in USD.")],
    save_payment_method: Annotated[
        bool,
        typer.Option(
            "--save-payment-method",
            help="Ask hosted Checkout to collect off-session future-use consent.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create a hosted Stripe Checkout session for paid usage credits."""
    amount = _positive_amount_usd(amount_usd)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        request_payload: dict[str, object] = {"amount_usd": amount}
        if save_payment_method:
            request_payload["save_payment_method"] = True
        response = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/billing/credits/top-up-checkout-session",
            json=request_payload,
        )
    if json_output:
        print_json(response)
        return
    url = _value(response, "url")
    print_success(
        f"Created top-up checkout session for {project_payload.get('slug', project)}."
    )
    table(
        "Credit top-up",
        ["Field", "Value"],
        [
            ["Amount USD", amount],
            ["Save payment method", "yes" if save_payment_method else "no"],
            ["URL", url],
            ["Next step", "Open the hosted URL and confirm the Stripe webhook creates credits."],
        ],
    )


@app.command("usage")
def billing_usage(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    start_date: Annotated[
        str | None,
        typer.Option(help="Inclusive start date, YYYY-MM-DD."),
    ] = None,
    end_date: Annotated[
        str | None,
        typer.Option(help="Inclusive end date, YYYY-MM-DD."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Show billing-relevant usage rollups for reconciliation."""
    runtime: Runtime = ctx.obj
    params = {
        key: value
        for key, value in {"start_date": start_date, "end_date": end_date}.items()
        if value is not None
    }
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        usage = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/usage",
            params=params,
        )
    payload = {"project": project_payload, "usage": usage}
    if json_output:
        print_json(payload)
        return
    totals = usage.get("totals", {}) if isinstance(usage, dict) else {}
    table(
        f"Billing usage for {project_payload.get('slug', project)}",
        ["Metric", "Value"],
        [
            ["Agent turns", _metric(totals, "agent_turns_count")],
            ["Stored messages", _metric(totals, "messages_count")],
            ["Tool calls", _metric(totals, "tool_calls_count")],
            ["Cost USD", _metric(totals, "total_cost_usd")],
        ],
    )
    days = usage.get("days", []) if isinstance(usage, dict) else []
    day_rows = [
        [
            item.get("date", item.get("day", "")),
            _metric(item, "agent_turns_count"),
            _metric(item, "tool_calls_count"),
            _metric(item, "total_cost_usd"),
        ]
        for item in days
        if isinstance(item, dict)
    ]
    if day_rows:
        table(
            "Daily billing usage",
            ["Date", "Agent turns", "Tool calls", "Cost USD"],
            day_rows,
        )


@app.command("verify")
def billing_verify(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Verify that platform-funded work is credit-gated and ledger-visible."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        project_id = str(project_payload["id"])
        credit_balance = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/billing/credits",
            params={"limit": 500},
        )
        usage = client.admin(
            "GET",
            f"/projects/{encode_path_segment(project_id)}/usage",
        )
    checks = _billing_verify_checks(
        project=project_payload,
        credit_balance=credit_balance,
        usage=usage,
    )
    verdict = _checks_verdict(checks)
    payload = {
        "project": project_payload,
        "credit_balance": credit_balance,
        "usage": usage,
        "verdict": verdict,
        "checks": checks,
    }
    if json_output:
        print_json(payload)
    else:
        table(
            f"Billing Verify: {project_payload.get('slug', project)}",
            ["Check", "Status", "Detail"],
            [[item["name"], item["status"], item["detail"]] for item in checks],
        )
    if verdict != "PASS":
        raise CLIError(f"Billing verification failed: {', '.join(_failed_checks(checks))}")


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
    if target not in {"build", "pro", "team"}:
        raise typer.BadParameter("Paid target tier must be 'build', 'pro', or 'team'.")
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


def _billing_verify_checks(
    *,
    project: dict[str, object],
    credit_balance: object,
    usage: object,
) -> list[dict[str, str]]:
    """Build read-only billing verification checks for one project."""

    rate_limits = project.get("rate_limits")
    funding_mode = _funding_mode(project)
    ledger_entries = _items(credit_balance, "ledger_entries")
    totals = usage.get("totals") if isinstance(usage, dict) else None
    return [
        _billing_check(
            "credit_billing_enabled",
            isinstance(rate_limits, dict) and rate_limits.get("credit_billing_enabled") is True,
            "credit_billing_enabled=true"
            if isinstance(rate_limits, dict) and rate_limits.get("credit_billing_enabled") is True
            else "rate_limits.credit_billing_enabled is not true",
        ),
        _billing_check(
            "funding_mode_declared",
            bool(funding_mode),
            f"funding_mode={funding_mode}" if funding_mode else "funding_mode is missing",
        ),
        _billing_check(
            "credit_balance_reachable",
            isinstance(credit_balance, dict) and "active_balance_usd" in credit_balance,
            f"active_balance_usd={_value(credit_balance, 'active_balance_usd')}"
            if isinstance(credit_balance, dict)
            else "credit balance response was not an object",
        ),
        _billing_check(
            "platform_ledger_metered",
            _platform_ledger_metered(ledger_entries),
            _platform_ledger_detail(ledger_entries),
        ),
        _billing_check(
            "usage_rollup_reachable",
            isinstance(totals, dict),
            "usage totals visible" if isinstance(totals, dict) else "usage totals missing",
        ),
    ]


def _billing_check(name: str, passed: bool, detail: object) -> dict[str, str]:
    """Return one billing verification check row."""

    return {"name": name, "status": "PASS" if passed else "FAIL", "detail": str(detail)}


def _checks_verdict(checks: list[dict[str, str]]) -> str:
    """Return PASS only when all billing verification checks pass."""

    return "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"


def _failed_checks(checks: list[dict[str, str]]) -> list[str]:
    """Return failed billing verification check names."""

    return [item["name"] for item in checks if item["status"] == "FAIL"]


def _platform_ledger_metered(items: list[dict[str, object]]) -> bool:
    """Return whether recent platform-funded ledger entries reference reservations."""

    return not _unmetered_platform_ledger_items(items)


def _platform_ledger_detail(items: list[dict[str, object]]) -> str:
    """Return a compact detail for recent platform-funded ledger rows."""

    platform_rows = [
        item for item in items if str(item.get("provider_source") or "") == "platform"
    ]
    unmetered = _unmetered_platform_ledger_items(items)
    if unmetered:
        event_types = ", ".join(str(item.get("event_type") or "") for item in unmetered)
        return f"platform ledger rows without reservation_id: {event_types}"
    if platform_rows:
        return f"{len(platform_rows)} recent platform ledger rows include reservation_id"
    return "no recent platform-funded ledger rows"


def _unmetered_platform_ledger_items(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return platform-funded agent ledger rows that lack reservation linkage."""

    return [
        item
        for item in items
        if str(item.get("provider_source") or "") == "platform"
        and str(item.get("event_type") or "").startswith("agent_turn_")
        and not item.get("reservation_id")
    ]


def _positive_amount_usd(value: str) -> str:
    """Validate a positive CLI USD amount while preserving operator-entered precision."""

    amount = value.strip()
    try:
        parsed = Decimal(amount)
    except (InvalidOperation, ValueError) as exc:
        raise typer.BadParameter("Top-up amount must be a positive USD decimal.") from exc
    if parsed <= 0:
        raise typer.BadParameter("Top-up amount must be greater than zero.")
    return amount


def _funding_mode(project: dict[str, object]) -> object:
    """Return the project funding mode when present."""

    rate_limits = project.get("rate_limits")
    if isinstance(rate_limits, dict):
        return rate_limits.get("funding_mode", "")
    return ""


def _auto_top_up_status(payload: object) -> str:
    """Return a compact auto top-up status label."""

    if not isinstance(payload, dict) or not payload.get("enabled"):
        return "disabled"
    if payload.get("charge_attempts_enabled"):
        return "enabled, charges active"
    return "enabled, charges disabled"


def _first_item(payload: object) -> dict[str, object] | None:
    """Return the first item from a list response."""

    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    return items[0]


def _attempt_summary(item: dict[str, object] | None) -> str:
    """Return a compact auto top-up attempt summary."""

    if item is None:
        return "none"
    status = str(item.get("status") or "")
    failure_code = str(item.get("failure_code") or "")
    return f"{status} ({failure_code})" if failure_code else status


def _count(payload: object, key: str) -> int:
    """Return the number of list items in a JSON object field."""

    if not isinstance(payload, dict):
        return 0
    items = payload.get(key)
    if isinstance(items, list):
        return len(items)
    return 0


def _items(payload: object, key: str) -> list[dict[str, object]]:
    """Return dictionary items from one list field."""

    if not isinstance(payload, dict):
        return []
    items = payload.get(key)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _metric(payload: object, *keys: str) -> object:
    """Return the first present metric value from a usage payload."""

    if not isinstance(payload, dict):
        return 0
    for key in keys:
        if key in payload:
            return payload[key]
    return 0


def _value(payload: object, key: str) -> object:
    """Safely read one response value."""

    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
