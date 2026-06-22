// Single source of truth: the version is imported from package.json so the
// published VERSION constant can never drift from the package manifest.
import pkg from "../package.json" with { type: "json" };

export const VERSION: string = (pkg as { version: string }).version;

export { AgentClient, testAgent } from "./agent.js";
export {
  GeneralAugmentAPIError,
  GeneralAugmentClient,
  responseOutputText,
  responseStructuredOutput
} from "./client.js";
export { linkUser, resolveUser, unlinkUser } from "./identity.js";
export { registerFromOpenAPI } from "./tools.js";
export type {
  AgentTestResponse,
  APIErrorDetail,
  GeneralAugmentClientOptions,
  JsonObject,
  LinkUserOptions,
  MemoryDeleteResponse,
  MemoryProfileResponse,
  MemorySearchRequest,
  MemorySearchResponse,
  MemoryStoreResponse,
  OpenAPIRegistrationOptions,
  Project,
  RateLimitInfo,
  ResponseCreateRequest,
  ResponseObject,
  ResponseRequestOptions,
  ResponseStreamEvent,
  StoreMemoryRequest,
  UsageResponse
} from "./types.js";
