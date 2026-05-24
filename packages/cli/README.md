# General Augment CLI

General Augment is the agent backend for your app. This is the standalone developer
CLI for creating, validating, deploying, and verifying General Augment projects.

For source-checkout development, use the repo-local command prefix:

```bash
uv run --project packages/cli genaug --version
uv run --project packages/cli genaug --help
```

This source checkout targets `0.1.1` of `general-augment-cli`. For application
integrations, install the published package once the registry shows the expected
version, then use the `genaug` entrypoint:

```bash
pip install general-augment-cli
genaug --version
genaug setup --capability code --capability browse --json
genaug setup --bootstrap --project-name "Petstore Agent" --project-slug petstore-agent --print-env
genaug migrate openai-responses --dry-run --json
genaug auth login
genaug doctor
genaug projects list
genaug init
genaug init dayplan-agent --tool web_search
genaug integrate https://petstore3.swagger.io/api/v3/openapi.json
genaug validate ./petstore-agent/genaug-agent.yaml
genaug deploy ./petstore-agent/genaug-agent.yaml
genaug keys create --project petstore-agent --name "Production backend"
genaug providers setup --capability browse --project petstore-agent --api-key-env BROWSERBASE_API_KEY --health-check
genaug connectors setup
genaug skills design --job-type website-builder --project petstore-agent --apply
genaug mock --host 127.0.0.1 --port 8787 --quiet
genaug smoke --idempotency-key smoke-replay-1 --metadata feature=spark
genaug smoke --project petstore-agent --evidence-output .genaug/smoke-evidence.json
genaug verify --project petstore-agent
genaug onboarding verify --project petstore-agent --json
genaug dashboard open --project petstore-agent
```

Until PyPI `0.1.1` is published, keep using `uv run --project packages/cli genaug ...`
from the General Augment repository and record package-index access as the environment
issue.

`genaug setup` is the install/configure path. It inspects the current app, detects
frameworks, env files, OpenAI Responses call sites, prompts, tools, and webhooks, then
writes a redacted `.genaug/setup-plan.json` without changing code or storing raw
secrets. Add `--bootstrap` after `genaug auth login` to create or select a project and
mint a project runtime key through installer auth; the setup artifact stores only the
masked key and updates `active_project`, not the raw runtime secret. Add `--print-env`
when you want the runtime env block shown once for your backend secret manager.
`genaug migrate openai-responses` does the same inspection and generates a patch for
OpenAI-compatible clients; it only edits files with `--apply` plus explicit
confirmation or `--yes`.

`genaug auth login` starts browser installer auth by default. The browser consent flow
opens the dashboard `/cli/authorize` approval page, creates an installer session for
setup tasks, and keeps that session separate from runtime `/v1/responses` keys. After
approval, paste the short-lived code back into the terminal. `genaug auth login
--api-key ...` remains available for operator/admin workflows and verifies the key
against `/api/v1/admin/me` before writing local config. The CLI stores auth at
`~/.genaug/config.yaml` by default with owner-only file permissions. Set
`GENAUG_CLI_CONFIG` to use a custom path.

Preferred environment overrides:

```bash
export GENAUG_ADMIN_API_KEY=gaadmlive...
export GENAUG_ADMIN_BASE_URL=https://api.generalaugment.com
```

`GENAUG_API_KEY` and `GENAUG_API_BASE_URL` are also accepted, which keeps local
mock and SDK test scripts easy to share.

The generated manifest is `genaug-agent.yaml`. Use `genaug init <name>` when you want
a starter agent before an OpenAPI spec exists. Use
`genaug integrate <openapi-spec> --auto-deploy` when you want the CLI to create or
update the project and register the generated OpenAPI tools in one pass. Without
`--auto-deploy`, review the scaffold first, then run
`genaug validate ./<agent>/genaug-agent.yaml` and
`genaug deploy ./<agent>/genaug-agent.yaml`. `deploy` runs the same local validation
before calling the hosted API. Both scaffolds include
`CODING_AGENT_PROMPT.md`, which is the paste-ready backend handoff for a coding agent.

## Common workflows

- Auth: `genaug auth login`, `genaug auth whoami`, `genaug auth logout`
- Self-serve setup: `genaug setup --capability code --capability browse --json`,
  `genaug setup --bootstrap --project-name "Demo Agent" --project-slug demo-agent`
- App migration: `genaug migrate openai-responses --dry-run`,
  `genaug migrate openai-responses --apply --yes`
- Starter scaffold: `genaug init <name> --tool web_search`
- Existing-app init: `genaug init --capability browse --json`
- Local config validation: `genaug validate ./genaug-agent.yaml --json`
- Projects: `genaug projects list`, `genaug projects create`, `genaug projects usage`,
  `genaug projects runtime-policy`, `genaug projects export`, `genaug projects archive`
- API keys: `genaug keys create`, `genaug keys list`, `genaug keys update`,
  `genaug keys revoke`
- Providers and connectors: `genaug providers setup --api-key-env <ENV> --health-check`,
  `genaug connectors setup --name <name> --url <mcp-url> --health-check`,
  `genaug model-providers set`, `genaug model-providers health`
- Skills, tools, and channels: `genaug skills design`, `genaug skills list`,
  `genaug skills design --apply`, `genaug skills view`, `genaug skills apply`,
  `genaug skills delete`,
  `genaug tools list`, `genaug tools toggle`,
  `genaug tools discovery`, `genaug channels status`, `genaug channels connect`,
  `genaug channels test`, `genaug channels disconnect`. Telegram supports connect,
  test, and disconnect; WhatsApp/SMS support sender configuration and clearing.
  Tenant-owned local connectors, such as a Mac-backed iMessage connector or private VM
  connector, are configured through `connectors.local` in `genaug-agent.yaml` and local
  connector scripts rather than `genaug channels connect`.
  For iMessage, use the npm helper on the Mac:
  `npx @general-augment/local-imessage setup --project dayplan-agent --write-prompt --write-config`.
- MCP servers: `genaug mcp list`, `genaug mcp add`, `genaug mcp test`,
  `genaug mcp delete`. Use exactly one transport per server: `--url` for HTTP
  endpoints or `--command` for stdio servers.
  `genaug connectors setup` can write MCP connector config through installer auth when
  `--name` plus `--url` or `--command` is supplied, and it rejects raw API keys in MCP
  URLs.
- Model providers: `genaug model-providers list`, `genaug model-providers set`,
  `genaug model-providers health`, `genaug model-providers revoke`
- Billing: `genaug billing status`, `genaug billing top-up`,
  `genaug billing usage`, `genaug billing verify`, `genaug billing checkout`,
  `genaug billing portal`, `genaug billing events`
- Memory: `genaug memory store`, `genaug memory search`, `genaug memory profile`,
  `genaug memory delete`, `genaug memory purge-user`
- Users and identity: `genaug users list`, `genaug users detail`,
  `genaug users delete`, `genaug identity list`, `genaug identity create-test`,
  `genaug identity link-user`, `genaug identity verification-code`,
  `genaug identity magic-link`, `genaug identity verify`,
  `genaug identity resolve`, `genaug identity unlink`
- Observability: `genaug observability trace`, `genaug observability support-bundle`
- Approvals: `genaug approvals list`, `genaug approvals approve`, `genaug approvals deny`
- Scheduled jobs: `genaug jobs create/list/detail/runs/run/pause/resume/delete`
- Operations: `genaug doctor`, `genaug status`, `genaug logs`
- App smoke checks: `genaug smoke --message "Reply exactly with: ok"`,
  `genaug smoke --structured`, `genaug smoke --evidence-output .genaug/smoke-evidence.json`,
  `genaug smoke --json`
- Dashboard review: `genaug dashboard open --project <project-slug>`
- Project acceptance checks: `genaug verify --project <project-slug>`, which checks
  project keys, hosted agent test, tools, logs, usage, usage limits, observability,
  runtime policy model routing, memory lifecycle, and tool-call audit before printing
  dashboard URLs for the same tenant.
- One-command onboarding gate: `genaug onboarding verify --project <project-slug> --json`,
  which wraps the same project checks with CLI/API version metadata and a coding-agent
  friendly ready/blocked payload.
- Local development: `genaug dev ./genaug-agent.yaml --message "Hello"`
- Local mock testing: `genaug mock --host 127.0.0.1 --port 8787 --quiet`

## Billing

```bash
genaug billing status --project dayplan-agent
genaug billing top-up --project dayplan-agent --amount-usd 25.00
genaug billing usage --project dayplan-agent --json
genaug billing verify --project dayplan-agent --json
genaug billing checkout --project dayplan-agent --tier pro
genaug billing portal --project dayplan-agent
genaug billing events --project dayplan-agent --json
```

Use these commands for hosted billing actions when Stripe is configured for the
project. `status` shows active credit balance, funding mode, and auto top-up state.
`top-up` returns a hosted Stripe Checkout URL for paid usage credits. `usage` returns
billing-relevant usage rollups for reconciliation. `verify` checks the project credit
gate, funding mode, credit ledger reservation linkage, and usage rollup visibility so
operators can catch unmetered platform-funded inference before launch. `checkout`
returns a hosted Stripe Checkout URL for Build, Pro, or Team, `portal` returns a hosted
Stripe Customer Portal URL for linked customers, and `events` lists stored Stripe
webhook events such as checkout completion, invoice payment, and payment failure. These
commands do not create Stripe products, prices, or webhooks directly; those stay in the
server-side billing setup and readiness flow.

## Scheduled Jobs

```bash
genaug jobs create \
  --project dayplan-agent \
  --target-app-user-id app-user-123 \
  --prompt "Review this account and summarize the next action." \
  --interval-seconds 3600 \
  --json
genaug jobs list --project dayplan-agent --json
genaug jobs runs "$GENAUG_SCHEDULED_JOB_ID" --project dayplan-agent --json
genaug jobs run "$GENAUG_SCHEDULED_JOB_ID" --project dayplan-agent --record-only --json
genaug jobs pause "$GENAUG_SCHEDULED_JOB_ID" --project dayplan-agent --json
genaug jobs resume "$GENAUG_SCHEDULED_JOB_ID" --project dayplan-agent --json
genaug jobs delete "$GENAUG_SCHEDULED_JOB_ID" --project dayplan-agent --yes --json
```

The commands use the authenticated admin API and return machine-readable JSON with
next/last run timestamps, retry history, terminal reason, target user/channel, latest
trace ID, and linked durable run IDs. Delete is a soft cancel so run history remains
available for support.

For a full CLI-to-dashboard onboarding proof from this repository, run:

```bash
make app-developer-onboarding-smoke
```

That harness creates a fresh dummy tenant from a richer OpenAPI fixture, proves
generated tool governance, deploys SOUL.md and skills, sends real `/v1/responses`
smokes for multiple app users, runs `genaug verify --json`, starts an owned dashboard
dev server, and writes JSON evidence plus screenshots for the project overview, skills,
tools, integrate, and analytics pages. Add `GENAUG_DASHBOARD_SMOKE_ARCHIVE_PROJECT=1`
when CI should archive the created smoke project after the artifact is captured.

`genaug smoke` checks `/health/ready` and sends one project-keyed `/v1/responses`
request using bearer auth. Use `--idempotency-key`, `--request-id`, `--traceparent`,
and repeated `--metadata key=value` flags when you need replayable support/debug
evidence from hosted API or the local mock. Use `--project <project-slug>` when the
configured key is a management key and the app-facing request needs `X-Project-ID`.
Use `--structured` to request the default `json_schema` smoke response, or
`--schema-file ./schema.json` to verify an app-specific structured-output contract.
Use `--evidence-output .genaug/smoke-evidence.json` to persist a redacted launch
artifact with the readiness result, response id, trace id, dashboard observability URL,
and secret-safety metadata. Add `--include-support-bundle` with `--project` when the
configured key can call admin APIs and you want the bounded support-bundle payload
embedded in that evidence file.
With `--json`, smoke output includes the `/health/ready` payload, the full
`/v1/responses` object, `response_id`, `request_id`, `trace_id`, dashboard URLs, and
the evidence object so automation can prove health and tracing from one command.

When the API returns a rate-limit `429`, the CLI prints the stable reason and
`Retry-After` timing when the platform includes them.

`genaug doctor` checks the resolved config path, base URL, API key presence,
`/health/ready`, and `/api/v1/admin/me` without printing secret values. Run it before
`integrate` when a new developer is unsure whether local auth or network setup is the
problem.

`genaug mock` runs the deterministic local HTTP mock. Use it for offline app contract tests against
`/v1/responses`, memory routes, project setup, OpenAPI tool registration, key
management, logs, usage, observability, health checks, idempotency replays, trace
metadata, structured-output fixtures, and semantic SSE fixtures.

The console commands are defined in `pyproject.toml`:

```toml
[project.scripts]
genaug = "platform_cli.main:app"
```
