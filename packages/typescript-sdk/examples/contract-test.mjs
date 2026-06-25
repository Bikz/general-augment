import assert from "node:assert/strict";

import {
  GeneralAugmentClient,
  responseOutputText,
} from "../dist/index.js";

const baseUrl = process.env.GENAUG_API_BASE_URL ?? "http://127.0.0.1:8787";
const apiKey = process.env.GENAUG_API_KEY ?? "local-test";

const client = new GeneralAugmentClient({ apiKey, baseUrl });

const response = await client.createResponse(
  {
    model: "balanced",
    user: "sdk-contract-user",
    input: "Reply exactly with: local-mock-ok",
    metadata: { fixture: "sdk-contract" },
  },
  {
    idempotencyKey: "sdk-contract-turn-1",
    requestId: "req_sdk_contract_ts",
  },
);

assert.equal(response.status, "completed");
assert.match(responseOutputText(response), /local-mock-ok|Reply exactly with/i);

const stored = await client.storeMemory({
  user_id: "sdk-contract-user",
  fact: "User prefers window seats",
  fact_type: "preference",
  collection_key: "travel_preferences",
  idempotency_key: "sdk-contract-memory-1",
});
assert.ok(stored.memory_id ?? stored.id);

const search = await client.searchMemory({
  user_id: "sdk-contract-user",
  query: "seat preference",
  limit: 3,
  collection_key: "travel_preferences",
});
assert.ok(Array.isArray(search.facts));

console.log("General Augment TypeScript SDK contract example passed.");
