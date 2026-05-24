# Platform Guide

General Augment is the hosted agent backend for an app. The app keeps its product
database, user authentication, business rules, and UI. General Augment provides the
governed agent layer around those app systems.

## Platform Surfaces

| Surface | What It Is For | Human Path | Agent/CLI Path |
| --- | --- | --- | --- |
| Dashboard | Project setup, API keys, model providers, tools, channels, usage, traces, users, billing | https://app.generalaugment.com | `genaug projects list` |
| Self-serve setup | Install/configure General Augment without code edits | CLI browser consent | `genaug auth login`, `genaug setup --bootstrap` |
| Safe migration | Patch an existing OpenAI Responses app after diff review | PR or local diff review | `genaug migrate openai-responses` |
| Responses API | App backend agent turns | App backend code | Python SDK, TypeScript SDK, raw HTTP |
| Capability providers | Tenant-owned coding, browser, search, video, and model/API capacity | Project provider panels | `genaug providers setup`, `genaug model-providers` |
| Tools | Governed app/API actions and BYO local connector capabilities | Project tools pages | `genaug integrate`, `genaug tools`, `genaug mcp`, `genaug connectors setup` |
| Skills | Durable tenant behavior guidance | Project skills pages | `genaug skills design`, `genaug skills apply` |
| Memory | User-scoped durable facts | User/memory views | SDK memory methods, `genaug memory` |
| Identity | Map app users to General Augment users and channels | Identity views | `genaug identity` |
| Channels | Telegram, WhatsApp, SMS, and other delivery surfaces | Channel setup flows | `genaug channels` |
| Observability | Traces, logs, support bundles, usage evidence | Observability views | `genaug logs`, `genaug observability` |
| Approvals | Human approval for sensitive tool actions | Approval queue | `genaug approvals` |
| Billing/usage | Plan limits, checkout/portal handoff, usage events | Billing and usage pages | `genaug billing`, `genaug projects usage` |

## Recommended Integration Order

1. Run `genaug auth login`.
2. Run `genaug setup --bootstrap` to create or select the project and print runtime env.
3. For existing OpenAI Responses apps, run `genaug migrate openai-responses --dry-run`
   and apply only after reviewing the diff.
4. Store the project runtime key in the app backend.
5. Run `genaug smoke --evidence-output .genaug/smoke-evidence.json --json` against `/v1/responses`.
6. Add tenant-owned model provider credentials when production traffic should use the
   tenant's own provider account.
7. Add generated OpenAPI tools, MCP servers, or BYO local connectors for private
   capacity.
8. Add skills and SOUL/personality guidance.
9. Add memory only for durable user facts the app is allowed to retain.
10. Add identity linking and external channels if users will interact outside the app UI.
11. Verify traces, usage, support bundles, and approval behavior.
12. Run `genaug verify --json`, `genaug onboarding verify --json`, and
    `genaug dashboard open --project <project>`.

## Responses API

Use `/v1/responses` from the app backend. Send a stable app user ID, model tier,
metadata, and idempotency key.

```bash
curl -sS https://api.generalaugment.com/v1/responses \
  -H "Authorization: Bearer $GENAUG_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: app-turn-123" \
  -d '{
    "model": "balanced",
    "user": "app-user-123",
    "input": "Help this user complete onboarding.",
    "metadata": {"surface": "backend"}
  }'
```

## Model Providers And Capacity

Production tenants should bring their own model-provider and cost-bearing third-party
API capacity unless a separate commercial agreement says General Augment will fund it.
Store provider keys through the dashboard or CLI; never put raw keys in code, prompts,
memory facts, docs, screenshots, or support artifacts.

```bash
genaug providers setup --capability browse --project my-agent --api-key-env BROWSERBASE_API_KEY --health-check
genaug model-providers set openai \
  --project my-agent \
  --api-key "$OPENAI_API_KEY"

genaug model-providers health openai --project my-agent --json
genaug model-providers list --project my-agent
```

## Tools, MCP, And Approvals

Use OpenAPI specs for app-owned APIs and MCP for external tool surfaces when the
credential and audit boundary is acceptable.

Use BYO local connectors for tenant-owned private capacity such as a Mac, VM, coding
sandbox, desktop automation host, or private network service. The connector keeps local
credentials and adapter internals private; General Augment exposes only governed tool
schemas to the runtime and handles approval, audit, redaction, policy, and rate limits.
iMessage should use this pattern through a tenant-owned Mac rather than raw shell or
direct adapter access.

```bash
genaug integrate ./openapi.yaml --name my-agent --auto-deploy
genaug connectors setup --name browserbase \
  --url 'https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}' \
  --health-check
genaug tools list --project my-agent
genaug tools toggle delete_account --project my-agent --disable
genaug tools discovery --project my-agent --mode always --json
genaug mcp add github --project my-agent --url https://example.com/mcp
genaug mcp test github --project my-agent
```

For destructive, expensive, or user-visible actions, require approvals:

```bash
genaug approvals list --project my-agent --json
genaug approvals approve <approval-id> --project my-agent --yes
genaug approvals deny <approval-id> --project my-agent --yes
```

## Skills And Behavior

Use skills for durable project behavior and repeatable operating rules.

```bash
genaug skills design --job-type website-builder --project my-agent --apply
genaug skills list --project my-agent
genaug skills apply ./skills/schedule-meeting/SKILL.md --project my-agent
genaug skills view "Schedule Meeting" --project my-agent
```

Keep skill files concise, operational, and specific. Do not put secrets or raw customer
regulated data in skills.

## Memory

Use memory for durable facts that improve product behavior. Keep sensitive data out
unless the launch scope explicitly permits it.

```bash
genaug memory store --project my-agent --user app-user-123 \
  --fact "User prefers concise updates" \
  --fact-type preference
genaug memory search --project my-agent --user app-user-123 --query "updates"
genaug memory profile --project my-agent --user app-user-123
genaug memory purge-user --project my-agent --user app-user-123 --yes
```

## Identity And Channels

Map every external identity back to a stable app user ID.

```bash
genaug identity list --project my-agent
genaug identity link-user --project my-agent \
  --external-user-id app-user-123 \
  --provider telegram \
  --provider-user-id telegram-user-123
genaug channels status --project my-agent
genaug channels test telegram --project my-agent
```

## Observability And Support

When debugging, keep a redacted support receipt instead of screenshots or raw logs.

```bash
genaug smoke --project my-agent --evidence-output .genaug/smoke-evidence.json --json
genaug smoke --project my-agent --include-support-bundle --evidence-output artifacts/smoke-evidence.json --json
genaug logs --project my-agent --follow
genaug observability trace <trace-id> --project my-agent --json
genaug observability support-bundle --project my-agent --json
```

The useful proof fields are response ID, request ID, trace ID, model/provider metadata,
usage metadata, stable reason codes, and the exact command that failed.

## Billing And Usage

General Augment records usage events and enforces finite plan limits. Provider/API bills
normally stay with the tenant's provider account during the first launch phase.

```bash
genaug projects usage --project my-agent --json
genaug billing checkout --project my-agent --tier pro
genaug billing portal --project my-agent
genaug billing events --project my-agent --json
```

Do not treat usage events or Stripe meter exports as an invoice by themselves unless a
commercial agreement says so.

## Local Mock And Contract Tests

Use the CLI mock for app CI before pointing tests at the hosted API.

```bash
genaug mock --host 127.0.0.1 --port 8787 --quiet
export GENAUG_API_BASE_URL=http://127.0.0.1:8787
export GENAUG_API_KEY=local-test
python examples/python-response.py
node examples/typescript-response.mjs
```

## Launch Evidence

A real launch proof should include:

- public package install proof;
- dashboard project setup proof;
- model-provider health evidence when using tenant-owned capacity;
- `/v1/responses` smoke with response ID and trace ID;
- smoke evidence JSON with dashboard observability links;
- memory behavior proof if memory is in scope;
- tool-call audit and approval proof if tools are in scope;
- support bundle or trace evidence;
- known limits and regulated-data scope explicitly accepted.
