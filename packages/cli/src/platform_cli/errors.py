"""CLI-specific error types and formatting."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import click


class CLIError(click.ClickException):
    """Base CLI error with a user-facing message."""


class APIError(CLIError):
    """Raised when the platform API returns an error."""

    def __init__(
        self,
        status_code: int,
        detail: Any,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Create an API error."""
        self.status_code = status_code
        self.detail = detail
        self.headers = dict(headers or {})
        super().__init__(helpful_api_error(status_code, detail, self.headers))


def helpful_api_error(
    status_code: int,
    detail: Any,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Return actionable API error text."""
    if status_code == 401:
        return "Your API key may be invalid. Run genaug auth login to re-authenticate."
    if status_code == 403:
        return f"You do not have access to this resource. API said: {_detail_message(detail)}"
    if status_code == 404:
        return (
            "That resource was not found. Check the project or tool name. "
            f"API said: {_detail_message(detail)}"
        )
    if status_code == 429:
        return _rate_limit_message(detail, headers or {})
    if status_code >= 500:
        return (
            "The platform API is having trouble. Retry shortly. "
            f"API said: {_detail_message(detail)}"
        )
    return f"Platform API returned {status_code}: {_detail_message(detail)}"


def _rate_limit_message(detail: Any, headers: Mapping[str, str]) -> str:
    """Return a specific rate-limit explanation when the API provides one."""
    reason = _detail_value(detail, "reason") or _detail_value(detail, "code")
    retry_after = _header_value(headers, "Retry-After") or _detail_value(detail, "retry_after")
    message = _detail_value(detail, "message")
    parts = ["You are being rate limited."]
    if retry_after:
        parts.append(f"Retry after {retry_after} seconds.")
    else:
        parts.append("Wait a minute and retry the command.")
    if reason:
        parts.append(f"Reason: {reason}.")
    if message and message != reason:
        parts.append(f"API said: {message}")
    return " ".join(parts)


def _detail_value(detail: Any, key: str) -> str | None:
    """Find a stable value in API error payloads."""
    if not isinstance(detail, dict):
        return None
    candidates = [detail]
    for nested_key in ("detail", "error"):
        nested = detail.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        value = candidate.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _header_value(headers: Mapping[str, str], key: str) -> str | None:
    """Return a header value without depending on header casing."""
    for header_key, value in headers.items():
        if header_key.lower() == key.lower():
            return value
    return None


def _detail_message(detail: Any) -> str:
    """Return a readable API detail string."""
    if isinstance(detail, dict):
        message = _detail_value(detail, "message")
        if message:
            return message
    return str(detail)
