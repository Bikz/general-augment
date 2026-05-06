"""Identity-linking helpers for app backends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from genaug.client import GeneralAugmentClient


def link_user(
    client: GeneralAugmentClient,
    project_id: str,
    *,
    phone: str,
    app_user_id: str,
    provider_name: str = "app",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Link a WhatsApp/SMS phone number to the app's user ID."""
    return client.link_user(
        project_id,
        phone=phone,
        app_user_id=app_user_id,
        provider_name=provider_name,
        metadata=metadata,
    )


def resolve_user(client: GeneralAugmentClient, project_id: str, *, phone: str) -> dict[str, Any]:
    """Resolve a linked phone number to app identity metadata."""
    return client.resolve_user(project_id, phone)


def unlink_user(client: GeneralAugmentClient, project_id: str, *, phone: str) -> dict[str, Any]:
    """Remove a phone-to-app identity link."""
    return client.unlink_user(project_id, phone)
