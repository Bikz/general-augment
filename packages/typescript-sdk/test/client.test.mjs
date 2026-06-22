import assert from "node:assert/strict";
import test from "node:test";

import {
  GeneralAugmentAPIError,
  GeneralAugmentClient,
  responseOutputText,
  responseStructuredOutput,
} from "../dist/index.js";

test("responseOutputText reads top-level and content output text", () => {
  assert.equal(responseOutputText({ output_text: "hello" }), "hello");
  assert.equal(
    responseOutputText({
      output: [
        {
          type: "message",
          content: [
            { type: "output_text", text: "one" },
            { type: "text", text: " two" },
            { type: "input_text", text: "ignored" },
          ],
        },
      ],
    }),
    "one two",
  );
});

test("responseStructuredOutput reads parsed values or parses output text", () => {
  assert.deepEqual(responseStructuredOutput({ output_parsed: { ok: true } }), { ok: true });
  assert.deepEqual(
    responseStructuredOutput({
      output: [{ type: "message", content: [{ type: "output_text", parsed: { seat: "window" } }] }],
    }),
    { seat: "window" },
  );
  assert.deepEqual(
    responseStructuredOutput({
      output: [{ type: "message", content: [{ type: "output_text", text: "{\"seat\":\"aisle\"}" }] }],
    }),
    { seat: "aisle" },
  );
  assert.throws(
    () => responseStructuredOutput({ output: [{ type: "message", content: [{ type: "output_text", text: "nope" }] }] }),
    /not valid JSON/,
  );
});

test("createResponse sends bearer auth and response headers", async () => {
  const calls = [];
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return jsonResponse({ id: "resp_123", output_text: "ok" });
    },
  });

  const response = await client.createResponse(
    { model: "balanced", input: "hi" },
    {
      idempotencyKey: "turn-1",
      requestId: "req_123",
      traceparent: "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    },
  );

  assert.equal(response.id, "resp_123");
  assert.equal(calls[0].url, "https://api.example.test/v1/responses");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.Authorization, "Bearer gaadmlive_test");
  assert.equal(calls[0].init.headers["X-Idempotency-Key"], "turn-1");
  assert.equal(calls[0].init.headers["X-Request-ID"], "req_123");
  assert.equal(calls[0].init.headers.traceparent, "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01");
});

test("admin helpers encode project IDs and send admin auth", async () => {
  const calls = [];
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_admin",
    baseUrl: "https://api.example.test/",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return jsonResponse({ project_id: "project/one", start_date: "2026-04-01", end_date: "2026-04-24", totals: {}, days: [] });
    },
  });

  await client.usage("project/one", { startDate: "2026-04-01", endDate: "2026-04-24" });

  assert.equal(
    calls[0].url,
    "https://api.example.test/api/v1/admin/projects/project%2Fone/usage?start_date=2026-04-01&end_date=2026-04-24",
  );
  assert.equal(calls[0].init.headers["X-Admin-Key"], "gaadmlive_admin");
});

test("listProjects accepts pagination params", async () => {
  const calls = [];
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_admin",
    baseUrl: "https://api.example.test",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return jsonResponse({ items: [{ id: "proj-1", name: "One", slug: "one", status: "active" }] });
    },
  });

  assert.deepEqual(await client.listProjects({ limit: 25, offset: 50 }), [
    { id: "proj-1", name: "One", slug: "one", status: "active" },
  ]);
  assert.equal(calls[0].url, "https://api.example.test/api/v1/admin/projects?limit=25&offset=50");
  assert.equal(calls[0].init.headers["X-Admin-Key"], "gaadmlive_admin");
});

test("API errors expose structured reason codes and rate limit metadata", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    maxRetries: 0,
    fetchImpl: async () =>
      jsonResponse(
        {
          detail: {
            code: "rate_limited",
            reason: "admin_api_key_rate_limit_exceeded",
            message: "Admin API key rate limit exceeded.",
            request_id: "req_429",
          },
        },
        {
          status: 429,
          statusText: "Too Many Requests",
          headers: {
            "Retry-After": "60",
            "X-RateLimit-Limit": "120",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1777046400",
          },
        },
      ),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.statusCode, 429);
      assert.equal(error.code, "rate_limited");
      assert.equal(error.reason, "admin_api_key_rate_limit_exceeded");
      assert.equal(error.requestId, "req_429");
      assert.equal(error.retryAfter, 60);
      assert.deepEqual(error.rateLimit, {
        retryAfter: 60,
        limit: 120,
        remaining: 0,
        reset: 1777046400,
      });
      assert.equal(
        error.detail,
        '{"code":"rate_limited","reason":"admin_api_key_rate_limit_exceeded","message":"Admin API key rate limit exceeded.","request_id":"req_429"}',
      );
      return true;
    },
  );
});

test("successful malformed JSON raises typed API error", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () =>
      new Response("not-json", {
        status: 200,
        headers: { "X-Request-ID": "req_bad_json" },
      }),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.statusCode, 200);
      assert.equal(error.reason, "malformed_json");
      assert.equal(error.requestId, "req_bad_json");
      return true;
    },
  );
});

test("fetch failures raise typed API error", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    maxRetries: 0,
    fetchImpl: async () => {
      throw new TypeError("network down");
    },
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.statusCode, 0);
      assert.equal(error.reason, "request_failed");
      return true;
    },
  );
});

test("request timeouts abort stalled fetches with typed API error", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    timeoutMs: 1,
    maxRetries: 0,
    fetchImpl: async (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal.addEventListener("abort", () => {
          reject(new DOMException("The operation was aborted.", "AbortError"));
        });
      }),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.statusCode, 0);
      assert.equal(error.reason, "request_timeout");
      return true;
    },
  );
});

test("request timeouts cover stalled JSON response bodies", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    timeoutMs: 1,
    maxRetries: 0,
    fetchImpl: async (_url, init) => ({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () =>
        new Promise((_resolve, reject) => {
          if (init.signal.aborted) {
            reject(new DOMException("The operation was aborted.", "AbortError"));
            return;
          }
          init.signal.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        }),
    }),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.reason, "request_timeout");
      return true;
    },
  );
});

test("timeoutMs can be disabled for tenant-managed fetch policies", async () => {
  const calls = [];
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    timeoutMs: 0,
    fetchImpl: async (_url, init) => {
      calls.push(init);
      return jsonResponse({ id: "resp_123", output_text: "ok" });
    },
  });

  await client.createResponse({ input: "hi" });

  assert.equal(calls[0].signal, undefined);
});

test("invalid timeoutMs fails fast", () => {
  assert.throws(
    () =>
      new GeneralAugmentClient({
        apiKey: "gaadmlive_test",
        timeoutMs: -1,
      }),
    /timeoutMs/,
  );
});

test("API errors fall back to correlation headers when body omits request ID", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () =>
      jsonResponse(
        {
          detail: {
            code: "idempotency_key_in_progress",
            reason: "idempotency_key_in_progress",
            message: "A request with this idempotency key is still processing.",
          },
        },
        {
          status: 409,
          headers: {
            "X-Request-ID": "req_header_409",
            "Retry-After": "1",
          },
        },
      ),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.reason, "idempotency_key_in_progress");
      assert.equal(error.requestId, "req_header_409");
      assert.equal(error.retryAfter, 1);
      return true;
    },
  );
});

test("API errors normalize flat 402 response bodies", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () =>
      jsonResponse(
        {
          code: "usage_limit_reached",
          reason: "pricing_tier_agent_turn_limit_reached",
          message: "Project daily agent turn limit reached.",
          request_id: "req_402",
          pricing_tier: "free",
        },
        { status: 402, statusText: "Payment Required" },
      ),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.statusCode, 402);
      assert.equal(error.code, "usage_limit_reached");
      assert.equal(error.reason, "pricing_tier_agent_turn_limit_reached");
      assert.equal(error.requestId, "req_402");
      assert.equal(error.error.pricing_tier, "free");
      return true;
    },
  );
});

test("API errors preserve top-level fields with nested detail objects", async () => {
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () =>
      jsonResponse(
        {
          detail: { message: "Project budget exceeded" },
          code: "project_budget_exceeded",
          reason: "project_budget_exceeded",
          request_id: "req_budget",
        },
        { status: 402, statusText: "Payment Required" },
      ),
  });

  await assert.rejects(
    () => client.createResponse({ input: "hi" }),
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.code, "project_budget_exceeded");
      assert.equal(error.reason, "project_budget_exceeded");
      assert.equal(error.requestId, "req_budget");
      assert.equal(
        error.detail,
        '{"message":"Project budget exceeded","code":"project_budget_exceeded","reason":"project_budget_exceeded","request_id":"req_budget"}',
      );
      return true;
    },
  );
});

test("streamResponse parses semantic SSE events", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: response.created\ndata: {"type":"response.created"}\n\n'));
      controller.enqueue(new TextEncoder().encode('data: {"type":"response.completed"}\n\n'));
      controller.close();
    },
  });
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () => new Response(stream, { status: 200 }),
  });

  const events = [];
  for await (const event of client.streamResponse({ input: "hi" })) {
    events.push(event);
  }

  assert.deepEqual(events, [
    { event: "response.created", data: { type: "response.created" } },
    { event: "message", data: { type: "response.completed" } },
  ]);
});

test("mock contract flow covers responses and memory fixtures", async () => {
  const calls = [];
  const memories = [];
  const client = new GeneralAugmentClient({
    apiKey: "local-test",
    baseUrl: "http://127.0.0.1:8787",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      const path = new URL(String(url)).pathname;
      if (path === "/v1/responses") {
        return jsonResponse({
          id: "resp_mock_contract",
          status: "completed",
          output: [
            {
              type: "message",
              content: [{ type: "output_text", text: "local-mock-ok" }],
            },
          ],
          usage: { input_tokens: 8, output_tokens: 4, total_tokens: 12 },
          metadata: { general_augment_model: "mock", fixture: "sdk-contract" },
        });
      }
      if (path === "/api/v1/agent/memory/store") {
        const body = JSON.parse(init.body);
        memories.push(body);
        return jsonResponse({ memory_id: "mem_mock_contract", ...body });
      }
      if (path === "/api/v1/agent/memory/search") {
        return jsonResponse({ user_id: "sdk-contract-user", facts: memories });
      }
      if (path === "/api/v1/agent/memory/profile/sdk-contract-user") {
        return jsonResponse({ user_id: "sdk-contract-user", recent_facts: memories });
      }
      throw new Error(`Unexpected mock contract URL: ${String(url)}`);
    },
  });

  const response = await client.createResponse(
    {
      model: "balanced",
      user: "sdk-contract-user",
      input: "Reply exactly with: local-mock-ok",
    },
    { idempotencyKey: "sdk-contract-turn-1", requestId: "req_contract_ts" },
  );
  const stored = await client.storeMemory({
    user_id: "sdk-contract-user",
    fact: "User prefers window seats",
    fact_type: "preference",
  });
  const search = await client.searchMemory({
    user_id: "sdk-contract-user",
    query: "seat preference",
    limit: 3,
  });
  const profile = await client.memoryProfile("sdk-contract-user");

  assert.equal(responseOutputText(response), "local-mock-ok");
  assert.equal(stored.memory_id, "mem_mock_contract");
  assert.equal(search.facts.length, 1);
  assert.equal(profile.recent_facts[0].fact, "User prefers window seats");
  assert.deepEqual(
    calls.map((call) => [call.init.method, new URL(call.url).pathname]),
    [
      ["POST", "/v1/responses"],
      ["POST", "/api/v1/agent/memory/store"],
      ["POST", "/api/v1/agent/memory/search"],
      ["GET", "/api/v1/agent/memory/profile/sdk-contract-user"],
    ],
  );
  assert.equal(calls[0].init.headers.Authorization, "Bearer local-test");
});

test("createResponse retries 429 and honors Retry-After, reusing idempotency key", async () => {
  const keys = [];
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    maxRetries: 2,
    fetchImpl: async (_url, init) => {
      keys.push(init.headers["X-Idempotency-Key"]);
      if (keys.length === 1) {
        return jsonResponse({ detail: "slow down" }, { status: 429, headers: { "Retry-After": "0" } });
      }
      return jsonResponse({ id: "resp_ok" });
    },
  });

  const response = await client.createResponse({ input: "hi" });

  assert.equal(response.id, "resp_ok");
  assert.equal(keys.length, 2);
  // Auto-generated key is reused across the retry so the turn cannot double-execute.
  assert.ok(keys[0]);
  assert.equal(keys[0], keys[1]);
});

test("request retries 503 then succeeds", async () => {
  let calls = 0;
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_admin",
    baseUrl: "https://api.example.test",
    maxRetries: 3,
    fetchImpl: async () => {
      calls += 1;
      if (calls < 3) {
        return jsonResponse({ detail: "unavailable" }, { status: 503 });
      }
      return jsonResponse({ items: [{ id: "p1", name: "One", slug: "one", status: "active" }] });
    },
  });

  const projects = await client.listProjects();
  assert.equal(projects.length, 1);
  assert.equal(calls, 3);
});

test("retries can be disabled with maxRetries=0", async () => {
  let calls = 0;
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_admin",
    baseUrl: "https://api.example.test",
    maxRetries: 0,
    fetchImpl: async () => {
      calls += 1;
      return jsonResponse({ detail: "unavailable" }, { status: 503 });
    },
  });

  await assert.rejects(
    () => client.listProjects(),
    (error) => {
      assert.equal(error.statusCode, 503);
      return true;
    },
  );
  assert.equal(calls, 1);
});

test("connection failures are retried before raising", async () => {
  let calls = 0;
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_admin",
    baseUrl: "https://api.example.test",
    maxRetries: 2,
    fetchImpl: async () => {
      calls += 1;
      if (calls < 2) {
        throw new TypeError("network down");
      }
      return jsonResponse({ items: [] });
    },
  });

  assert.deepEqual(await client.listProjects(), []);
  assert.equal(calls, 2);
});

test("createResponse auto-generates an idempotency key when omitted", async () => {
  let seenKey;
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async (_url, init) => {
      seenKey = init.headers["X-Idempotency-Key"];
      return jsonResponse({ id: "resp_1" });
    },
  });

  await client.createResponse({ input: "hi" });
  assert.ok(seenKey && seenKey.length > 0);
});

test("createResponse preserves a caller-supplied idempotency key", async () => {
  let seenKey;
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async (_url, init) => {
      seenKey = init.headers["X-Idempotency-Key"];
      return jsonResponse({ id: "resp_1" });
    },
  });

  await client.createResponse({ input: "hi" }, { idempotencyKey: "mine-1" });
  assert.equal(seenKey, "mine-1");
});

test("Retry-After in HTTP-date form is parsed (not NaN), capped backoff applied", async () => {
  const httpDate = new Date(Date.now() + 50).toUTCString();
  let calls = 0;
  const started = Date.now();
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_admin",
    baseUrl: "https://api.example.test",
    maxRetries: 1,
    fetchImpl: async () => {
      calls += 1;
      if (calls === 1) {
        return jsonResponse({ detail: "slow" }, { status: 503, headers: { "Retry-After": httpDate } });
      }
      return jsonResponse({ items: [] });
    },
  });

  await client.listProjects();
  // If the HTTP-date had produced NaN, backoff would have fallen back to
  // jittered exponential (>=250ms). The date is ~50ms out, so retry is prompt.
  assert.equal(calls, 2);
  assert.ok(Date.now() - started < 2000);
});

test("streamResponse stops cleanly on [DONE] sentinel", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: response.created\ndata: {"type":"response.created"}\n\n'));
      controller.enqueue(new TextEncoder().encode("data: [DONE]\n\n"));
      controller.enqueue(new TextEncoder().encode('event: response.completed\ndata: {"type":"never"}\n\n'));
      controller.close();
    },
  });
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () => new Response(stream, { status: 200 }),
  });

  const events = [];
  for await (const event of client.streamResponse({ input: "hi" })) {
    events.push(event);
  }
  assert.deepEqual(events.map((e) => e.event), ["response.created"]);
});

test("streamResponse raises on a mid-stream error frame", async () => {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: response.created\ndata: {"type":"response.created"}\n\n'));
      controller.enqueue(new TextEncoder().encode('event: error\ndata: {"reason":"agent_failed","message":"boom"}\n\n'));
      controller.close();
    },
  });
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () => new Response(stream, { status: 200 }),
  });

  const events = [];
  await assert.rejects(
    async () => {
      for await (const event of client.streamResponse({ input: "hi" })) {
        events.push(event);
      }
    },
    (error) => {
      assert.ok(error instanceof GeneralAugmentAPIError);
      assert.equal(error.reason, "agent_failed");
      assert.ok(error.detail.includes("boom"));
      return true;
    },
  );
  assert.deepEqual(events.map((e) => e.event), ["response.created"]);
});

test("streamResponse releases the reader lock on early break", async () => {
  let released = false;
  const underlying = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('event: a\ndata: {"n":1}\n\n'));
      controller.enqueue(new TextEncoder().encode('event: b\ndata: {"n":2}\n\n'));
      controller.close();
    },
  });
  // Wrap getReader so we can observe releaseLock being called.
  const body = {
    getReader() {
      const reader = underlying.getReader();
      const originalRelease = reader.releaseLock.bind(reader);
      reader.releaseLock = () => {
        released = true;
        originalRelease();
      };
      return reader;
    },
  };
  const client = new GeneralAugmentClient({
    apiKey: "gaadmlive_test",
    baseUrl: "https://api.example.test",
    fetchImpl: async () => ({ ok: true, status: 200, headers: new Headers(), body }),
  });

  for await (const event of client.streamResponse({ input: "hi" })) {
    assert.equal(event.event, "a");
    break; // early exit before consuming the whole stream
  }

  assert.equal(released, true);
});

test("invalid maxRetries fails fast", () => {
  assert.throws(
    () =>
      new GeneralAugmentClient({
        apiKey: "gaadmlive_test",
        maxRetries: -1,
      }),
    /maxRetries/,
  );
});

function jsonResponse(payload, init = {}) {
  return new Response(JSON.stringify(payload), {
    status: init.status ?? 200,
    statusText: init.statusText,
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
  });
}
