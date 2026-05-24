# Quickstart

This quickstart is for app developers integrating General Augment from a trusted
backend.

Agent-readable docs are available at `https://docs.generalaugment.com/llms.txt`.
For full implementation context, use `https://docs.generalaugment.com/llms-full.txt`
or the page-level Markdown exports under `https://docs.generalaugment.com/markdown/`.

## 1. Choose Setup Or Migration

Use setup mode when you want General Augment configured before touching app code:

```bash
pip install general-augment-cli
genaug auth login
genaug setup --bootstrap --project-name "My Agent" --project-slug my-agent --print-env
```

Use migration mode when an existing app already calls OpenAI Responses and you want a
coding agent to patch the app safely:

```bash
genaug init --json
genaug migrate openai-responses --dry-run --json
genaug migrate openai-responses --apply --yes
```

Both paths are idempotent and inspectable. They write setup artifacts under `.genaug/`,
show diffs before code changes, and do not store raw provider credentials in the repo.

## 2. Configure Runtime Secrets

Store the runtime values printed by setup in your backend secret manager or local
server environment. Keep project API keys server-side.

```bash
export GENAUG_API_KEY="ga_project_..."
export GENAUG_API_BASE_URL="https://api.generalaugment.com"
export GENAUG_PROJECT_ID="project_..."
export GENAUG_OPENAI_BASE_URL="https://api.generalaugment.com/v1"
```

## 3. Configure Providers, Connectors, And Skills

```bash
genaug providers setup --capability browse --project my-agent --api-key-env BROWSERBASE_API_KEY --health-check
genaug connectors setup --name browserbase \
  --url 'https://mcp.browserbase.com/mcp?api_key=${{ providers.browserbase.api_key }}' \
  --health-check
genaug skills design --job-type website-builder --project my-agent --apply
```

Provider keys go into General Augment credential custody. Do not paste raw keys into
MCP URLs, prompts, docs, screenshots, or support artifacts.

## 4. Verify The API

```bash
genaug smoke --project my-agent \
  --message "Reply exactly with: ok" \
  --evidence-output .genaug/smoke-evidence.json \
  --json
```

Keep the JSON output when asking for support. It includes request, response, trace, and
ready-status evidence without printing secrets. The evidence file adds dashboard
observability links and redacted support metadata.

## 5. Call From Your Backend

Use the Python or TypeScript SDK from server code. Do not call General Augment directly
from browser or mobile clients with project keys.

```bash
pip install general-augment-sdk
npm install @general-augment/sdk
```

Every production request should include:

- a stable app user ID in `user`;
- an idempotency key when the turn can be retried;
- app metadata useful for support and analytics;
- request/trace IDs in app logs.

## 6. Bring Your Own Provider Capacity

Production tenants should add their own model-provider keys in the dashboard or through
the CLI. General Augment governs routing, traces, usage, memory, tools, and approvals;
the tenant pays provider/API bills directly unless a separate commercial agreement says
otherwise.

```bash
genaug model-providers set openai \
  --project my-agent \
  --api-key "$OPENAI_API_KEY"

genaug model-providers health openai --project my-agent --json
```

Do not write provider keys to repo files, prompts, docs, screenshots, or artifacts.

## 7. Prove The Integration

```bash
genaug verify --project my-agent --json
genaug onboarding verify --project my-agent --json
```

Return a clear `ready` or `blocked` verdict with exact failing command output when a
coding agent implements the integration.

Next, use the [platform guide](platform-guide.md) for tools, memory, model providers,
channels, approvals, observability, billing, and launch evidence.
