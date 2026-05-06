"""Minimal General Augment Python SDK quickstart."""

from __future__ import annotations

import os
from pathlib import Path

from genaug import GeneralAugmentClient, response_output_text
from genaug.identity import link_user
from genaug.tools import register_from_openapi


def main() -> None:
    """Deploy a config, register tools, link a user, and call Responses."""
    api_key = os.environ["GENAUG_API_KEY"]
    spec_url = os.environ["OPENAPI_SPEC_URL"]
    with GeneralAugmentClient(api_key=api_key) as client:
        project = client.deploy_config_file(Path("genaug-agent.yaml"))
        register_from_openapi(spec_url, client=client, project_id=str(project["id"]))
        link_user(
            client,
            str(project["id"]),
            phone="+15551234567",
            app_user_id="app-user-123",
        )
        response = client.create_response(
            {
                "model": "balanced",
                "user": "app-user-123",
                "input": "Reply with a concise onboarding summary.",
                "metadata": {"feature": "sdk-quickstart"},
            },
            idempotency_key="sdk-quickstart-turn-1",
            request_id="req_sdk_quickstart",
        )
        print(response_output_text(response))


if __name__ == "__main__":
    main()
