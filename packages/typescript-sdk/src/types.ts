export type JsonObject = Record<string, unknown>;

export interface GeneralAugmentClientOptions {
  apiKey: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
  /**
   * Number of automatic retries for transient failures (HTTP 429/5xx and
   * connection/timeout errors). Defaults to 2. Set to 0 to disable.
   */
  maxRetries?: number;
}

export interface APIErrorDetail {
  code?: string;
  reason?: string;
  message?: string;
  request_id?: string | null;
  retry_after?: number;
  retry_after_seconds?: number;
  [key: string]: unknown;
}

export interface RateLimitInfo {
  retryAfter?: number;
  limit?: number;
  remaining?: number;
  reset?: number;
}

export interface Project {
  id: string;
  name: string;
  slug: string;
  status: string;
  enabled_tool_ids?: string[];
  mcp_servers?: JsonObject[];
  tool_discovery?: {
    mode: "auto" | "always" | "direct";
    direct_schema_tool_limit: number;
    max_search_results: number;
  };
}

export interface ListProjectsOptions {
  limit?: number;
  offset?: number;
}

export interface AgentTestResponse {
  response?: string | null;
  response_text: string;
  warnings: string[];
  metadata: JsonObject;
  error?: string | null;
  details?: string | null;
  suggestion?: string | null;
  model_used?: string | null;
  cost_usd: number;
}

export interface UsageResponse {
  project_id: string;
  start_date: string;
  end_date: string;
  totals: JsonObject;
  days: JsonObject[];
  limits?: JsonObject;
}

export interface LinkUserOptions {
  phone: string;
  appUserId: string;
  providerName?: string;
  metadata?: JsonObject;
}

export interface OpenAPIRegistrationOptions {
  client: import("./client.js").GeneralAugmentClient;
  projectId: string;
  includePaths?: string[];
  excludePaths?: string[];
  targetCount?: number;
  autoDeploy?: boolean;
}

export interface ResponseRequestOptions {
  idempotencyKey?: string;
  requestId?: string;
  traceparent?: string;
  tracestate?: string;
}

export type ResponseCreateRequest = JsonObject;
export type ResponseObject = JsonObject;

export interface ResponseStreamEvent {
  event: string;
  data: unknown;
}

export type StoreMemoryRequest = JsonObject & {
  user_id?: string;
  user?: string;
  fact?: string;
  content?: string;
  fact_type?: string;
  importance_score?: number;
  source?: string;
  metadata?: JsonObject;
  user_profile?: JsonObject;
  idempotency_key?: string;
};

export type MemorySearchRequest = JsonObject & {
  user_id?: string;
  user?: string;
  query?: string;
  limit?: number;
  min_similarity?: number;
  fact_type?: string;
  min_importance?: number;
  source?: string;
  created_after?: string;
};

export type MemoryStoreResponse = JsonObject;
export type MemorySearchResponse = JsonObject;
export type MemoryProfileResponse = JsonObject;
export type MemoryDeleteResponse = JsonObject;
