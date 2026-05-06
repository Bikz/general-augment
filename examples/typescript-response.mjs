import { GeneralAugmentClient, responseOutputText } from "@general-augment/sdk";

const apiKey = process.env.GENAUG_API_KEY;
if (!apiKey) {
  throw new Error("Set GENAUG_API_KEY to a project-scoped key");
}

const client = new GeneralAugmentClient({
  apiKey,
  baseUrl: process.env.GENAUG_API_BASE_URL ?? "https://api.generalaugment.com",
});

const response = await client.createResponse(
  {
    model: "balanced",
    user: "app-user-123",
    input: "Reply with a concise welcome message.",
    metadata: { example: "typescript" },
  },
  { idempotencyKey: "example-typescript-response-1" },
);

console.log(responseOutputText(response));
