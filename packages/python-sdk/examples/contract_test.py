"""Mock-backed contract example for the General Augment Python SDK.

Run `uv run --project packages/cli genaug mock --host 127.0.0.1 --port 8787 --quiet`
from the repository root, then run this file with GENAUG_API_BASE_URL pointed at the
mock server.
"""

from __future__ import annotations

import os

from genaug import GeneralAugmentClient, response_output_text


def main() -> None:
    """Exercise Responses and memory against the local mock contract."""
    client = GeneralAugmentClient(
        api_key=os.getenv("GENAUG_API_KEY", "local-test"),
        base_url=os.getenv("GENAUG_API_BASE_URL", "http://127.0.0.1:8787"),
    )

    response = client.create_response(
        {
            "model": "balanced",
            "user": "sdk-contract-user",
            "input": "Reply exactly with: local-mock-ok",
            "metadata": {"fixture": "sdk-contract"},
        },
        idempotency_key="sdk-contract-turn-1",
        request_id="req_sdk_contract_py",
    )
    assert response["status"] == "completed"
    assert response_output_text(response)

    stored = client.store_memory(
        {
            "user_id": "sdk-contract-user",
            "fact": "User prefers window seats",
            "fact_type": "preference",
            "idempotency_key": "sdk-contract-memory-1",
        }
    )
    assert stored.get("memory_id") or stored.get("id")

    search = client.search_memory(
        {"user_id": "sdk-contract-user", "query": "seat preference", "limit": 3}
    )
    assert isinstance(search.get("facts"), list)

    print("General Augment Python SDK contract example passed.")


if __name__ == "__main__":
    main()
