"""Tenant identity-link management commands."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage tenant identity links.")


@app.command("list")
def list_identity_links(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    limit: Annotated[int, typer.Option(min=1, max=1000, help="Maximum links to return.")] = 100,
    offset: Annotated[int, typer.Option(min=0, help="Pagination offset.")] = 0,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """List verified and pending identity links for one project."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "GET",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/identity-links",
            params={"limit": limit, "offset": offset},
        )
    if json_output:
        print_json(response)
        return
    items = response.get("items", []) if isinstance(response, dict) else []
    rows = [_identity_row(item) for item in items if isinstance(item, dict)]
    table(
        "Identity links",
        ["Phone", "Provider", "Provider User", "Verified", "Linked At"],
        rows,
    )


@app.command("create-test")
def create_test_identity_link(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to link.")],
    provider_user_id: Annotated[
        str,
        typer.Option("--provider-user-id", help="Tenant app user id for this link."),
    ],
    provider_name: Annotated[
        str,
        typer.Option("--provider-name", help="Tenant identity provider name."),
    ] = "app",
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Metadata as key=value. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create or update one verified test identity link."""
    payload = {
        "phone_e164": phone,
        "provider_user_id": provider_user_id,
        "provider_name": provider_name,
        "metadata": _metadata_pairs(metadata or []),
    }
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            f"/projects/{encode_path_segment(str(project_payload['id']))}/identity-links/test",
            json=payload,
        )
    if json_output:
        print_json(response)
        return
    table(
        "Created test identity link",
        ["Field", "Value"],
        [
            ["Phone", _value(response, "phone_e164") or phone],
            ["Provider", _value(response, "provider_name") or provider_name],
            ["Provider User", _value(response, "provider_user_id") or provider_user_id],
            ["Verified", _value(response, "verified")],
            ["Linked At", _value(response, "linked_at")],
        ],
    )


@app.command("link-user")
def link_user(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to link.")],
    provider_user_id: Annotated[
        str,
        typer.Option("--provider-user-id", help="Tenant app user id for this link."),
    ],
    provider_name: Annotated[
        str,
        typer.Option("--provider-name", help="Tenant identity provider name."),
    ] = "app",
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Metadata as key=value. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create an app-initiated OTP identity-link challenge."""
    response = _identity_challenge(
        ctx,
        project=project,
        endpoint="link-user",
        payload={
            "phone_e164": phone,
            "provider_user_id": provider_user_id,
            "provider_name": provider_name,
            "metadata": _metadata_pairs(metadata or []),
        },
    )
    _print_challenge(
        response,
        title="Identity link challenge",
        fallback_phone=phone,
        fallback_provider=provider_name,
        fallback_provider_user_id=provider_user_id,
        json_output=json_output,
    )


@app.command("verification-code")
def verification_code(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to link.")],
    provider_user_id: Annotated[
        str,
        typer.Option("--provider-user-id", help="Tenant app user id for this link."),
    ],
    provider_name: Annotated[
        str,
        typer.Option("--provider-name", help="Tenant identity provider name."),
    ] = "app",
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Metadata as key=value. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create a code that the tenant app can show to the user."""
    response = _identity_challenge(
        ctx,
        project=project,
        endpoint="verification-code",
        payload={
            "phone_e164": phone,
            "provider_user_id": provider_user_id,
            "provider_name": provider_name,
            "metadata": _metadata_pairs(metadata or []),
        },
    )
    _print_challenge(
        response,
        title="Identity verification code",
        fallback_phone=phone,
        fallback_provider=provider_name,
        fallback_provider_user_id=provider_user_id,
        json_output=json_output,
    )


@app.command("magic-link")
def magic_link(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to link.")],
    user_identifier: Annotated[
        str,
        typer.Option("--user-identifier", help="Email or app username to prefill."),
    ],
    provider_name: Annotated[
        str,
        typer.Option("--provider-name", help="Tenant identity provider name."),
    ] = "app",
    channel: Annotated[
        str,
        typer.Option("--channel", help="Delivery channel: whatsapp, sms, or telegram."),
    ] = "whatsapp",
    metadata: Annotated[
        list[str] | None,
        typer.Option("--metadata", help="Metadata as key=value. Repeatable."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Create and optionally deliver an agent-initiated magic-link challenge."""
    response = _identity_challenge(
        ctx,
        project=project,
        endpoint="magic-link",
        payload={
            "phone_e164": phone,
            "user_identifier": user_identifier,
            "provider_name": provider_name,
            "channel": channel,
            "metadata": _metadata_pairs(metadata or []),
        },
    )
    _print_challenge(
        response,
        title="Identity magic link",
        fallback_phone=phone,
        fallback_provider=provider_name,
        fallback_provider_user_id=user_identifier,
        json_output=json_output,
    )


@app.command("verify")
def verify_identity(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to verify.")],
    code: Annotated[str, typer.Option("--code", help="OTP, texted code, or magic-link state.")],
    provider_name: Annotated[
        str,
        typer.Option("--provider-name", help="Tenant identity provider name."),
    ] = "app",
    provider_user_id: Annotated[
        str | None,
        typer.Option("--provider-user-id", help="Override app user id after Auth0 callback."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Verify a pending identity-link challenge."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        payload = {
            "phone_e164": phone,
            "provider_name": provider_name,
            "code": code,
        }
        if provider_user_id:
            payload["provider_user_id"] = provider_user_id
        response = client.integrations(
            "POST",
            f"/{encode_path_segment(str(project_payload['id']))}/verify",
            json=payload,
        )
    _print_resolution(response, json_output=json_output, success_message="Identity verified")


@app.command("resolve")
def resolve_identity(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to resolve.")],
    provider_name: Annotated[
        str | None,
        typer.Option("--provider-name", help="Optional tenant identity provider name."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Resolve a verified phone-to-app-account identity link."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        params = {"provider_name": provider_name} if provider_name else None
        response = client.integrations(
            "GET",
            f"/{encode_path_segment(str(project_payload['id']))}/resolve/{encode_path_segment(phone)}",
            params=params,
        )
    _print_resolution(response, json_output=json_output, success_message="Identity resolved")


@app.command("unlink")
def unlink_identity(
    ctx: typer.Context,
    project: Annotated[str, typer.Option(help="Project id, slug, or name.")],
    phone: Annotated[str, typer.Option("--phone", help="Phone number to unlink.")],
    provider_name: Annotated[
        str,
        typer.Option("--provider-name", help="Tenant identity provider name."),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm unlinking this identity mapping."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable JSON."),
    ] = False,
) -> None:
    """Remove one phone-to-app-account identity link."""
    if not yes and not typer.confirm(f"Unlink {phone} from {provider_name}?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.integrations(
            "DELETE",
            f"/{encode_path_segment(str(project_payload['id']))}/unlink/{encode_path_segment(phone)}",
            params={"provider_name": provider_name},
        )
    if json_output:
        print_json(response)
        return
    message = "Identity unlinked" if _value(response, "unlinked") else "Identity link not found"
    print_success(message)


def _identity_row(link: dict[str, Any]) -> list[object]:
    """Return a table row for one identity link."""
    return [
        link.get("phone_e164", ""),
        link.get("provider_name", ""),
        link.get("provider_user_id", ""),
        "yes" if link.get("verified") else "no",
        link.get("linked_at", ""),
    ]


def _identity_challenge(
    ctx: typer.Context,
    *,
    project: str,
    endpoint: str,
    payload: dict[str, Any],
) -> object:
    """Create one identity challenge through the integration API."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        return client.integrations(
            "POST",
            f"/{encode_path_segment(str(project_payload['id']))}/{endpoint}",
            json=payload,
        )


def _print_challenge(
    response: object,
    *,
    title: str,
    fallback_phone: str,
    fallback_provider: str,
    fallback_provider_user_id: str,
    json_output: bool,
) -> None:
    """Print an identity challenge response."""
    if json_output:
        print_json(response)
        return
    table(
        title,
        ["Field", "Value"],
        [
            ["Phone", _value(response, "phone_e164") or fallback_phone],
            ["Provider", _value(response, "provider_name") or fallback_provider],
            ["Provider User", _value(response, "provider_user_id") or fallback_provider_user_id],
            ["Expires", _value(response, "verification_expires_at")],
            ["Magic Link", _value(response, "magic_link")],
            ["Debug Code", _value(response, "debug_verification_code")],
        ],
    )


def _print_resolution(
    response: object,
    *,
    json_output: bool,
    success_message: str,
) -> None:
    """Print an identity resolution response."""
    if json_output:
        print_json(response)
        return
    print_success(success_message)
    table(
        "Identity resolution",
        ["Field", "Value"],
        [
            ["Phone", _value(response, "phone_e164")],
            ["Provider", _value(response, "provider_name")],
            ["Provider User", _value(response, "provider_user_id")],
            ["Linked At", _value(response, "linked_at")],
        ],
    )


def _metadata_pairs(values: list[str]) -> dict[str, str]:
    """Parse repeated key=value metadata flags."""
    parsed: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise typer.BadParameter("--metadata values must use key=value.")
        parsed[key.strip()] = value
    return parsed


def _value(payload: object, key: str) -> object:
    """Safely read a value from a response mapping."""
    if isinstance(payload, dict):
        return payload.get(key, "")
    return ""
