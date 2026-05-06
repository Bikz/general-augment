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

Use `genaug` for project setup, OpenAPI tool registration, provider-key health checks,
smoke tests, support receipts, memory checks, and onboarding verification.

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

```bash
genaug mock --host 127.0.0.1 --port 8787 --quiet
```

The local mock supports deterministic Responses, memory, usage, logs, trace metadata,
and project setup routes for app CI.
