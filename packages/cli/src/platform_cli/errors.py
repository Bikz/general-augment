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
        *,
        request_path: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        """Create an API error."""
        self.status_code = status_code
        self.detail = detail
        self.headers = dict(headers or {})
        self.request_path = request_path
        self.auth_mode = auth_mode
        super().__init__(
            helpful_api_error(
                status_code,
                detail,
                self.headers,
                request_path=request_path,
                auth_mode=auth_mode,
            )
        )


def helpful_api_error(
    status_code: int,
    detail: Any,
    headers: Mapping[str, str] | None = None,
    *,
    request_path: str | None = None,
    auth_mode: str | None = None,
) -> str:
    """Return actionable API error text."""
    app_facing = auth_mode == "bearer" or (request_path or "").startswith("/v1/")
    detail_message = _detail_message(detail)
    if status_code == 401:
        if app_facing:
            return (
                "The project runtime key may be invalid or expired. Create or paste a "
                f"server-side project runtime key for this project. API said: {detail_message}"
            )
        return "Your API key may be invalid. Run genaug auth login to re-authenticate."
    if status_code == 403:
        if app_facing:
            return (
                "The project runtime key is not allowed to call this project. Check the "
                f"selected project, key scope, and X-Project-ID. API said: {detail_message}"
            )
        return f"You do not have access to this resource. API said: {detail_message}"
    if status_code == 402:
        return (
            "The project hit a billing, credits, plan, or LLM budget limit. Check "
            "genaug billing status and project rate limits before retrying. "
            f"API said: {detail_message}"
        )
    if status_code == 404:
        if app_facing:
            return (
                "The app-facing resource was not found. Verify the project ID, API base URL, "
                f"and that this key belongs to the selected project. API said: {detail_message}"
            )
        return (
            "That resource was not found. Check the project or tool name. "
            f"API said: {detail_message}"
        )
    if status_code == 409:
        return _conflict_message(detail, headers or {})
    if status_code == 422:
        return (
            "The API rejected the request payload or project configuration. Review the "
            f"command inputs and generated request shape. API said: {detail_message}"
        )
    if status_code == 429:
        return _rate_limit_message(detail, headers or {})
    if status_code >= 500:
        if app_facing:
            provider_context = _provider_failure_context(detail)
            return (
                "The platform, provider, or deployment is having trouble. Check API health "
                f"and retry shortly.{provider_context} API said: {detail_message}"
            )
        return (
            "The platform API is having trouble. Retry shortly. "
            f"API said: {detail_message}"
        )
    return f"Platform API returned {status_code}: {detail_message}"


def _conflict_message(detail: Any, headers: Mapping[str, str]) -> str:
    """Return an actionable conflict explanation for replay/idempotency collisions."""
    retry_after = _header_value(headers, "Retry-After") or _detail_value(detail, "retry_after")
    parts = ["The request conflicts with an in-flight or already-used operation."]
    if retry_after:
        parts.append(f"Retry after {retry_after} seconds with a fresh request.")
    else:
        parts.append("Wait for the in-flight operation to finish, then retry with a fresh request.")
    parts.append(f"API said: {_detail_message(detail)}")
    return " ".join(parts)


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


def _provider_failure_context(detail: Any) -> str:
    """Return provider-specific recovery hints when an API error includes stable fields."""

    provider = _detail_value(detail, "provider") or _detail_value(detail, "model_provider")
    model = _detail_value(detail, "model") or _detail_value(detail, "requested_model")
    source = _detail_value(detail, "model_provider_source") or _detail_value(
        detail,
        "provider_source",
    )
    actions = _detail_actions(detail)
    if not actions:
        if provider:
            actions.extend([
                f"genaug model-providers check --project <project> --provider {provider}",
                "genaug projects runtime-policy --project <project> --json",
            ])
        elif model:
            actions.append("genaug projects runtime-policy --project <project> --json")
    if not any((provider, model, source, actions)):
        return ""

    parts = []
    context_fields = []
    if provider:
        context_fields.append(f"provider={provider}")
    if model:
        context_fields.append(f"model={model}")
    if source:
        context_fields.append(f"source={source}")
    if context_fields:
        parts.append(f"Provider context: {', '.join(context_fields)}.")
    if actions:
        parts.append(f"Next actions: {'; '.join(actions[:3])}.")
    return f" {' '.join(parts)}"


def _detail_actions(detail: Any) -> list[str]:
    """Extract bounded command-style next actions from a structured API error."""

    actions: list[str] = []
    for candidate in _detail_candidates(detail):
        raw_actions = candidate.get("next_actions") or candidate.get("remediation")
        if isinstance(raw_actions, str) and raw_actions.strip():
            actions.append(raw_actions.strip())
        elif isinstance(raw_actions, list):
            for item in raw_actions:
                if isinstance(item, str) and item.strip():
                    actions.append(item.strip())
                elif isinstance(item, dict):
                    command = item.get("command") or item.get("next_action")
                    if isinstance(command, str) and command.strip():
                        actions.append(command.strip())
    return list(dict.fromkeys(actions))


def _detail_value(detail: Any, key: str) -> str | None:
    """Find a stable value in API error payloads."""
    for candidate in _detail_candidates(detail):
        value = candidate.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _detail_candidates(detail: Any) -> list[dict[str, Any]]:
    """Return top-level and nested structured error payloads."""

    if not isinstance(detail, dict):
        return []
    candidates = [detail]
    for nested_key in ("detail", "error"):
        nested = detail.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested)
    return candidates


def _header_value(headers: Mapping[str, str], key: str) -> str | None:
    """Return a header value without depending on header casing."""
    for header_key, value in headers.items():
        if header_key.lower() == key.lower():
            return value
    return None


def _detail_message(detail: Any) -> str:
    """Return a readable API detail string."""
    if isinstance(detail, dict):
        direct_detail = detail.get("detail")
        if isinstance(direct_detail, str) and direct_detail:
            return direct_detail
        direct_error = detail.get("error")
        if isinstance(direct_error, str) and direct_error:
            return direct_error
        message = _detail_value(detail, "message")
        if message:
            return message
    return str(detail)
