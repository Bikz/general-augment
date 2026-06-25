# Platform Guide

General Augment is the hosted agent backend for an app. The app keeps its product
database, user authentication, business rules, and UI. General Augment provides the
governed agent layer around those app systems.

## Platform Surfaces

| Surface | What It Is For | Human Path | Agent/CLI Path |
| --- | --- | --- | --- |
| Dashboard | Project setup, API keys, providers, tools, usage, traces | https://app.generalaugment.com | `genaug dashboard open` |
| Self-serve setup | Install/configure General Augment without code edits | CLI browser consent | `genaug auth login`, `genaug setup --bootstrap` |
| Safe migration | Patch an existing OpenAI Responses app after diff review | PR or local diff review | `genaug migrate openai-responses` |
| Responses API | App backend agent turns | App backend code | Python SDK, TypeScript SDK, raw HTTP |
| Capability providers | Tenant-owned coding, browser, search, video, and model/API capacity | Project provider panels | `genaug providers setup`, `genaug providers smoke` |
| Tools | Governed app/API actions and BYO local connector capabilities | Project tools pages | `genaug integrate`, `genaug tools`, `genaug connectors setup` |
| Skills | Durable tenant behavior guidance | Project skills pages | `genaug skills design`, `genaug skills apply` |
| Memory | User-scoped durable facts | User/memory views | SDK memory methods (`store_memory`, `search_memory`, ...) |
| Identity | Map app users to General Augment users and channels | Identity views | SDK identity methods (`link_user`, `resolve_user`, `unlink_user`) |
| Verification | Project acceptance proof and launch evidence | Dashboard review | `genaug smoke`, `genaug verify` |

## Recommended Integration Order

1. Run `genaug auth login`.
2. Run `genaug setup --bootstrap` to create or select the project and print runtime env.
3. For existing OpenAI Responses apps, run `genaug migrate openai-responses --dry-run`
   and apply only after reviewing the diff.
4. Store the project runtime key in the app backend.
5. Run `genaug smoke --evidence-output .genaug/smoke-evidence.json --json` against `/v1/responses`.
6. Add tenant-owned provider credentials when production traffic should use the
   tenant's own provider account.
7. Add generated OpenAPI tools, MCP servers, or BYO local connectors for private
   capacity.
8. Add skills and SOUL/personality guidance.
9. Add memory only for durable user facts the app is allowed to retain.
10. Add identity linking from backend SDK code if users will interact outside the app UI.
11. Verify traces, usage, and tool behavior.
12. Run `genaug verify --json` and `genaug dashboard open --project <project>`.

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
genaug providers setup --provider codex-mcp --project my-agent --api-key-env OPENAI_API_KEY --health-check
genaug providers smoke --capability code --capability browse --json
genaug providers readiness --project my-agent --json
```

`providers setup` reads the named env var once, stores the credential in General Augment
custody, runs a health check, and writes only redacted setup evidence. `providers smoke`
plans launch evidence per capability or provider; `providers readiness` reports the
provider readiness rows for the project.

## Tools, MCP, And Connectors

Use OpenAPI specs for app-owned APIs and MCP for external tool surfaces when the
credential and audit boundary is acceptable.

Use BYO local connectors for tenant-owned private capacity such as a Mac, VM, coding
sandbox, desktop automation host, or private network service. The connector keeps local
credentials and adapter internals private; General Augment exposes only governed tool
schemas to the runtime and handles approval, audit, redaction, policy, and rate limits.
iMessage should use this pattern through a tenant-owned Mac rather than raw shell or
direct adapter access.

```bash
genaug integrate ./openapi.yaml --auto-deploy
genaug connectors setup --name browserbase \
  --url 'https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}' \
  --health-check
genaug tools list --project my-agent
genaug tools toggle delete_account --project my-agent --disable
genaug tools discovery --project my-agent --json
genaug tools add-mcp github --project my-agent --url https://example.com/mcp
```

Use exactly one transport per MCP server: `--url` for HTTP endpoints or `--command`
for stdio servers. For destructive, expensive, or user-visible actions, keep the tool
disabled by default (`genaug tools toggle <tool> --disable`) until the app owner accepts
the risk and an approval policy is in place.

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
unless the launch scope explicitly permits it. Memory is called from backend code
through the SDK, not a CLI command.

```python
client.store_memory(
    {
        "user_id": "app-user-123",
        "fact": "User prefers concise updates",
        "fact_type": "preference",
    }
)
client.search_memory({"user_id": "app-user-123", "query": "updates"})
client.memory_profile("app-user-123")
client.purge_user_memory("app-user-123")
```

## Identity

Map every external identity back to a stable app user ID. Identity linking is called
from backend code through the SDK.

```python
client.link_user(
    "project_123",
    phone="+15555550123",
    app_user_id="app-user-123",
    provider_name="app",
)
client.resolve_user("project_123", "+15555550123")
client.unlink_user("project_123", "+15555550123")
```

## Observability And Support

When debugging, keep a redacted support receipt instead of screenshots or raw logs.
The CLI captures this evidence through `smoke`:

```bash
genaug smoke --project my-agent --evidence-output .genaug/smoke-evidence.json --json
genaug smoke --project my-agent --include-support-bundle \
  --evidence-output artifacts/smoke-evidence.json --json
```

The useful proof fields are response ID, request ID, trace ID, model/provider metadata,
usage metadata, stable reason codes, and the exact command that failed. The evidence
file also includes the dashboard observability URL for the same trace.

## Local Mock And Contract Tests

Use the CLI mock for app CI before pointing tests at the hosted API. It ships with the
CLI package and is launched as a Python module (not a `genaug` subcommand):

```bash
uv run --project packages/cli python -m platform_cli.local_mock \
  --host 127.0.0.1 --port 8787 --quiet
export GENAUG_API_BASE_URL=http://127.0.0.1:8787
export GENAUG_API_KEY=local-test
python examples/python-response.py
node examples/typescript-response.mjs
```

## Launch Evidence

A real launch proof should include:

- public package install proof;
- dashboard project setup proof;
- provider health evidence when using tenant-owned capacity;
- `/v1/responses` smoke with response ID and trace ID;
- smoke evidence JSON with dashboard observability links;
- memory behavior proof if memory is in scope;
- tool-call audit and approval proof if tools are in scope;
- support bundle or trace evidence;
- known limits and regulated-data scope explicitly accepted.
