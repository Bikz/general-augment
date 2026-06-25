# General Augment CLI

General Augment is the agent backend for your app. This is the standalone developer
CLI for creating, validating, deploying, and verifying General Augment projects, and
for wiring up auth, providers, tools, and skills.

```bash
pip install general-augment-cli
genaug --version
genaug --help
```

For source-checkout development, use the repo-local command prefix:

```bash
uv run --project packages/cli genaug --version
uv run --project packages/cli genaug --help
```

The version reported by `genaug --version` is single-sourced from the installed package
metadata, so it always matches the published distribution.

## Command surface

The published CLI is intentionally minimal — the setup, integration, and verification
commands an app developer needs, and nothing else:

| Command group | Commands |
| --- | --- |
| `genaug auth` | `login`, `logout`, `whoami` |
| `genaug keys` | `list`, `create`, `update`, `revoke` |
| `genaug tools` | `list`, `toggle`, `catalog`, `discovery`, `explain-turn`, `add-mcp` |
| `genaug skills` | `design`, `list`, `view`, `apply`, `delete` |
| `genaug providers` | `setup`, `smoke`, `readiness` |
| `genaug connectors` | `setup` |
| `genaug migrate` | `openai-responses` |
| `genaug dashboard` | `open` |
| top-level | `integrate`, `init`, `setup`, `doctor`, `status`, `smoke`, `validate`, `verify` |

## Quick start

For an existing app, the friendliest first command is the guided wizard. It inspects
the current app, detects frameworks, env files, OpenAI Responses call sites, prompts,
tools, and webhooks, then writes a redacted `.genaug/setup-plan.json` without changing
code or storing raw secrets.

```bash
genaug auth login
genaug init --interactive
```

`genaug init --interactive` runs the guided setup wizard without scaffolding a starter
project. `genaug setup` is the explicit install/configure path with the same options.

```bash
genaug setup --capability code --capability browse --json
genaug setup --interactive
genaug setup --answers-template ./genaug-answers.template.json --json
genaug setup --guided --answers-file ./genaug-answers.json --json
genaug setup --guided --answers-file ./genaug-answers.json --project petstore-agent --configure-providers --json
genaug setup --bootstrap --project-name "Petstore Agent" --project-slug petstore-agent --print-env
genaug init --interactive --login --bootstrap --project-name "Petstore Agent" --project-slug petstore-agent --configure-providers --print-env
genaug setup --interactive --handoff-output .genaug/setup-handoff.md
```

Add `--bootstrap` after `genaug auth login` to create or select a project and mint a
project runtime key through installer auth; the setup artifact stores only the masked
key. Add `--login` to start browser installer auth inline before bootstrapping. Add
`--print-env` to show the runtime env block once for your backend secret manager. Use
`--answers-template ... --json` to generate the secret-free questionnaire before a human
or coding agent fills it, then pass the completed file with `--answers-file` for
non-interactive runs. `--guided --json` requires `--answers-file` so interactive prompts
do not contaminate machine-readable output. Human guided setup writes
`.genaug/setup-handoff.md` by default; use `--handoff-output` for a different path or
`--no-handoff` to skip it.

## Auth

```bash
genaug auth login
genaug auth whoami --json
genaug auth logout
```

`genaug auth login` starts browser installer auth by default: it opens the dashboard
`/cli/authorize` approval page, creates an installer session for setup tasks, and keeps
that session separate from runtime `/v1/responses` keys. After approval the dashboard
redirects to the CLI's loopback callback and the CLI exchanges the short-lived code
automatically; if the callback cannot start or times out, it falls back to a paste-code
prompt. `genaug auth login --api-key ...` remains available for operator/admin
workflows and verifies the key against `/api/v1/admin/me` before writing local config.
The CLI stores auth at `~/.genaug/config.yaml` with owner-only file permissions; set
`GENAUG_CLI_CONFIG` to use a custom path.

Environment overrides:

```bash
export GENAUG_ADMIN_API_KEY=gaadmlive...
export GENAUG_ADMIN_BASE_URL=https://api.generalaugment.com
```

`GENAUG_API_KEY` and `GENAUG_API_BASE_URL` are also accepted, which keeps local mock
and SDK test scripts easy to share.

## Migrating an existing app

```bash
genaug migrate openai-responses --dry-run --json
genaug migrate openai-responses --apply --yes
genaug migrate openai-responses --apply --yes --branch genaug/openai-responses --push --create-pr
```

`genaug migrate openai-responses` inspects the app and generates a patch for
OpenAI-compatible clients. It only edits files with `--apply` plus explicit confirmation
or `--yes`. For coding-agent migration work, add `--branch`, `--push`, and `--create-pr`
to create the branch, commit the safe edits, push, and open a GitHub pull request through
the `gh` CLI.

## Building a project from an OpenAPI spec

```bash
genaug integrate https://petstore3.swagger.io/api/v3/openapi.json
genaug integrate https://petstore3.swagger.io/api/v3/openapi.json --auto-deploy
genaug validate ./petstore-agent/genaug-agent.yaml
```

Use `genaug init <name>` for a starter agent before an OpenAPI spec exists, or
`genaug integrate <openapi-spec> --auto-deploy` to create/update the project and register
the generated OpenAPI tools in one pass. Deployment is performed by `integrate
--auto-deploy` (there is no separate `genaug deploy` command). Without `--auto-deploy`,
review the scaffold, then `genaug validate ./<agent>/genaug-agent.yaml`. The generated
manifest is `genaug-agent.yaml`; both scaffolds include `CODING_AGENT_PROMPT.md`, the
paste-ready backend handoff for a coding agent.

```bash
genaug init
genaug init dayplan-agent --tool web_search
genaug init --interactive
```

## API keys

```bash
genaug keys create --project petstore-agent --name "Production backend"
genaug keys list --project petstore-agent --json
genaug keys update <key-id> --project petstore-agent --name "Renamed"
genaug keys revoke <key-id> --project petstore-agent
```

## Providers and connectors

```bash
genaug providers setup --capability browse --project petstore-agent --api-key-env BROWSERBASE_API_KEY --health-check
genaug providers setup --capability video --json
genaug providers setup --provider codex-mcp --project petstore-agent --api-key-env OPENAI_API_KEY --health-check
genaug providers setup --provider fal --project petstore-agent --api-key-env FAL_API_KEY --health-check
genaug providers smoke --capability code --capability browse --capability video --json
genaug providers smoke --provider browserbase --project petstore-agent --api-key-env BROWSERBASE_API_KEY --json
genaug providers readiness --project petstore-agent --json
genaug connectors setup --name my-mcp --url https://example.com/mcp --health-check
```

`providers setup` reads the named env var once, stores the credential in General Augment
custody, runs a health check, and writes only redacted provider setup evidence — raw
secrets are never written to the setup artifact. `providers smoke` plans launch evidence
per capability; add `--evidence-output .genaug/provider-smoke-evidence.json` to persist
the redacted launch-smoke payload for launch reviews. `providers readiness` reports the
provider readiness rows for a project. `connectors setup` writes MCP connector config
through installer auth when `--name` plus `--url` or `--command` is supplied, and rejects
raw API keys in MCP URLs.

For iMessage, use the npm helper on the Mac:

```bash
npx @general-augment/local-imessage setup --project dayplan-agent --write-prompt --write-config
```

## Skills and tools

```bash
genaug skills design --job-type website-builder --project petstore-agent --apply
genaug skills list --project petstore-agent
genaug skills view <skill-name> --project petstore-agent
genaug skills apply ./SKILL.md --project petstore-agent
genaug skills delete <skill-name> --project petstore-agent

genaug tools catalog --project petstore-agent
genaug tools list --project petstore-agent --json
genaug tools toggle web_search --project petstore-agent --enable
genaug tools discovery --project petstore-agent
genaug tools explain-turn --project petstore-agent --requested-tool web_search --json
genaug tools add-mcp my-mcp --project petstore-agent --url https://example.com/mcp
```

Use exactly one transport per MCP server: `--url` for HTTP endpoints or `--command`
for stdio servers.

## Doctor, status, and smoke

```bash
genaug doctor
genaug doctor --project petstore-agent --user user_123 --json
genaug status --json
genaug smoke --idempotency-key smoke-replay-1 --metadata feature=spark
genaug smoke --project petstore-agent --structured --json
genaug smoke --project petstore-agent --memory-recall --import-evidence \
  --evidence-output .genaug/smoke-evidence.json --json
```

`genaug doctor` checks the resolved config path, base URL, API key presence,
`/health/ready`, and `/api/v1/admin/me` without printing secret values. Add `--project`
to verify read-only agent-cloud readiness (runtime policy, tool catalog, approvals, run
timelines) and `--user` to include a tenant memory profile reachability check.

`genaug smoke` checks `/health/ready` and sends one project-keyed `/v1/responses`
request using bearer auth. Use `--idempotency-key`, `--request-id`, `--traceparent`, and
repeated `--metadata key=value` flags for replayable support/debug evidence. Use
`--project <slug>` when the configured key is a management key and the app-facing request
needs `X-Project-ID`. Use `--structured` for the default `json_schema` smoke response or
`--schema-file ./schema.json` for an app-specific contract. Use `--evidence-output` to
persist a redacted launch artifact (readiness result, response id, trace id, dashboard
observability URL, secret-safety metadata). Add `--memory-recall` to prove `/v1/responses`
recalls a seeded fact without prompt echo.

## Verify

```bash
genaug verify --project petstore-agent
```

`genaug verify` runs project acceptance checks: project keys, hosted agent test, tools,
logs, usage, usage limits, observability, runtime-policy model routing, memory lifecycle,
and tool-call audit, then prints dashboard URLs for the same tenant.

## Dashboard

```bash
genaug dashboard open --project petstore-agent
```

## Local mock for offline tests

The deterministic local HTTP mock ships with the package and is launched as a module
(it is not a `genaug` subcommand):

```bash
uv run --project packages/cli python -m platform_cli.local_mock \
  --host 127.0.0.1 --port 8787 --quiet
```

It serves offline app contract tests against `/v1/responses`, memory routes, project
setup, OpenAPI tool registration, key management, logs, usage, observability, health
checks, idempotency replays, trace metadata, structured-output fixtures, and semantic
SSE fixtures. Point the SDKs at it with `GENAUG_API_BASE_URL=http://127.0.0.1:8787`.

## Console entrypoint

The console command is defined in `pyproject.toml`:

```toml
[project.scripts]
genaug = "platform_cli.main:app"
```
