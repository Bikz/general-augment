"""App-developer onboarding commands."""

from __future__ import annotations

import os

import typer

from platform_cli.commands.verify import build_project_verification_payload
from platform_cli.errors import CLIError
from platform_cli.output import panel, print_json, table
from platform_cli.runtime import Runtime

app = typer.Typer(help="Verify app-developer onboarding before launch.")


@app.command("verify")
def verify_onboarding(
    ctx: typer.Context,
    project: str = typer.Option(..., help="Project id, slug, or name."),
    message: str = typer.Option(
        "Reply with one short sentence confirming this General Augment project works.",
        help="Message for the hosted agent test.",
    ),
    user: str = typer.Option(
        "genaug-onboarding-user",
        help="Synthetic app user id for memory and agent checks.",
    ),
    phone_e164: str = typer.Option("+15550000000", help="Synthetic E.164 user identity."),
    channel: str = typer.Option("sms", help="Synthetic channel: sms, whatsapp, ios, or telegram."),
    dashboard_url: str = typer.Option(
        os.getenv("GENAUG_DASHBOARD_URL", "https://app.generalaugment.com"),
        help="Dashboard base URL for follow-up UI checks.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Run the one-command onboarding gate for an existing project."""

    runtime: Runtime = ctx.obj
    with runtime.client() as client:
        payload = build_project_verification_payload(
            client,
            project=project,
            message=message,
            user=user,
            phone_e164=phone_e164,
            channel=channel,
            dashboard_url=dashboard_url,
        )
    payload["onboarding"] = {
        "verdict": payload["verdict"],
        "required_follow_up": [
            "Confirm the dashboard shows the same project, tools, usage, traces, logs, and memory.",
            "Keep project API keys server-side in the app backend.",
            "Handle 402 and 429 responses before production traffic.",
        ],
    }
    if json_output:
        print_json(payload)
    else:
        table(
            f"Onboarding Verify: {payload['project']['slug']}",
            ["Check", "Status", "Detail"],
            [[item["name"], item["status"], item["detail"]] for item in payload["checks"]],
        )
        panel(
            "Dashboard Follow-up",
            "\n".join(f"{key}: {value}" for key, value in payload["dashboard"].items()),
        )
    if payload["verdict"] != "PASS":
        failed = ", ".join(item["name"] for item in payload["checks"] if item["status"] != "PASS")
        raise CLIError(f"Onboarding verification failed: {failed}")
