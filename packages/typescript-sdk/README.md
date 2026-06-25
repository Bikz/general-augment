# General Augment TypeScript SDK

General Augment is the agent backend for your app. This TypeScript SDK is for trusted
server-side app integrations.

Project-scoped keys carry app traffic such as Responses and memory calls and are sent
as a bearer token. Admin and setup helpers require a management/admin-capable key and
send it as `X-Admin-Key`.

```bash
npm install @general-augment/sdk
```

```ts
import {
  GeneralAugmentClient,
  VERSION,
  responseOutputText,
  responseStructuredOutput,
} from "@general-augment/sdk";

const client = new GeneralAugmentClient({
  apiKey: process.env.GENAUG_API_KEY!,
  baseUrl: process.env.GENAUG_API_BASE_URL ?? "https://api.generalaugment.com",
  timeoutMs: 60_000,
  maxRetries: 2,
});

console.log(`General Augment SDK ${VERSION}`);
```

`timeoutMs` defaults to 60 seconds and raises a typed `request_timeout` API error when
the platform request does not receive a response in time. Set `timeoutMs: 0` only when
your app already applies its own fetch timeout policy. `maxRetries` (default 2) retries
transient failures (HTTP 429/5xx and connection/timeout errors) with exponential backoff
and `Retry-After` support; `createResponse` always sends an idempotency key so retries
are safe.

## Responses

```ts
const response = await client.createResponse(
  {
    model: "balanced",
    user: "app-user-123",
    input: "Reply with a concise onboarding summary.",
    metadata: { feature: "onboarding" },
  },
  { idempotencyKey: "onboarding-turn-1", requestId: "req_app_123" },
);

console.log(responseOutputText(response));
```

`createResponse` auto-generates an idempotency key for each billable turn; pass
`idempotencyKey` to supply your own. The request options also accept `requestId`,
`traceparent`, and `tracestate`.

Structured output:

```ts
const structuredResponse = await client.createResponse({
  model: "balanced",
  user: "app-user-123",
  input: "Extract the user's preference: window seat.",
  text: {
    format: {
      type: "json_schema",
      name: "preference",
      strict: true,
      schema: {
        type: "object",
        required: ["seat"],
        properties: { seat: { type: "string" } },
        additionalProperties: false,
      },
    },
  },
});

const preference = responseStructuredOutput<{ seat: string }>(structuredResponse);
```

Streaming:

```ts
for await (const event of client.streamResponse({
  model: "balanced",
  user: "app-user-123",
  input: "Draft a two sentence welcome message.",
})) {
  if (event.event === "response.output_text.delta") {
    process.stdout.write(String((event.data as { delta?: string }).delta ?? ""));
  }
}
```

`streamResponse` yields `{ event, data }` server-sent events and throws a
`GeneralAugmentAPIError` if the stream emits an error event.

## Memory

```ts
const stored = await client.storeMemory({
  user_id: "app-user-123",
  fact: "User prefers window seats",
  fact_type: "preference",
  importance_score: 0.9,
  idempotency_key: "memory-window-seat-1",
});

await client.searchMemory({
  user_id: "app-user-123",
  query: "seat preference",
  limit: 5,
});

await client.memoryProfile("app-user-123");
await client.deleteMemory(String(stored.memory_id), "app-user-123");
await client.purgeUserMemory("app-user-123");
```

## Error Handling

```ts
import { GeneralAugmentAPIError } from "@general-augment/sdk";

try {
  await client.createResponse({ model: "balanced", input: "Hello" });
} catch (error) {
  if (error instanceof GeneralAugmentAPIError) {
    if (error.reason === "rate_limit_exceeded") {
      console.log(`Retry after ${error.retryAfter} seconds`);
    }
    console.log(error.statusCode, error.requestId, error.detail);
  }
}
```

`GeneralAugmentAPIError` preserves the HTTP `statusCode`, the stable `reason`/`code`
when the API returns one, `retryAfter`, the `rateLimit` headers, the `requestId`, and
the decoded error detail.

## Admin & integration helpers

Admin helpers use a management/admin-capable key sent as `X-Admin-Key`.

```ts
const projects = await client.listProjects({ limit: 25 });
const project = await client.getProject("project_123");

const created = await client.createProjectFromConfig(yamlContent, {
  soulContent,
  skills: ["website-builder"],
});

await client.updateProject("project_123", { name: "Renamed" });

const usage = await client.usage("project_123", {
  startDate: "2026-04-01",
  endDate: "2026-04-24",
});
console.log(usage.totals);

const test = await client.testAgent("project_123", "Hello from ops", {
  channel: "whatsapp",
});
console.log(test.response_text);

await client.registerOpenAPITools("project_123", "https://example.com/openapi.json", {
  targetCount: 15,
  autoDeploy: true,
});
```

Identity linking maps your app's users to a channel identity:

```ts
await client.linkUser("project_123", {
  phone: "+15555550123",
  appUserId: "app-user-123",
  providerName: "app",
});
await client.resolveUser("project_123", "+15555550123");
await client.unlinkUser("project_123", "+15555550123");
```

Free-function equivalents are also exported for code that prefers them:
`linkUser`, `resolveUser`, `unlinkUser`, `registerFromOpenAPI`, and `testAgent`.
`AgentClient` wraps a client + project id so you can call `agent.test(message)`
without repeating the project id.

## Typed request/response models

The Fern-generated typed contract for the curated public API is re-exported under the
`types` namespace (the TypeScript mirror of Python's `genaug.types`). Import from it
when you want statically-typed request/response shapes. The hand-written
`GeneralAugmentClient` keeps its own hardened fetch transport and accepts
`JsonObject`-based payloads for `createResponse`/`streamResponse` and memory calls.

```ts
import { types } from "@general-augment/sdk";
```

## Local Tests

Run the local mock server (shipped with the CLI package) and point the SDK at it:

```bash
uv run --project packages/cli python -m platform_cli.local_mock \
  --host 127.0.0.1 --port 8787 --quiet
export GENAUG_API_BASE_URL="http://127.0.0.1:8787"
export GENAUG_API_KEY="local-test"
npm install
npm run build
node examples/contract-test.mjs
```

The contract example covers a Responses turn plus memory store/search against the same
deterministic routes used by app-backend CI.
