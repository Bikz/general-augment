# Quickstart

This quickstart is for app developers integrating General Augment from a trusted
backend.

Agent-readable docs are available at `https://docs.generalaugment.com/llms.txt`.
For full implementation context, use `https://docs.generalaugment.com/llms-full.txt`
or the page-level Markdown exports under `https://docs.generalaugment.com/markdown/`.

## 1. Create A Project

Sign in at https://app.generalaugment.com, create a project, and copy a project API key.
Store the key in your server environment.

```bash
export GENAUG_API_KEY="ga_project_..."
export GENAUG_API_BASE_URL="https://api.generalaugment.com"
```

## 2. Verify The API

```bash
pip install general-augment-cli
genaug smoke --message "Reply exactly with: ok" --json
```

Keep the JSON output when asking for support. It includes request, response, trace, and
ready-status evidence without printing secrets.

## 3. Call From Your Backend

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

## 4. Bring Your Own Provider Capacity

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

## 5. Prove The Integration

```bash
genaug verify --project my-agent --json
genaug onboarding verify --project my-agent --json
```

Return a clear `ready` or `blocked` verdict with exact failing command output when a
coding agent implements the integration.

Next, use the [platform guide](platform-guide.md) for tools, memory, model providers,
channels, approvals, observability, billing, and launch evidence.
