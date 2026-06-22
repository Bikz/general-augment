"""Tests for General Augment SDK URL construction."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genaug import (
    UNSET,
    GeneralAugmentAPIError,
    GeneralAugmentClient,
    response_output_text,
    response_structured_output,
)
from genaug import client as client_module


def test_request_paths_encode_reserved_segments() -> None:
    """Dynamic path segments should be URL-encoded before request dispatch."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"id": "proj/1"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    client.get_project("proj/1")

    def integration_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"phone": "+1555/1"})

    http_client = httpx.Client(transport=httpx.MockTransport(integration_handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)
    client.resolve_user("proj/1", "+1555/1")

    assert seen == [
        "http://api.test/api/v1/admin/projects/proj%2F1",
        "http://api.test/api/v1/integrations/proj%2F1/resolve/%2B1555%2F1",
    ]


def test_create_response_uses_project_key_bearer_headers() -> None:
    """Responses calls should use app-integration bearer auth and correlation headers."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "resp_123", "status": "completed"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    response = client.create_response(
        {"model": "balanced", "input": "Hello", "user": "app-user-1"},
        idempotency_key="turn-1",
        request_id="req-app-1",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )

    assert response["id"] == "resp_123"
    assert seen["url"] == "http://api.test/v1/responses"
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert "x-admin-key" not in seen["headers"]
    assert seen["headers"]["x-idempotency-key"] == "turn-1"
    assert seen["headers"]["x-request-id"] == "req-app-1"
    assert seen["headers"]["traceparent"].startswith("00-4bf92f")
    assert seen["json"]["user"] == "app-user-1"


def test_stream_response_sets_stream_and_parses_sse_events() -> None:
    """Streaming helper should request stream=true and yield parsed SSE events."""
    seen: dict[str, Any] = {}
    sse_body = (
        'event: response.created\ndata: {"type":"response.created"}\n\n'
        "event: response.completed\n"
        'data: {"type":"response.completed","response":{"id":"resp_123"}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, content=sse_body, headers={"Content-Type": "text/event-stream"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    events = list(client.stream_response({"model": "balanced", "input": "Hello"}))

    assert seen["json"]["stream"] is True
    assert seen["headers"]["authorization"] == "Bearer secret"
    assert [event["event"] for event in events] == ["response.created", "response.completed"]
    assert events[1]["data"]["response"]["id"] == "resp_123"


def test_memory_helpers_use_current_agent_memory_routes() -> None:
    """Memory helpers should hit the documented project-keyed memory paths."""
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                str(request.url),
                dict(request.headers),
            )
        )
        return httpx.Response(200, json={"ok": True})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    assert client.store_memory({"user_id": "app-user-1", "fact": "Likes tea"}) == {"ok": True}
    assert client.search_memory({"user_id": "app-user-1", "query": "tea"}) == {"ok": True}
    assert client.memory_profile("app/user") == {"ok": True}
    assert client.delete_memory("mem/1", user_id="app/user") == {"ok": True}
    assert client.purge_user_memory("app/user") == {"ok": True}

    assert [(method, url) for method, url, _ in seen] == [
        ("POST", "http://api.test/api/v1/agent/memory/store"),
        ("POST", "http://api.test/api/v1/agent/memory/search"),
        ("GET", "http://api.test/api/v1/agent/memory/profile/app%2Fuser"),
        ("DELETE", "http://api.test/api/v1/agent/memory/mem%2F1?user_id=app%2Fuser"),
        ("DELETE", "http://api.test/api/v1/agent/memory/user/app%2Fuser"),
    ]
    assert all(headers["authorization"] == "Bearer secret" for _, _, headers in seen)


def test_response_output_helpers_extract_text_and_structured_json() -> None:
    """Apps should not need to hand-walk Responses output content."""
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": '{"seat":"window"}'},
                    {"type": "refusal", "refusal": None},
                ],
            }
        ]
    }

    assert response_output_text(response) == '{"seat":"window"}'
    assert response_output_text({"output_text": "hello"}) == "hello"
    assert response_structured_output(response) == {"seat": "window"}
    assert response_structured_output({"output_parsed": {"seat": "aisle"}}) == {"seat": "aisle"}


def test_usage_helper_uses_admin_usage_endpoint_with_dates() -> None:
    """Usage helper should keep the project usage API easy to reach."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"project_id": "proj/1", "totals": {}})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    assert client.usage("proj/1", start_date="2026-04-01", end_date="2026-04-24") == {
        "project_id": "proj/1",
        "totals": {},
    }
    assert seen["url"] == (
        "http://api.test/api/v1/admin/projects/proj%2F1/usage?"
        "start_date=2026-04-01&end_date=2026-04-24"
    )
    assert seen["headers"]["x-admin-key"] == "secret"


def test_list_projects_accepts_pagination_params() -> None:
    """Project listing should expose the backend's bounded pagination controls."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"items": [{"id": "proj-1"}]})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    assert client.list_projects(limit=25, offset=50) == [{"id": "proj-1"}]
    assert seen["url"] == "http://api.test/api/v1/admin/projects?limit=25&offset=50"
    assert seen["headers"]["x-admin-key"] == "secret"


def test_api_error_exposes_reason_correlation_and_rate_limit_headers() -> None:
    """Apps should be able to switch on stable API reason codes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"detail": "Too many requests", "reason": "rate_limit_exceeded"},
            headers={
                "Retry-After": "30",
                "X-RateLimit-Limit": "1500",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1713974400",
                "X-Request-ID": "req_123",
                "X-Trace-ID": "trace_456",
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "secret", base_url="http://api.test", max_retries=0, client=http_client
    )

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        client.create_response({"model": "balanced", "input": "Hello"})

    error = exc_info.value
    assert error.status_code == 429
    assert error.detail == "Too many requests"
    assert error.code is None
    assert error.reason == "rate_limit_exceeded"
    assert error.retry_after == "30"
    assert error.request_id == "req_123"
    assert error.trace_id == "trace_456"
    assert error.rate_limit == {"limit": "1500", "remaining": "0", "reset": "1713974400"}
    assert error.body == {"detail": "Too many requests", "reason": "rate_limit_exceeded"}


def test_success_malformed_json_raises_typed_api_error() -> None:
    """Malformed successful responses should not leak raw JSON parser exceptions."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="not-json",
            headers={"X-Request-ID": "req_bad_json", "X-Trace-ID": "trace_bad_json"},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        client.create_response({"model": "balanced", "input": "Hello"})

    error = exc_info.value
    assert error.status_code == 200
    assert error.reason == "malformed_json"
    assert error.request_id == "req_bad_json"
    assert error.trace_id == "trace_bad_json"
    assert error.body == "not-json"


def test_transport_failure_raises_typed_api_error() -> None:
    """Network failures should use the SDK error type so app handlers can catch one class."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "secret", base_url="http://api.test", max_retries=0, client=http_client
    )

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        client.create_response({"model": "balanced", "input": "Hello"})

    assert exc_info.value.status_code == 0
    assert exc_info.value.reason == "connection_failed"
    assert exc_info.value.detail == "General Augment API could not be reached."


def test_api_error_reads_nested_detail_and_body_retry_metadata() -> None:
    """FastAPI-style structured detail bodies should expose stable SDK fields."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "idempotency_key_in_progress",
                    "reason": "idempotency_key_in_progress",
                    "message": "A request with this idempotency key is still processing.",
                    "request_id": "req_body_409",
                    "retry_after_seconds": 1,
                }
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        client.create_response({"model": "balanced", "input": "Hello"})

    error = exc_info.value
    assert error.status_code == 409
    assert error.code == "idempotency_key_in_progress"
    assert error.reason == "idempotency_key_in_progress"
    assert error.request_id == "req_body_409"
    assert error.retry_after == "1"


def test_stream_response_error_uses_same_api_error_shape() -> None:
    """Streaming failures should expose the same structured error metadata."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "detail": {"message": "Project budget exceeded"},
                "code": "project_budget_exceeded",
            },
            headers={"X-Request-ID": "req_budget"},
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        list(client.stream_response({"model": "balanced", "input": "Hello"}))

    assert exc_info.value.status_code == 402
    assert exc_info.value.reason == "project_budget_exceeded"
    assert exc_info.value.request_id == "req_budget"
    assert exc_info.value.detail == '{"message": "Project budget exceeded"}'


def test_stream_response_transport_failure_raises_typed_api_error() -> None:
    """Streaming network failures should use the same SDK error boundary."""

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        list(client.stream_response({"model": "balanced", "input": "Hello"}))

    assert exc_info.value.status_code == 0
    assert exc_info.value.reason == "connection_failed"


def test_mock_contract_flow_covers_responses_and_memory_fixtures() -> None:
    """The SDK should satisfy the same Responses and memory contract as the local mock."""
    calls: list[tuple[str, str, dict[str, str]]] = []
    memories: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, dict(request.headers)))
        if request.url.path == "/v1/responses":
            return httpx.Response(
                200,
                json={
                    "id": "resp_mock_contract",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "local-mock-ok"}],
                        }
                    ],
                    "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
                    "metadata": {"general_augment_model": "mock", "fixture": "sdk-contract"},
                },
            )
        if request.url.path == "/api/v1/agent/memory/store":
            body = json.loads(request.content)
            memories.append(body)
            return httpx.Response(200, json={"memory_id": "mem_mock_contract", **body})
        if request.url.path == "/api/v1/agent/memory/search":
            return httpx.Response(
                200,
                json={"user_id": "sdk-contract-user", "facts": memories},
            )
        if request.url.path == "/api/v1/agent/memory/profile/sdk-contract-user":
            return httpx.Response(
                200,
                json={"user_id": "sdk-contract-user", "recent_facts": memories},
            )
        return httpx.Response(404, json={"detail": f"unexpected path {request.url.path}"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "local-test",
        base_url="http://127.0.0.1:8787",
        client=http_client,
    )

    response = client.create_response(
        {
            "model": "balanced",
            "user": "sdk-contract-user",
            "input": "Reply exactly with: local-mock-ok",
        },
        idempotency_key="sdk-contract-turn-1",
        request_id="req_contract_py",
    )
    stored = client.store_memory(
        {
            "user_id": "sdk-contract-user",
            "fact": "User prefers window seats",
            "fact_type": "preference",
        }
    )
    search = client.search_memory(
        {"user_id": "sdk-contract-user", "query": "seat preference", "limit": 3}
    )
    profile = client.memory_profile("sdk-contract-user")

    assert response_output_text(response) == "local-mock-ok"
    assert stored["memory_id"] == "mem_mock_contract"
    assert len(search["facts"]) == 1
    assert profile["recent_facts"][0]["fact"] == "User prefers window seats"
    assert [(method, path) for method, path, _ in calls] == [
        ("POST", "/v1/responses"),
        ("POST", "/api/v1/agent/memory/store"),
        ("POST", "/api/v1/agent/memory/search"),
        ("GET", "/api/v1/agent/memory/profile/sdk-contract-user"),
    ]
    assert calls[0][2]["authorization"] == "Bearer local-test"


def test_create_response_retries_429_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient 429s should be retried with backoff that honors Retry-After."""
    slept: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: slept.append(seconds))

    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.headers["x-idempotency-key"])
        if len(attempts) == 1:
            return httpx.Response(
                429,
                json={"detail": "slow down"},
                headers={"Retry-After": "7"},
            )
        return httpx.Response(200, json={"id": "resp_ok"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "secret", base_url="http://api.test", max_retries=2, client=http_client
    )

    response = client.create_response({"model": "balanced", "input": "Hello"})

    assert response["id"] == "resp_ok"
    assert len(attempts) == 2
    # Same auto-generated idempotency key reused across retries -> safe replay.
    assert attempts[0] == attempts[1]
    # Retry-After (7s) honored, capped at the max backoff (8s).
    assert slept == [7.0]


def test_request_retries_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """503s should be retried up to max_retries with jittered backoff."""
    monkeypatch.setattr(client_module.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json={"items": [{"id": "p1"}]})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "secret", base_url="http://api.test", max_retries=3, client=http_client
    )

    assert client.list_projects() == [{"id": "p1"}]
    assert calls["n"] == 3


def test_retries_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_retries=0 should surface the first transient failure immediately."""
    monkeypatch.setattr(client_module.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "unavailable"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "secret", base_url="http://api.test", max_retries=0, client=http_client
    )

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        client.list_projects()
    assert exc_info.value.status_code == 503
    assert calls["n"] == 1


def test_connection_errors_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection failures should be retried before raising a typed error."""
    monkeypatch.setattr(client_module.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("network down")
        return httpx.Response(200, json={"items": []})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient(
        "secret", base_url="http://api.test", max_retries=2, client=http_client
    )

    assert client.list_projects() == []
    assert calls["n"] == 2


def test_create_response_auto_generates_idempotency_key() -> None:
    """create_response should auto-send X-Idempotency-Key when omitted."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-idempotency-key"]
        return httpx.Response(200, json={"id": "resp_1"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    client.create_response({"model": "balanced", "input": "Hello"})
    assert seen["key"]  # non-empty UUID


def test_create_response_preserves_caller_idempotency_key() -> None:
    """A caller-supplied idempotency key must win over auto-generation."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-idempotency-key"]
        return httpx.Response(200, json={"id": "resp_1"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    client.create_response({"model": "balanced", "input": "Hello"}, idempotency_key="mine-1")
    assert seen["key"] == "mine-1"


def test_parse_retry_after_handles_http_date() -> None:
    """Retry-After in HTTP-date form should parse to a non-negative delay."""
    assert client_module._parse_retry_after("30") == 30.0
    past = client_module._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT")
    assert past == 0.0  # date in the past clamps to 0
    assert client_module._parse_retry_after("not-a-date") is None
    assert client_module._parse_retry_after(None) is None


def test_stream_stops_cleanly_on_done_sentinel() -> None:
    """A [DONE] sentinel should end the stream without being yielded."""
    sse_body = (
        'event: response.created\ndata: {"type":"response.created"}\n\n'
        "data: [DONE]\n\n"
        'event: response.completed\ndata: {"type":"never"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"Content-Type": "text/event-stream"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    events = list(client.stream_response({"model": "balanced", "input": "Hello"}))
    assert [event["event"] for event in events] == ["response.created"]


def test_stream_mid_stream_error_frame_raises() -> None:
    """A mid-stream `event: error` frame should raise a typed API error."""
    sse_body = (
        'event: response.created\ndata: {"type":"response.created"}\n\n'
        'event: error\ndata: {"reason":"agent_failed","message":"boom"}\n\n'
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body, headers={"Content-Type": "text/event-stream"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    with pytest.raises(GeneralAugmentAPIError) as exc_info:
        list(client.stream_response({"model": "balanced", "input": "Hello"}))
    assert exc_info.value.reason == "agent_failed"
    assert "boom" in exc_info.value.detail


def test_update_project_omits_unset_but_sends_explicit_null() -> None:
    """UNSET fields are dropped; explicit None is PATCHed as null."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "p1"})

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    client = GeneralAugmentClient("secret", base_url="http://api.test", client=http_client)

    client.update_project("p1", name="New", description=None, skipped=UNSET)
    assert seen["json"] == {"name": "New", "description": None}
