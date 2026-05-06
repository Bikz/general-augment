import os

from genaug import GeneralAugmentClient, response_output_text


client = GeneralAugmentClient(
    api_key=os.environ["GENAUG_API_KEY"],
    base_url=os.getenv("GENAUG_API_BASE_URL", "https://api.generalaugment.com"),
)

response = client.create_response(
    {
        "model": "balanced",
        "user": "app-user-123",
        "input": "Reply with a concise welcome message.",
        "metadata": {"example": "python"},
    },
    idempotency_key="example-python-response-1",
)

print(response_output_text(response))
