"""Thin HTTP client for the standalone CLI."""

from __future__ import annotations

import json as json_module
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from platform_cli.config import CLIConfig
from platform_cli.errors import APIError, CLIError

ADMIN_PREFIX = "/api/v1/admin"
INTEGRATIONS_PREFIX = "/api/v1/integrations"
REQUEST_TIMEOUT_SECONDS = 30.0
PROJECT_LIST_PAGE_SIZE = 1000


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

    def installer(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> Any:
        """Call an installer-auth endpoint."""
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return self._request(
            method,
            f"/api/v1/installer{path}",
            json=json,
            extra_headers=headers,
            authenticated=False,
        )

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

    def app_event_stream(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream semantic SSE events from an app-facing endpoint."""
        if not self.config.api_key:
            raise CLIError("No API key configured. Run genaug auth login first.")
        request_headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            **dict(headers or {}),
        }
        try:
            with self._client.stream(
                "GET",
                f"{self.base_url}{path}",
                headers=request_headers,
                params=dict(params) if params is not None else None,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    raise APIError(
                        response.status_code,
                        _error_detail(response),
                        response.headers,
                        request_path=path,
                        auth_mode="bearer",
                    )
                yield from _iter_sse_events(response.iter_lines())
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            raise CLIError(f"Could not reach the platform API at {self.base_url}: {exc}") from exc

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
            raise APIError(
                response.status_code,
                _error_detail(response),
                response.headers,
                request_path=path,
                auth_mode=auth_mode if authenticated else None,
            )
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
    if _is_uuid(project_ref):
        try:
            payload = client.admin("GET", f"/projects/{encode_path_segment(project_ref)}")
        except APIError as exc:
            if exc.status_code != 404:
                raise
        else:
            if isinstance(payload, dict):
                return payload

    offset = 0
    while True:
        payload = client.admin(
            "GET",
            "/projects",
            params={"limit": PROJECT_LIST_PAGE_SIZE, "offset": offset},
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidates = {
                str(value)
                for value in (item.get("id"), item.get("slug"), item.get("name"))
                if value not in (None, "")
            }
            if project_ref in candidates:
                return item
        if not isinstance(items, list) or len(items) < PROJECT_LIST_PAGE_SIZE:
            break
        offset += PROJECT_LIST_PAGE_SIZE
    raise CLIError(f"Project not found: {project_ref}")


def encode_path_segment(value: str) -> str:
    """Encode one path segment for safe URL interpolation."""
    return quote(value, safe="")


def _is_uuid(value: str) -> bool:
    """Return whether a project reference is a UUID."""
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _error_detail(response: httpx.Response) -> Any:
    """Extract one error detail from an HTTP response."""
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        return payload.get("detail") or payload
    return payload


def _iter_sse_events(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Parse semantic SSE events from an iterator of text lines."""
    event = "message"
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            parsed = _sse_event(event, data_lines)
            if parsed is not None:
                yield parsed
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").lstrip())
    parsed = _sse_event(event, data_lines)
    if parsed is not None:
        yield parsed


def _sse_event(event: str, data_lines: list[str]) -> dict[str, Any] | None:
    """Return one parsed SSE event or None for empty blocks."""
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    try:
        parsed_data: Any = json_module.loads(data)
    except json_module.JSONDecodeError:
        parsed_data = data
    return {"event": event, "data": parsed_data}
