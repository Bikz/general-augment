# Packages

General Augment publishes three public packages.

The canonical docs live at https://docs.generalaugment.com. Coding agents should start
with `https://docs.generalaugment.com/llms.txt` and use `/markdown/...` page exports
when they need a single source page.

## CLI

```bash
pip install general-augment-cli
genaug --help
```

Use `genaug` for project setup and migration, OpenAPI tool registration, provider-key
health checks, connector and skill configuration, smoke tests, and `verify` acceptance
checks.

## Python SDK

```bash
pip install general-augment-sdk
```

Use from trusted server code for `/v1/responses`, memory operations, usage checks, and
admin setup helpers.

## TypeScript SDK

```bash
npm install @general-augment/sdk
```

Use from trusted Node.js backend code. Keep API keys out of browser and mobile bundles.

## Local Development

The deterministic local HTTP mock ships with the CLI package and is launched as a Python
module (it is not a `genaug` subcommand):

```bash
uv run --project packages/cli python -m platform_cli.local_mock \
  --host 127.0.0.1 --port 8787 --quiet
```

The local mock supports deterministic Responses, memory, usage, logs, trace metadata,
and project setup routes for app CI. Point the SDKs at it with
`GENAUG_API_BASE_URL=http://127.0.0.1:8787`.
