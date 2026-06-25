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

// Fern-generated typed request/response models for the curated public API,
// re-exported under the `types` namespace as the drift-checked typed contract —
// the TypeScript mirror of Python's `genaug.types`. The re-export is CURATED to
// the same 24 public models Python exposes (see src/types-generated.ts). Import
// from `types` when you want the statically-typed request/response shapes; the
// hand-written GeneralAugmentClient keeps its own hardened fetch transport and
// JsonObject-based payloads for backwards compatibility.
export * as types from "./types-generated.js";
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
