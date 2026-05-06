# General Augment TypeScript SDK

Backend SDK for General Augment app integrations. Use it from trusted server code.
Project-scoped keys are for app traffic such as Responses and memory calls. Admin and
setup helpers require a management/admin-capable key and send it as `X-Admin-Key`.

Agent-readable docs: `https://docs.generalaugment.com/llms.txt`. SDK reference:
`https://docs.generalaugment.com/sdk/reference/` or the Markdown export at
`https://docs.generalaugment.com/markdown/sdk/reference.md`.

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
});

console.log(`General Augment SDK ${VERSION}`);
```

`timeoutMs` defaults to 60 seconds and raises a typed `request_timeout` API error when
the platform request does not receive a response in time. Set `timeoutMs: 0` only when
your app already applies its own fetch timeout policy.

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
    process.stdout.write(String((event.data as any).delta ?? ""));
  }
}
```

## Memory

```ts
const stored = await client.storeMemory({
  user_id: "app-user-123",
  fact: "User prefers window seats",
  fact_type: "preference",
  importance_score: 0.9,
  idempotency_key: "memory-window-seat-1",
});

await client.searchMemory({ user_id: "app-user-123", query: "seat preference" });
await client.memoryProfile("app-user-123");
await client.deleteMemory(String(stored.memory_id), "app-user-123");
await client.purgeUserMemory("app-user-123");
```

## Usage

```ts
const usage = await client.usage("project_123", {
  startDate: "2026-04-01",
  endDate: "2026-04-24",
});
console.log(usage.totals);
```

## Local Tests

Run the local mock server and point the SDK at it:

```bash
uv run --project packages/cli genaug mock --host 127.0.0.1 --port 8787 --quiet
export GENAUG_API_BASE_URL="http://127.0.0.1:8787"
export GENAUG_API_KEY="local-test"
npm install
npm run build
node examples/contract-test.mjs
```

The contract example covers a Responses turn plus memory store/search against the same
deterministic routes used by app backend CI.

## Other Helpers

The SDK also includes `createProjectFromConfig`, `registerOpenAPITools`, `linkUser`,
`usage`, and `testAgent` for admin and integration workflows.
