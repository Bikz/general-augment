export const VERSION = "0.1.0";

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
