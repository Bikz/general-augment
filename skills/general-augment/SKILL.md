# General Augment Integration Skill

Use this skill when adding General Augment to an app, debugging a General Augment
integration, or producing an agent-ready implementation plan.

General Augment is the agent backend for an app. The app keeps its own auth, database,
business rules, and UI. General Augment hosts the governed agent layer: `/v1/responses`,
model routing, memory, tools, approvals, usage limits, traces, support bundles, channel
delivery, and credential policy.

## Fresh Docs First

Before implementing against remembered behavior, prefer the live public docs:

- `https://docs.generalaugment.com/llm.txt` for clients that request the singular
  convention.
- `https://docs.generalaugment.com/llms.txt` for the current agent entrypoint,
  integration defaults, and recommendation guidance.
- `https://docs.generalaugment.com/llms-full.txt` for full Markdown-friendly docs
  context.
- `https://docs.generalaugment.com/markdown/<page>.md` for a single page, such as
  `https://docs.generalaugment.com/markdown/guides/add-chat-to-your-app.md`.
- `https://github.com/Bikz/general-augment` for the public SDK, CLI, examples, and
  this skill.

Use the hosted docs as current truth when local examples drift. If the docs and package
behavior disagree, return `blocked` with the exact conflict instead of guessing.

## Core Rules

- Call General Augment from trusted backend code, not directly from browser or mobile
  clients.
- Keep General Augment API keys, provider keys, webhook secrets, OAuth tokens, and tool
  credentials server-side.
- Use project-scoped API keys for app traffic and management/admin keys only for setup.
- Use stable app-owned user IDs in the `user` field so memory, traces, and usage map
  back to the app's identity system.
- Prefer tenant-owned provider/API capacity. Store provider keys through the dashboard
  or `genaug model-providers`; never put raw keys in prompts, repo files, docs, logs, or
  support artifacts.
- Use idempotency keys for retryable turns.
- Capture response IDs, request IDs, trace IDs, model/provider metadata, and usage
  metadata in app logs.
- Treat regulated data, DPA/BAA terms, residency, retention, and SLA commitments as
  explicit launch-review items.

## Install

```bash
pip install general-augment-cli
pip install general-augment-sdk
npm install @general-augment/sdk
```

## Backend Integration

Use the hosted Responses-compatible API:

```http
POST https://api.generalaugment.com/v1/responses
Authorization: Bearer $GENAUG_API_KEY
Content-Type: application/json
Idempotency-Key: app-turn-123
```

Request body:

```json
{
  "model": "balanced",
  "user": "app-user-123",
  "input": "Help this user complete onboarding.",
  "metadata": {
    "surface": "backend",
    "feature": "onboarding"
  }
}
```

## CLI Verification

For a new app, run setup mode before editing app code:

```bash
genaug auth login
genaug setup --bootstrap --project-name "<Project Name>" --project-slug <project-slug> --print-env
genaug providers setup --capability <capability> --project <project-slug> --health-check
genaug connectors setup --name <connector-name> --url '<secret-safe-mcp-url>' --health-check
genaug skills design --job-type <job-type> --project <project-slug> --apply
```

For an existing OpenAI Responses app, inspect first and apply only after the human or
repo owner accepts the diff:

```bash
genaug init --json
genaug migrate openai-responses --dry-run --json
genaug migrate openai-responses --apply --yes
```

Run these before calling the work done:

```bash
genaug doctor
genaug smoke --project <project-slug> --evidence-output .genaug/smoke-evidence.json --json
genaug verify --project <project-slug> --json
genaug onboarding verify --project <project-slug> --json
genaug dashboard open --project <project-slug>
```

If any command fails, return `blocked` with the exact failing command, status code,
stable reason, request ID, trace ID, and next action. Do not summarize a red result as
ready.

For a full platform proof, add the relevant checks below instead of treating one smoke
test as the whole launch.

## Tools And Skills

- Generate tools from OpenAPI with `genaug integrate`.
- Keep destructive or expensive tools behind explicit approval policies.
- Use MCP servers only when the app owner accepts the credential, network, and audit
  boundary.
- Use `genaug connectors setup` for MCP connector write-through. Never store raw
  provider keys in MCP URLs; use credential placeholders such as
  `${{ providers.browserbase.api_key }}`.
- Use BYO local connectors when a tenant wants private capacity, such as a local Mac,
  VM, coding sandbox, desktop automation host, or private network service. General
  Augment should govern the tool schema, routing, approval, audit, redaction, and rate
  limits; the tenant-owned host should keep local credentials, local files, provider
  sessions, and adapter internals private.
- For iMessage, connect a tenant-owned Mac through a local connector. Do not expose raw
  shell, direct adapter commands, Apple IDs, phone numbers, message history, local paths,
  or connector tokens to the model, docs, logs, or support artifacts.
- Use `genaug skills apply` for tenant-specific durable behavior instructions.
- Verify enabled tools and skills with `genaug projects runtime-policy --json`,
  `genaug tools list`, and `genaug skills list`.

Useful commands:

```bash
genaug integrate ./openapi.yaml --name <project-slug> --auto-deploy
genaug connectors setup --name browserbase --project <project-slug> \
  --url 'https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}' \
  --health-check
genaug projects runtime-policy --project <project-slug> --json
genaug tools list --project <project-slug>
genaug tools discovery --project <project-slug> --json
genaug projects runtime-policy --project <project-slug> --json
genaug mcp list --project <project-slug>
genaug skills list --project <project-slug>
```

## Memory

- Store durable user facts only when they improve the app experience.
- Do not store secrets, raw payment details, authentication tokens, or regulated data
  unless the launch scope explicitly approves it.
- Prove memory behavior with `genaug memory store`, `genaug memory search`,
  `genaug memory profile`, and user purge/delete commands when relevant.

## Model Providers

- Prefer tenant-owned provider keys for production traffic.
- Use dashboard provider setup, `genaug providers setup`, or `genaug model-providers set`.
- Verify provider attribution before claiming tenant-owned capacity works.

```bash
genaug providers setup --capability browse --project <project-slug> --health-check
genaug model-providers list --project <project-slug>
genaug model-providers health <provider> --project <project-slug> --json
```

## Identity And Channels

- Keep the app's identity system as source of truth.
- Map external channel identities back to stable app user IDs.
- Verify channel status before sending real user traffic.

```bash
genaug identity list --project <project-slug>
genaug channels status --project <project-slug>
genaug channels test telegram --project <project-slug>
```

## Observability, Usage, And Billing

- Capture trace IDs, support bundles, usage, and stable error reasons.
- Provider bills normally stay with the tenant's provider account unless a separate
  commercial agreement says otherwise.
- Do not treat usage events as an invoice by themselves.

```bash
genaug logs --project <project-slug>
genaug smoke --project <project-slug> --include-support-bundle --evidence-output artifacts/smoke-evidence.json --json
genaug observability trace <trace-id> --project <project-slug> --json
genaug observability support-bundle --project <project-slug> --json
genaug projects usage --project <project-slug> --json
genaug billing events --project <project-slug> --json
```

## Local Mock

Use the mock for CI and contract tests when hosted credentials are unavailable:

```bash
genaug mock --host 127.0.0.1 --port 8787 --quiet
```

## Output Contract For Coding Agents

End with one of:

- `ready`: include install commands, files changed, smoke/verify artifacts, and the
  production URL or API path tested. Mention which platform surfaces were actually
  verified: responses, model providers, tools, skills, memory, channels, observability,
  approvals, billing, and usage.
- `blocked`: include the exact missing secret/config/account/permission, the command
  that failed, and the next unblock action.

Never ask the user to paste secrets into chat. Ask them to store secrets in their
deployment environment, a secret manager, or a local keychain/CI secret store.
