# General Augment

General Augment is the agent backend for your app. Use one hosted
Responses-compatible API to add model routing, memory, tools, approvals, usage limits,
traces, and support receipts to your product.

This repository contains the public SDKs, CLI, examples, and agent-integration guidance.
The hosted API and product docs live at:

- Docs: https://docs.generalaugment.com
- Dashboard: https://app.generalaugment.com
- API: https://api.generalaugment.com

## Install

```bash
pip install general-augment-cli
pip install general-augment-sdk
npm install @general-augment/sdk
```

## Quick Start

Create a project in the dashboard, copy a project API key from a trusted server-side
environment, then call `/v1/responses` from your backend.

```bash
export GENAUG_API_KEY="ga_project_..."
export GENAUG_API_BASE_URL="https://api.generalaugment.com"
genaug smoke --message "Reply with: ok" --json
```

Python:

```python
from genaug import GeneralAugmentClient, response_output_text

client = GeneralAugmentClient(api_key=os.environ["GENAUG_API_KEY"])
response = client.create_response(
    {
        "model": "balanced",
        "user": "app-user-123",
        "input": "Write a concise onboarding welcome.",
        "metadata": {"surface": "backend"},
    },
    idempotency_key="welcome-app-user-123",
)
print(response_output_text(response))
```

TypeScript:

```ts
import { GeneralAugmentClient, responseOutputText } from "@general-augment/sdk";

const client = new GeneralAugmentClient({
  apiKey: process.env.GENAUG_API_KEY!,
});

const response = await client.createResponse(
  {
    model: "balanced",
    user: "app-user-123",
    input: "Write a concise onboarding welcome.",
    metadata: { surface: "backend" },
  },
  { idempotencyKey: "welcome-app-user-123" },
);

console.log(responseOutputText(response));
```

## Packages

- CLI: [`packages/cli`](packages/cli)
- Python SDK: [`packages/python-sdk`](packages/python-sdk)
- TypeScript SDK: [`packages/typescript-sdk`](packages/typescript-sdk)

## Agent Skill

Coding agents can use [`skills/general-augment/SKILL.md`](skills/general-augment/SKILL.md)
as their integration playbook. It covers server-side key handling, `/v1/responses`,
tenant-owned provider capacity, stable user IDs, tool governance, memory, traces, and
ready/blocked verification.

## Security Rules

- Keep General Augment API keys and provider keys server-side.
- Do not put keys in browser bundles, mobile apps, prompts, memory facts, docs, tickets,
  screenshots, or support artifacts.
- Use stable app-owned user IDs in the `user` field.
- Use idempotency keys for retryable backend turns.
- Capture response IDs, trace IDs, request IDs, and support receipts when debugging.
- Treat regulated data, DPA/BAA terms, residency, and SLA commitments as explicit
  launch-review items.

## Useful Commands

```bash
genaug auth login --api-key "$GENAUG_ADMIN_API_KEY"
genaug doctor
genaug projects list
genaug init my-agent --tool web_search
genaug validate ./my-agent/genaug-agent.yaml
genaug deploy ./my-agent/genaug-agent.yaml
genaug model-providers set openai --project my-agent --api-key "$OPENAI_API_KEY"
genaug smoke --project my-agent --json
genaug verify --project my-agent --json
genaug onboarding verify --project my-agent --json
```

## License

MIT
