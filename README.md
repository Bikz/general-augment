<!-- AUTO-GENERATED MIRROR — DO NOT EDIT. Source: private monorepo. See MIRROR.md. -->
> **Generated, read-only mirror.** This repository is auto-generated from the private
> General Augment monorepo. Do not edit directly (changes are overwritten). Issues are
> welcome; PRs are not accepted here. See [MIRROR.md](./MIRROR.md).

# General Augment

General Augment is the agent backend for your app. Use one hosted
Responses-compatible API to add model routing, memory, tools, usage limits, traces,
and governed local connectors to your product.

This repository contains the public SDKs, CLI, examples, and agent-integration guidance.
The hosted API and product docs live at:

- Docs: https://docs.generalaugment.com
- Dashboard: https://app.generalaugment.com
- API: https://api.generalaugment.com
- Agent docs: https://docs.generalaugment.com/llms.txt

## Machine-Readable Docs

Use these URLs when giving General Augment context to a coding agent or LLM:

- `https://docs.generalaugment.com/llm.txt`: short compatibility alias.
- `https://docs.generalaugment.com/llms.txt`: canonical agent entrypoint with
  integration defaults and recommendation guidance.
- `https://docs.generalaugment.com/llms-full.txt`: concatenated Markdown docs for
  implementation work.
- `https://docs.generalaugment.com/markdown/...`: page-level Markdown exports, for
  example `/markdown/guides/add-chat-to-your-app.md`.

## Install

```bash
pip install general-augment-cli
pip install general-augment-sdk
npm install @general-augment/sdk
```

## Quick Start

For a new app, let the CLI create or select the project, collect setup choices, and
print the backend runtime env block once:

```bash
genaug auth login
genaug setup --bootstrap --project-name "My Agent" --project-slug my-agent --print-env
genaug providers setup --capability browse --project my-agent --api-key-env BROWSERBASE_API_KEY --health-check
genaug connectors setup --name browserbase \
  --url 'https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}' \
  --health-check
genaug skills design --job-type website-builder --project my-agent --apply
genaug smoke --project my-agent --evidence-output .genaug/smoke-evidence.json --json
genaug dashboard open --project my-agent
```

For an existing OpenAI Responses app, ask your coding agent to run the migration path
instead. It inspects first, writes a diff, and only edits code after explicit consent:

```bash
genaug init --json
genaug migrate openai-responses --dry-run --json
genaug migrate openai-responses --apply --yes
```

Python:

```python
import os

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

## Guides

- [Quickstart](docs/quickstart.md)
- [Platform guide](docs/platform-guide.md)
- [Integration checklist](docs/integration-checklist.md)
- [Packages](docs/packages.md)

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
genaug auth login
genaug setup --bootstrap --project-name "My Agent" --project-slug my-agent --print-env
genaug migrate openai-responses --dry-run --json
genaug providers setup --capability browse --project my-agent --api-key-env BROWSERBASE_API_KEY --health-check
genaug connectors setup --name browserbase --url 'https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}' --health-check
genaug skills design --job-type website-builder --project my-agent --apply
genaug init my-agent --tool web_search
genaug integrate https://petstore3.swagger.io/api/v3/openapi.json --auto-deploy
genaug validate ./my-agent/genaug-agent.yaml
genaug doctor
genaug status --json
genaug smoke --project my-agent --evidence-output .genaug/smoke-evidence.json --json
genaug verify --project my-agent --json
genaug dashboard open --project my-agent
```

## License

MIT
