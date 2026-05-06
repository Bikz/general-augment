"""Agent test helpers for General Augment SDK users."""

from __future__ import annotations

from typing import Any

from genaug.client import GeneralAugmentClient


class AgentClient:
    """Convenience wrapper scoped to one General Augment project."""

    def __init__(self, client: GeneralAugmentClient, project_id: str) -> None:
        """Initialize a project-scoped agent client."""
        self.client = client
        self.project_id = project_id

    def test(
        self,
        message: str,
        *,
        phone_e164: str = "+15550000000",
        channel: str = "whatsapp",
    ) -> dict[str, Any]:
        """Send a test message to the configured agent."""
        return self.client.test_agent(
            self.project_id,
            message,
            phone_e164=phone_e164,
            channel=channel,
        )


def test(
    client: GeneralAugmentClient,
    project_id: str,
    message: str,
    *,
    phone_e164: str = "+15550000000",
    channel: str = "whatsapp",
) -> dict[str, Any]:
    """Send a one-off test message to a General Augment project."""
    return client.test_agent(
        project_id,
        message,
        phone_e164=phone_e164,
        channel=channel,
    )
