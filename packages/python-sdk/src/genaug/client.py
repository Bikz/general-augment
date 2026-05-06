"""Typed API client for General Augment.

The SDK targets the public admin and integration APIs exposed by the General Augment platform:

- `/api/v1/admin/*` for project, usage, logs, config, and test-message operations
- `/api/v1/integrations/*` for app-user identity linking

It also wraps `/v1/responses` and `/api/v1/agent/memory/*` for app backend
integrations. See https://docs.generalaugment.com/sdk/reference/ for end-to-end
examples and https://docs.generalaugment.com/markdown/sdk/reference.md for the
Markdown export.
"""

from __future__ import annotations

import json as json_module
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

ADMIN_API_KEY_HEADER = "X-Admin-Key"
BEARER_AUTH_HEADER = "Authorization"
ADMIN_PREFIX = "/api/v1/admin"
INTEGRATIONS_PREFIX = "/api/v1/integrations"
DEFAULT_BASE_URL = "https://api.generalaugment.com"


class GeneralAugmentAPIError(RuntimeError):
    """Raised when the General Augment API returns an error response."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str | None = None,
        reason: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        retry_after: str | None = None,
        rate_limit: Mapping[str, str] | None = None,
        body: Any | None = None,
    ) -> None:
        """Create an API error with the HTTP status and response detail."""
        super().__init__(f"General Augment API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.reason = reason
        self.request_id = request_id
        self.trace_id = trace_id
        self.retry_after = retry_after
        self.rate_limit = dict(rate_limit or {})
        self.body = body


class GeneralAugmentClient:
    """Synchronous client for the General Augment API.

    Args:
        api_key: Admin API key. Project-scoped keys are supported.
        base_url: General Augment API base URL.
        timeout: Request timeout in seconds.
        client: Optional injected `httpx.Client`, useful for tests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        """Initialize the General Augment API client."""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        """Close the underlying HTTP client if the SDK created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> GeneralAugmentClient:
        """Return the context-managed client."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the client on context-manager exit."""
        self.close()

    def admin_request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call an admin API endpoint and return decoded JSON."""
        return self._request(method, f"{ADMIN_PREFIX}{path}", json=json, params=params)

    def integration_request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a developer integration API endpoint and return decoded JSON."""
        return self._request(method, f"{INTEGRATIONS_PREFIX}{path}", json=json, params=params)

    def list_projects(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return projects visible to this API key."""
        payload = self.admin_request(
            "GET",
            "/projects",
            params=_defined_params(
                {
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        if isinstance(payload, dict):
            items = payload.get("items", [])
            return [item for item in items if isinstance(item, dict)]
        return []

    def create_response(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        traceparent: str | None = None,
        tracestate: str | None = None,
    ) -> dict[str, Any]:
        """Create one Responses-compatible General Augment turn."""
        return _as_dict(
            self._request(
                "POST",
                "/v1/responses",
                json=payload,
                headers=_response_headers(
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    traceparent=traceparent,
                    tracestate=tracestate,
                ),
                auth="bearer",
            )
        )

    def stream_response(
        self,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
        traceparent: str | None = None,
        tracestate: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream semantic Responses SSE events."""
        body = dict(payload)
        body["stream"] = True
        try:
            with self._client.stream(
                "POST",
                f"{self.base_url}/v1/responses",
                headers=self._headers(
                    _response_headers(
                        idempotency_key=idempotency_key,
                        request_id=request_id,
                        traceparent=traceparent,
                        tracestate=tracestate,
                    ),
                    auth="bearer",
                ),
                json=body,
            ) as response:
                if response.is_error:
                    response.read()
                    raise _api_error_from_response(response)
                yield from _iter_sse_events(response.iter_lines())
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            raise _api_error_from_transport(exc) from exc

    def store_memory(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Store one durable memory fact for an app user."""
        return _as_dict(
            self._request(
                "POST",
                "/api/v1/agent/memory/store",
                json=payload,
                auth="bearer",
            )
        )

    def search_memory(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Search memory facts for an app user."""
        return _as_dict(
            self._request(
                "POST",
                "/api/v1/agent/memory/search",
                json=payload,
                auth="bearer",
            )
        )

    def memory_profile(self, user_id: str) -> dict[str, Any]:
        """Return profile and recent facts for one app user."""
        return _as_dict(
            self._request(
                "GET",
                f"/api/v1/agent/memory/profile/{_path_segment(user_id)}",
                auth="bearer",
            )
        )

    def delete_memory(self, memory_id: str, *, user_id: str) -> dict[str, Any]:
        """Delete one memory fact for the scoped app user."""
        return _as_dict(
            self._request(
                "DELETE",
                f"/api/v1/agent/memory/{_path_segment(memory_id)}",
                params={"user_id": user_id},
                auth="bearer",
            )
        )

    def purge_user_memory(self, user_id: str) -> dict[str, Any]:
        """Delete all memory facts for one app user."""
        return _as_dict(
            self._request(
                "DELETE",
                f"/api/v1/agent/memory/user/{_path_segment(user_id)}",
                auth="bearer",
            )
        )

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Return one project by ID."""
        return _as_dict(self.admin_request("GET", f"/projects/{_path_segment(project_id)}"))

    def create_project_from_config(
        self,
        yaml_content: str,
        *,
        soul_content: str | None = None,
        skills: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a project from a General Augment project YAML document."""
        return _as_dict(
            self.admin_request(
                "POST",
                "/projects/from-config",
                json={
                    "yaml_content": yaml_content,
                    "soul_content": soul_content,
                    "skills": skills or [],
                },
            )
        )

    def deploy_config_file(self, config_path: str | Path) -> dict[str, Any]:
        """Create a project from a local General Augment project YAML file."""
        content = Path(config_path).read_text(encoding="utf-8")
        return self.create_project_from_config(content)

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        """Patch mutable project fields."""
        payload = {key: value for key, value in fields.items() if value is not None}
        return _as_dict(
            self.admin_request("PATCH", f"/projects/{_path_segment(project_id)}", json=payload)
        )

    def integration_prompt(self, project_id: str) -> str:
        """Return the copy-paste AI coding agent integration prompt."""
        payload = _as_dict(
            self.admin_request("GET", f"/projects/{_path_segment(project_id)}/integration-prompt")
        )
        return str(payload.get("prompt", ""))

    def usage(
        self,
        project_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Return daily usage and billing aggregates for a project."""
        params = {
            key: value
            for key, value in {"start_date": start_date, "end_date": end_date}.items()
            if value is not None
        }
        return _as_dict(
            self.admin_request("GET", f"/projects/{_path_segment(project_id)}/usage", params=params)
        )

    def test_agent(
        self,
        project_id: str,
        message: str,
        *,
        phone_e164: str = "+15550000000",
        channel: str = "whatsapp",
    ) -> dict[str, Any]:
        """Send a test message to an agent without using a live channel webhook."""
        return _as_dict(
            self.admin_request(
                "POST",
                f"/projects/{_path_segment(project_id)}/test",
                json={"message": message, "phone_e164": phone_e164, "channel": channel},
            )
        )

    def link_user(
        self,
        project_id: str,
        *,
        phone: str,
        app_user_id: str,
        provider_name: str = "app",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Link an app user account to a WhatsApp/SMS phone number."""
        return _as_dict(
            self.integration_request(
                "POST",
                f"/{_path_segment(project_id)}/link-user",
                json={
                    "phone_e164": phone,
                    "provider_user_id": app_user_id,
                    "provider_name": provider_name,
                    "metadata": dict(metadata or {}),
                },
            )
        )

    def resolve_user(self, project_id: str, phone: str) -> dict[str, Any]:
        """Resolve a linked phone number to the external app user ID."""
        return _as_dict(
            self.integration_request(
                "GET",
                f"/{_path_segment(project_id)}/resolve/{_path_segment(phone)}",
            )
        )

    def unlink_user(self, project_id: str, phone: str) -> dict[str, Any]:
        """Remove a phone-to-app identity link."""
        return _as_dict(
            self.integration_request(
                "DELETE",
                f"/{_path_segment(project_id)}/unlink/{_path_segment(phone)}",
            )
        )

    def register_openapi_tools(
        self,
        project_id: str,
        spec_url: str,
        *,
        include_paths: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        target_count: int = 15,
        auto_deploy: bool = True,
    ) -> dict[str, Any]:
        """Ask General Augment to parse an OpenAPI spec and register curated generated tools."""
        return _as_dict(
            self.admin_request(
                "POST",
                f"/projects/{_path_segment(project_id)}/tools/from-openapi",
                json={
                    "spec_url": spec_url,
                    "include_paths": include_paths or [],
                    "exclude_paths": exclude_paths or [],
                    "target_count": target_count,
                    "auto_deploy": auto_deploy,
                },
            )
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: str = "admin",
    ) -> Any:
        """Execute a raw request against the General Augment API."""
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(headers, auth=auth),
                json=json,
                params=params,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
            raise _api_error_from_transport(exc) from exc
        if response.is_error:
            raise _api_error_from_response(response)
        if response.status_code == 204:
            return None
        return _success_body(response)

    def _headers(self, extra: Mapping[str, str] | None = None, *, auth: str) -> dict[str, str]:
        """Build request headers for admin or project-key app calls."""
        headers = {"Content-Type": "application/json"}
        if auth == "bearer":
            headers[BEARER_AUTH_HEADER] = f"Bearer {self.api_key}"
        else:
            headers[ADMIN_API_KEY_HEADER] = self.api_key
        headers.update(dict(extra or {}))
        return headers


__all__ = [
    "GeneralAugmentAPIError",
    "GeneralAugmentClient",
    "response_output_text",
    "response_structured_output",
]


def response_output_text(response: Mapping[str, Any]) -> str:
    """Return concatenated assistant output text from a Responses object."""
    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    text_parts: list[str] = []
    for part in _response_content_parts(response):
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        if part_type in {"output_text", "text"} and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    return "".join(text_parts)


def response_structured_output(response: Mapping[str, Any]) -> Any:
    """Return parsed structured output from a Responses object.

    The API may expose parsed structured output directly on a content part or as
    JSON text. This helper keeps app code from hand-walking the Responses shape.
    """
    if "output_parsed" in response:
        return response["output_parsed"]
    for part in _response_content_parts(response):
        if isinstance(part, Mapping) and "parsed" in part:
            return part["parsed"]
    text = response_output_text(response).strip()
    if not text:
        raise ValueError("Response output text is empty; no structured JSON to parse.")
    try:
        return json_module.loads(text)
    except json_module.JSONDecodeError as exc:
        raise ValueError("Response output text is not valid JSON.") from exc


def _as_dict(payload: Any) -> dict[str, Any]:
    """Return a JSON object payload or fail with a useful SDK error."""
    if isinstance(payload, dict):
        return payload
    raise TypeError(f"Expected General Augment API object response, got {type(payload).__name__}")


def _response_content_parts(response: Mapping[str, Any]) -> Iterator[Any]:
    """Yield content parts from a Responses object."""
    output = response.get("output")
    if not isinstance(output, list):
        return
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        yield from content


def _path_segment(value: str) -> str:
    """Encode one URL path segment safely."""
    return quote(value, safe="")


def _defined_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return query params with omitted optional values removed."""
    return {key: value for key, value in params.items() if value is not None}


def _api_error_from_response(response: httpx.Response) -> GeneralAugmentAPIError:
    """Build a rich SDK exception from a General Augment error response."""
    body = _response_body(response)
    return GeneralAugmentAPIError(
        response.status_code,
        _error_detail(body, response.text),
        code=_error_code(body),
        reason=_error_reason(body),
        request_id=response.headers.get("X-Request-ID") or _error_string(body, "request_id"),
        trace_id=(
            response.headers.get("X-Trace-ID")
            or response.headers.get("X-Trace-Id")
            or _error_string(body, "trace_id")
        ),
        retry_after=response.headers.get("Retry-After") or _error_retry_after(body),
        rate_limit=_rate_limit_headers(response.headers),
        body=body,
    )


def _api_error_from_transport(exc: httpx.HTTPError) -> GeneralAugmentAPIError:
    """Build a typed SDK exception for transport-level API failures."""
    if isinstance(exc, httpx.TimeoutException):
        detail = "General Augment API request timed out."
        reason = "request_timeout"
    elif isinstance(exc, httpx.ConnectError):
        detail = "General Augment API could not be reached."
        reason = "connection_failed"
    else:
        detail = "General Augment API request failed."
        reason = "request_failed"
    return GeneralAugmentAPIError(
        0,
        detail,
        reason=reason,
        body=None,
    )


def _success_body(response: httpx.Response) -> Any:
    """Decode a successful API response or raise a typed SDK parse error."""
    try:
        return response.json()
    except ValueError as exc:
        raise GeneralAugmentAPIError(
            response.status_code,
            "General Augment API returned malformed JSON.",
            reason="malformed_json",
            request_id=response.headers.get("X-Request-ID"),
            trace_id=response.headers.get("X-Trace-ID") or response.headers.get("X-Trace-Id"),
            body=response.text,
        ) from exc


def _response_body(response: httpx.Response) -> Any:
    """Decode an error response body when it is JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _error_detail(body: Any, fallback: str) -> str:
    """Extract a compact error detail from a decoded response body."""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json_module.dumps(detail, sort_keys=True)
        message = body.get("message")
        if isinstance(message, str):
            return message
        return json_module.dumps(body, sort_keys=True)
    if body is not None:
        return str(body)
    return fallback


def _error_reason(body: Any) -> str | None:
    """Return the stable reason/code field from an API error body when present."""
    error = _structured_error(body)
    if error is not None:
        reason = error.get("reason")
        if isinstance(reason, str):
            return reason
        code = error.get("code")
        if isinstance(code, str):
            return code
    if not isinstance(body, dict):
        return None
    for key in ("reason", "code", "error"):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return None


def _error_code(body: Any) -> str | None:
    """Return a machine-readable API error code when present."""
    error = _structured_error(body)
    if error is not None:
        code = error.get("code")
        if isinstance(code, str):
            return code
    if isinstance(body, dict):
        code = body.get("code")
        if isinstance(code, str):
            return code
    return None


def _error_string(body: Any, key: str) -> str | None:
    """Return one string field from structured or flat API error bodies."""
    error = _structured_error(body)
    if error is not None:
        value = error.get(key)
        if isinstance(value, str):
            return value
    if isinstance(body, dict):
        value = body.get(key)
        if isinstance(value, str):
            return value
    return None


def _error_retry_after(body: Any) -> str | None:
    """Return retry-after seconds from structured error JSON when present."""
    for key in ("retry_after", "retry_after_seconds"):
        value = _error_string(body, key)
        if value is not None:
            return value
        error = _structured_error(body)
        if error is not None and isinstance(error.get(key), (int, float)):
            return str(error[key])
        if isinstance(body, dict) and isinstance(body.get(key), (int, float)):
            return str(body[key])
    return None


def _structured_error(body: Any) -> dict[str, Any] | None:
    """Return the nested or flat structured API error object when present."""
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict):
        return detail
    error = body.get("error")
    if isinstance(error, dict):
        return error
    if any(isinstance(body.get(key), str) for key in ("code", "reason", "message")):
        return body
    return None


def _rate_limit_headers(headers: httpx.Headers) -> dict[str, str]:
    """Return rate-limit headers using stable snake_case keys."""
    mapping = {
        "limit": "X-RateLimit-Limit",
        "remaining": "X-RateLimit-Remaining",
        "reset": "X-RateLimit-Reset",
        "policy": "X-RateLimit-Policy",
    }
    return {key: value for key, header in mapping.items() if (value := headers.get(header))}


def _response_headers(
    *,
    idempotency_key: str | None,
    request_id: str | None,
    traceparent: str | None,
    tracestate: str | None,
) -> dict[str, str]:
    """Build optional Responses correlation headers."""
    headers: dict[str, str] = {}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    if request_id:
        headers["X-Request-ID"] = request_id
    if traceparent:
        headers["traceparent"] = traceparent
    if tracestate:
        headers["tracestate"] = tracestate
    return headers


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
