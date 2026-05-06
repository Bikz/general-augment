# General Augment CLI

Standalone developer CLI for General Augment.

Install the public package and use the `genaug` entrypoint:

```bash
pip install general-augment-cli
genaug --version
genaug auth login --api-key ga_admin_redacted
genaug doctor
genaug projects list
genaug init demo-agent --tool web_search
genaug integrate https://petstore3.swagger.io/api/v3/openapi.json \
  --name demo-agent \
  --output-dir ./demo-agent
genaug validate ./demo-agent/genaug-agent.yaml
genaug deploy ./demo-agent/genaug-agent.yaml
genaug keys create --project demo-agent --name "Production backend"
genaug mock --host 127.0.0.1 --port 8787 --quiet
genaug smoke --idempotency-key smoke-replay-1 --metadata feature=demo
genaug smoke --project demo-agent
genaug verify --project demo-agent
genaug onboarding verify --project demo-agent --json
```

`genaug auth login` verifies the key against `/api/v1/admin/me` before writing local
config. The CLI stores auth at `~/.genaug/config.yaml` by default with owner-only file
permissions. Set
`GENAUG_CLI_CONFIG` to use a custom path.

Preferred environment overrides:

```bash
export GENAUG_ADMIN_API_KEY=ga_admin_redacted
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

Current public docs for agents are available at
`https://docs.generalaugment.com/llms.txt`. Use
`https://docs.generalaugment.com/llms-full.txt` or the `/markdown/...` page exports
when a coding agent needs implementation context without navigating the HTML site.

## Common workflows

- Auth: `genaug auth login`, `genaug auth whoami`, `genaug auth logout`
- Starter scaffold: `genaug init <name> --tool web_search`
- Local config validation: `genaug validate ./genaug-agent.yaml --json`
- Projects: `genaug projects list`, `genaug projects create`, `genaug projects usage`,
  `genaug projects runtime-policy`, `genaug projects export`, `genaug projects archive`
- API keys: `genaug keys create`, `genaug keys list`, `genaug keys update`,
  `genaug keys revoke`
- Skills, tools, and channels: `genaug skills list`, `genaug skills view`,
  `genaug skills apply`, `genaug skills delete`, `genaug tools list`, `genaug tools toggle`,
  `genaug tools discovery`, `genaug channels status`, `genaug channels connect`,
  `genaug channels test`, `genaug channels disconnect`. Telegram supports connect,
  test, and disconnect; WhatsApp/SMS support sender configuration and clearing.
- MCP servers: `genaug mcp list`, `genaug mcp add`, `genaug mcp test`,
  `genaug mcp delete`. Use exactly one transport per server: `--url` for HTTP
  endpoints or `--command` for stdio servers.
- Model providers: `genaug model-providers list`, `genaug model-providers set`,
  `genaug model-providers health`, `genaug model-providers revoke`
- Billing: `genaug billing checkout`, `genaug billing portal`,
  `genaug billing events`
- Memory: `genaug memory store`, `genaug memory search`, `genaug memory profile`,
  `genaug memory delete`, `genaug memory purge-user`
- Users and identity: `genaug users list`, `genaug users detail`,
  `genaug users delete`, `genaug identity list`, `genaug identity create-test`,
  `genaug identity link-user`, `genaug identity verification-code`,
  `genaug identity magic-link`, `genaug identity verify`,
  `genaug identity resolve`, `genaug identity unlink`
- Observability: `genaug observability trace`, `genaug observability support-bundle`
- Approvals: `genaug approvals list`, `genaug approvals approve`, `genaug approvals deny`
- Operations: `genaug doctor`, `genaug status`, `genaug logs`
- App smoke checks: `genaug smoke --message "Reply exactly with: ok"`,
  `genaug smoke --structured`, `genaug smoke --json`
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
genaug billing checkout --project demo-agent --tier pro
genaug billing portal --project demo-agent
genaug billing events --project demo-agent --json
```

Use these commands for hosted billing actions when Stripe is configured for the
project. `checkout` returns a hosted Stripe Checkout URL for Pro or Team, `portal`
returns a hosted Stripe Customer Portal URL for linked customers, and `events` lists
stored Stripe webhook events such as checkout completion, invoice payment, and payment
failure. These commands do not create Stripe products, prices, or webhooks directly;
those stay in the server-side billing setup and readiness flow.

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
With `--json`, smoke output includes the `/health/ready` payload, the full
`/v1/responses` object, `response_id`, `request_id`, and `trace_id` so automation can
prove health and tracing from one command.

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
