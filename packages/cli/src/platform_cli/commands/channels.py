"""Channel management commands."""

from __future__ import annotations

import typer

from platform_cli.client import encode_path_segment, resolve_project
from platform_cli.output import panel, print_json, print_success, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Manage channels.")


@app.command("status")
def channel_status(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show channel status."""
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        telegram = client.admin(
            "GET",
            "/channels/telegram/status",
            params={"project_id": project_payload["id"]},
        )
    if json_output:
        print_json(
            {
                "project": project_payload,
                "channels": {
                    "whatsapp": {
                        "connected": bool(project_payload.get("whatsapp_phone_number_id")),
                        "phone_number_id": project_payload.get("whatsapp_phone_number_id"),
                    },
                    "sms": {
                        "connected": bool(project_payload.get("twilio_phone_number")),
                        "twilio_phone_number": project_payload.get("twilio_phone_number"),
                    },
                    "telegram": telegram,
                },
            }
        )
        return
    rows: list[list[object]] = [
        ["WhatsApp", "connected" if project_payload.get("whatsapp_phone_number_id") else "open"],
        ["SMS", "connected" if project_payload.get("twilio_phone_number") else "open"],
        ["Telegram", "connected" if telegram.get("connected") else "open"],
    ]
    table("Channels", ["Channel", "Status"], rows)
    if telegram.get("bot_username"):
        panel(
            "Telegram",
            f"Bot: @{telegram['bot_username']}\n"
            f"Last message: {telegram.get('last_message_at') or 'never'}\n"
            f"24h messages: {telegram.get('message_count_24h', 0)}",
        )


@app.command("connect")
def channel_connect(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    channel: str = typer.Option("telegram", help="Channel to connect."),
    bot_token: str | None = typer.Option(None, help="Telegram bot token."),
    phone_number_id: str | None = typer.Option(
        None,
        "--phone-number-id",
        help="WhatsApp Business phone number id.",
    ),
    twilio_number: str | None = typer.Option(
        None,
        "--twilio-number",
        help="Twilio SMS sender number.",
    ),
    webhook_base_url: str | None = typer.Option(None, help="Public API base URL."),
) -> None:
    """Connect a provider channel."""
    normalized_channel = _normalize_channel(channel)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        if normalized_channel == "telegram":
            if not bot_token:
                bot_token = typer.prompt("Telegram bot token", hide_input=True)
            response = client.admin(
                "POST",
                "/channels/telegram/connect",
                json={
                    "project_id": str(project_payload["id"]),
                    "bot_token": bot_token,
                    "webhook_base_url": webhook_base_url or runtime.config.base_url,
                },
            )
            print_success(f"Telegram connected: @{response.get('bot_username', 'bot')}")
            return
        if normalized_channel == "whatsapp":
            value = _required_channel_value(
                phone_number_id or typer.prompt("WhatsApp Business phone number id"),
                "--phone-number-id",
            )
            client.admin(
                "PATCH",
                f"/projects/{encode_path_segment(str(project_payload['id']))}",
                json={"whatsapp_phone_number_id": value},
            )
            print_success("WhatsApp sender configured.")
            return
        value = _required_channel_value(
            twilio_number or typer.prompt("Twilio SMS sender number"),
            "--twilio-number",
        )
        client.admin(
            "PATCH",
            f"/projects/{encode_path_segment(str(project_payload['id']))}",
            json={"twilio_phone_number": value},
        )
        print_success("SMS sender configured.")


@app.command("disconnect")
def channel_disconnect(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    channel: str = typer.Option("telegram", help="Channel to disconnect."),
    yes: bool = typer.Option(False, "--yes", help="Confirm disconnecting this channel."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Disconnect a provider channel."""
    normalized_channel = _normalize_channel(channel)
    label = _channel_label(normalized_channel)
    if not yes and not typer.confirm(f"Disconnect {label} for project {project}?"):
        raise typer.Exit(1)
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        if normalized_channel == "telegram":
            response = client.admin(
                "POST",
                "/channels/telegram/disconnect",
                json={"project_id": str(project_payload["id"])},
            )
        else:
            field = (
                "whatsapp_phone_number_id"
                if normalized_channel == "whatsapp"
                else "twilio_phone_number"
            )
            response = client.admin(
                "PATCH",
                f"/projects/{encode_path_segment(str(project_payload['id']))}",
                json={field: None},
            )
    if json_output:
        print_json(response)
        return
    print_success(f"{label} disconnected.")


@app.command("test")
def channel_test(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    channel: str = typer.Option("telegram", help="Channel to test."),
    chat_id: str | None = typer.Option(None, help="Telegram chat id for a test message."),
    message: str = typer.Option("Hello from your General Augment agent.", help="Test message."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Send a provider test message."""
    if channel != "telegram":
        raise typer.BadParameter("Only telegram guided test is supported by this command.")
    if not chat_id:
        chat_id = typer.prompt("Telegram chat id")
    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        project_payload = resolve_project(client, project)
        response = client.admin(
            "POST",
            "/channels/telegram/test",
            json={
                "project_id": str(project_payload["id"]),
                "chat_id": chat_id,
                "message": message,
            },
        )
    if json_output:
        print_json(response)
        return
    print_success("Telegram test message sent.")


def _normalize_channel(channel: str) -> str:
    """Return a supported channel id."""
    normalized = channel.strip().casefold()
    if normalized not in {"telegram", "whatsapp", "sms"}:
        raise typer.BadParameter("--channel must be one of: telegram, whatsapp, sms")
    return normalized


def _channel_label(channel: str) -> str:
    """Return a display label for a channel id."""
    return {"telegram": "Telegram", "whatsapp": "WhatsApp", "sms": "SMS"}[channel]


def _required_channel_value(value: str, option_name: str) -> str:
    """Return a non-empty channel configuration value."""
    normalized = value.strip()
    if not normalized:
        raise typer.BadParameter(f"{option_name} is required for this channel")
    return normalized
