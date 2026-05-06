# General Augment Python SDK

Backend SDK for General Augment app integrations. Use it from trusted server code.
Project-scoped keys are for app traffic such as Responses and memory calls. Admin and
setup helpers require a management/admin-capable key and send it as `X-Admin-Key`.

```bash
pip install general-augment-sdk
```

```python
import os

from genaug import (
    GeneralAugmentClient,
    __version__,
    response_output_text,
    response_structured_output,
)

client = GeneralAugmentClient(
    api_key=os.environ["GENAUG_API_KEY"],
    base_url=os.getenv("GENAUG_API_BASE_URL", "https://api.generalaugment.com"),
)

print(f"General Augment SDK {__version__}")
```

## Responses

```python
response = client.create_response(
    {
        "model": "balanced",
        "user": "app-user-123",
        "input": "Reply with a concise onboarding summary.",
        "metadata": {"feature": "onboarding"},
    },
    idempotency_key="onboarding-turn-1",
    request_id="req_app_123",
)

print(response_output_text(response))
```

Structured output:

```python
structured_response = client.create_response(
    {
        "model": "balanced",
        "user": "app-user-123",
        "input": "Extract the user's preference: window seat.",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "preference",
                "strict": True,
                "schema": {
                    "type": "object",
                    "required": ["seat"],
                    "properties": {"seat": {"type": "string"}},
                    "additionalProperties": False,
                },
            }
        },
    }
)

preference = response_structured_output(structured_response)
```

Streaming:

```python
for event in client.stream_response(
    {
        "model": "balanced",
        "user": "app-user-123",
        "input": "Draft a two sentence welcome message.",
    }
):
    if event["event"] == "response.output_text.delta":
        print(event["data"].get("delta", ""), end="")
```

## Memory

```python
stored = client.store_memory(
    {
        "user_id": "app-user-123",
        "fact": "User prefers window seats",
        "fact_type": "preference",
        "importance_score": 0.9,
        "idempotency_key": "memory-window-seat-1",
    }
)

client.search_memory({"user_id": "app-user-123", "query": "seat preference"})
client.memory_profile("app-user-123")
client.delete_memory(str(stored["memory_id"]), user_id="app-user-123")
client.purge_user_memory("app-user-123")
```

## Usage

```python
usage = client.usage("project_123", start_date="2026-04-01", end_date="2026-04-24")
print(usage["totals"])
```

## Error Handling

```python
from genaug import GeneralAugmentAPIError

try:
    client.create_response({"model": "balanced", "input": "Hello"})
except GeneralAugmentAPIError as exc:
    if exc.reason == "rate_limit_exceeded":
        print(f"Retry after {exc.retry_after} seconds")
    print(exc.request_id, exc.trace_id, exc.detail)
```

`GeneralAugmentAPIError` preserves the HTTP status, stable `reason`/`code` when the
API returns one, `Retry-After`, `X-RateLimit-*`, request/trace IDs, and the decoded JSON
body. Existing code that only reads `status_code` or `detail` keeps working.

## Local Tests

Run the local mock server and point the SDK at it:

```bash
uv run --project packages/cli genaug mock --host 127.0.0.1 --port 8787 --quiet
export GENAUG_API_BASE_URL="http://127.0.0.1:8787"
export GENAUG_API_KEY="local-test"
PYTHONPATH=src python examples/contract_test.py
```

The contract example covers a Responses turn plus memory store/search against the same
deterministic routes used by app backend CI.

## Other Helpers

The SDK also includes `create_project_from_config`, `register_openapi_tools`,
`link_user`, `usage`, and `test_agent` for admin and integration workflows.
