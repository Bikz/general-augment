"""Thin HTTP client for the standalone CLI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from platform_cli.config import CLIConfig
from platform_cli.errors import APIError, CLIError

ADMIN_PREFIX = "/api/v1/admin"
INTEGRATIONS_PREFIX = "/api/v1/integrations"
REQUEST_TIMEOUT_SECONDS = 30.0


class PlatformClient:
    """Synchronous HTTP client for platform admin and public endpoints."""

    def __init__(self, config: CLIConfig, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        """Initialize the client from CLI config."""
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> PlatformClient:
        """Return this client in context managers."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close this client."""
        self.close()

    def admin(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call an admin API endpoint."""
        if not self.config.api_key:
            raise CLIError("No API key configured. Run genaug auth login first.")
        return self._request(
            method,
            f"{ADMIN_PREFIX}{path}",
            json=json,
            params=params,
            authenticated=True,
        )

    def public(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a public API endpoint."""
        return self._request(method, path, params=params, authenticated=False)

    def integrations(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call an app-integration endpoint with admin API-key auth."""
        if not self.config.api_key:
            raise CLIError("No API key configured. Run genaug auth login first.")
        return self._request(
            method,
            f"{INTEGRATIONS_PREFIX}{path}",
            json=json,
            params=params,
            authenticated=True,
        )

    def app(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Call an app-facing endpoint using bearer auth."""
        if not self.config.api_key:
            raise CLIError("No API key configured. Run genaug auth login first.")
        return self._request(
            method,
            path,
            json=json,
            params=params,
            extra_headers=headers,
            authenticated=True,
            auth_mode="bearer",
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        authenticated: bool,
        auth_mode: str = "admin",
    ) -> Any:
        """Send one request and decode the response."""
        headers: dict[str, str] = {}
        if authenticated and self.config.api_key:
            if auth_mode == "bearer":
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            else:
                headers["X-Admin-Key"] = self.config.api_key
        headers.update(dict(extra_headers or {}))
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=dict(json) if json is not None else None,
                params=dict(params) if params is not None else None,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            raise CLIError(f"Could not reach the platform API at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise APIError(response.status_code, _error_detail(response), response.headers)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            if authenticated:
                raise CLIError(
                    "Platform API returned malformed JSON for an authenticated request."
                ) from exc
            return response.text


def resolve_project(client: PlatformClient, project_ref: str) -> dict[str, Any]:
    """Resolve a project by id, slug, or name."""
    payload = client.admin("GET", "/projects")
    items = payload.get("items", []) if isinstance(payload, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if project_ref in {str(item.get("id")), str(item.get("slug")), str(item.get("name"))}:
            return item
    raise CLIError(f"Project not found: {project_ref}")


def encode_path_segment(value: str) -> str:
    """Encode one path segment for safe URL interpolation."""
    return quote(value, safe="")


def _error_detail(response: httpx.Response) -> Any:
    """Extract one error detail from an HTTP response."""
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        return payload.get("detail") or payload
    return payload
