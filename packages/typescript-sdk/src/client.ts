import type {
  AgentTestResponse,
  APIErrorDetail,
  GeneralAugmentClientOptions,
  JsonObject,
  ListProjectsOptions,
  LinkUserOptions,
  MemoryDeleteResponse,
  MemoryProfileResponse,
  MemorySearchRequest,
  MemorySearchResponse,
  MemoryStoreResponse,
  Project,
  ResponseCreateRequest,
  ResponseObject,
  ResponseRequestOptions,
  ResponseStreamEvent,
  RateLimitInfo,
  StoreMemoryRequest,
  UsageResponse
} from "./types.js";

const ADMIN_PREFIX = "/api/v1/admin";
const INTEGRATIONS_PREFIX = "/api/v1/integrations";
const DEFAULT_BASE_URL = "https://api.generalaugment.com";
const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_MAX_RETRIES = 2;
const RETRY_STATUS_CODES = new Set([429, 500, 502, 503, 504]);
const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 8_000;

export class GeneralAugmentAPIError extends Error {
  statusCode: number;
  detail: string;
  error?: APIErrorDetail;
  code?: string;
  reason?: string;
  requestId?: string;
  retryAfter?: number;
  rateLimit: RateLimitInfo;

  constructor(
    statusCode: number,
    detail: string,
    options: { error?: APIErrorDetail; rateLimit?: RateLimitInfo; requestId?: string } = {}
  ) {
    super(`General Augment API returned ${statusCode}: ${detail}`);
    this.name = "GeneralAugmentAPIError";
    this.statusCode = statusCode;
    this.detail = detail;
    this.error = options.error;
    this.code = stringField(options.error?.code);
    this.reason = stringField(options.error?.reason);
    this.requestId = options.requestId ?? stringField(options.error?.request_id);
    this.retryAfter =
      options.rateLimit?.retryAfter ??
      numberField(options.error?.retry_after) ??
      numberField(options.error?.retry_after_seconds);
    this.rateLimit = {
      ...(options.rateLimit ?? {}),
      ...(this.retryAfter === undefined ? {} : { retryAfter: this.retryAfter })
    };
  }
}

export class GeneralAugmentClient {
  private apiKey: string;
  private baseUrl: string;
  private fetchImpl: typeof fetch;
  private timeoutMs: number | undefined;
  private maxRetries: number;

  constructor(options: GeneralAugmentClientOptions) {
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.timeoutMs = normalizeTimeoutMs(options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    this.maxRetries = normalizeMaxRetries(options.maxRetries ?? DEFAULT_MAX_RETRIES);
  }

  async adminRequest<T>(
    method: string,
    path: string,
    options: { json?: JsonObject; params?: Record<string, string | undefined> } = {}
  ): Promise<T> {
    return this.request<T>(method, `${ADMIN_PREFIX}${path}`, options);
  }

  async integrationRequest<T>(
    method: string,
    path: string,
    options: { json?: JsonObject; params?: Record<string, string | undefined> } = {}
  ): Promise<T> {
    return this.request<T>(method, `${INTEGRATIONS_PREFIX}${path}`, options);
  }

  async createResponse(
    payload: ResponseCreateRequest,
    options: ResponseRequestOptions = {}
  ): Promise<ResponseObject> {
    return this.request<ResponseObject>("POST", "/v1/responses", {
      json: payload,
      headers: responseHeaders(options, { autoIdempotency: true }),
      auth: "bearer"
    });
  }

  async *streamResponse(
    payload: ResponseCreateRequest,
    options: ResponseRequestOptions = {}
  ): AsyncIterable<ResponseStreamEvent> {
    const timeout = requestTimeout(this.timeoutMs);
    let response: Response;
    try {
      response = await this.sendFetch(
        "POST",
        "/v1/responses",
        {
          json: { ...payload, stream: true },
          headers: responseHeaders(options, { autoIdempotency: true }),
          auth: "bearer"
        },
        timeout.signal
      );
      if (!response.ok) {
        const errorInfo = await responseErrorInfo(response);
        throw new GeneralAugmentAPIError(response.status, errorInfo.detail, {
          error: errorInfo.error,
          requestId: errorInfo.requestId,
          rateLimit: errorInfo.rateLimit
        });
      }
    } catch (error) {
      timeout.cancel();
      throw requestFailureError(error);
    }
    if (!response.body) {
      timeout.cancel();
      return;
    }
    // The timeout signal (and its abort timer) is intentionally NOT cancelled
    // here: it must cover the whole streamed body, not just response headers.
    // parseSSE cancels the timer once iteration finishes or aborts.
    try {
      yield* parseSSE(response.body, timeout);
    } catch (error) {
      throw requestFailureError(error);
    }
  }

  async storeMemory(payload: StoreMemoryRequest): Promise<MemoryStoreResponse> {
    return this.request<MemoryStoreResponse>("POST", "/api/v1/agent/memory/store", {
      json: payload,
      auth: "bearer"
    });
  }

  async searchMemory(payload: MemorySearchRequest): Promise<MemorySearchResponse> {
    return this.request<MemorySearchResponse>("POST", "/api/v1/agent/memory/search", {
      json: payload,
      auth: "bearer"
    });
  }

  async memoryProfile(userId: string): Promise<MemoryProfileResponse> {
    return this.request<MemoryProfileResponse>(
      "GET",
      `/api/v1/agent/memory/profile/${encodePathSegment(userId)}`,
      { auth: "bearer" }
    );
  }

  async deleteMemory(memoryId: string, userId: string): Promise<MemoryDeleteResponse> {
    return this.request<MemoryDeleteResponse>(
      "DELETE",
      `/api/v1/agent/memory/${encodePathSegment(memoryId)}`,
      {
        params: { user_id: userId },
        auth: "bearer"
      }
    );
  }

  async purgeUserMemory(userId: string): Promise<MemoryDeleteResponse> {
    return this.request<MemoryDeleteResponse>(
      "DELETE",
      `/api/v1/agent/memory/user/${encodePathSegment(userId)}`,
      { auth: "bearer" }
    );
  }

  async listProjects(options: ListProjectsOptions = {}): Promise<Project[]> {
    const response = await this.adminRequest<{ items: Project[] }>("GET", "/projects", {
      params: {
        limit: numberParam(options.limit),
        offset: numberParam(options.offset)
      }
    });
    return response.items ?? [];
  }

  async getProject(projectId: string): Promise<Project> {
    return this.adminRequest<Project>("GET", `/projects/${encodePathSegment(projectId)}`);
  }

  async createProjectFromConfig(
    yamlContent: string,
    options: { soulContent?: string; skills?: string[] } = {}
  ): Promise<Project> {
    return this.adminRequest<Project>("POST", "/projects/from-config", {
      json: {
        yaml_content: yamlContent,
        soul_content: options.soulContent,
        skills: options.skills ?? []
      }
    });
  }

  async updateProject(projectId: string, fields: JsonObject): Promise<Project> {
    return this.adminRequest<Project>("PATCH", `/projects/${encodePathSegment(projectId)}`, {
      json: fields
    });
  }

  async integrationPrompt(projectId: string): Promise<string> {
    const response = await this.adminRequest<{ prompt: string }>(
      "GET",
      `/projects/${encodePathSegment(projectId)}/integration-prompt`
    );
    return response.prompt;
  }

  async usage(
    projectId: string,
    options: { startDate?: string; endDate?: string } = {}
  ): Promise<UsageResponse> {
    return this.adminRequest<UsageResponse>(
      "GET",
      `/projects/${encodePathSegment(projectId)}/usage`,
      {
        params: { start_date: options.startDate, end_date: options.endDate }
      }
    );
  }

  async testAgent(
    projectId: string,
    message: string,
    options: { phoneE164?: string; channel?: string } = {}
  ): Promise<AgentTestResponse> {
    return this.adminRequest<AgentTestResponse>(
      "POST",
      `/projects/${encodePathSegment(projectId)}/test`,
      {
        json: {
          message,
          phone_e164: options.phoneE164 ?? "+15550000000",
          channel: options.channel ?? "whatsapp"
        }
      }
    );
  }

  async linkUser(projectId: string, options: LinkUserOptions): Promise<JsonObject> {
    return this.integrationRequest<JsonObject>(
      "POST",
      `/${encodePathSegment(projectId)}/link-user`,
      {
        json: {
          phone_e164: options.phone,
          provider_user_id: options.appUserId,
          provider_name: options.providerName ?? "app",
          metadata: options.metadata ?? {}
        }
      }
    );
  }

  async resolveUser(projectId: string, phone: string): Promise<JsonObject> {
    return this.integrationRequest<JsonObject>(
      "GET",
      `/${encodePathSegment(projectId)}/resolve/${encodePathSegment(phone)}`
    );
  }

  async unlinkUser(projectId: string, phone: string): Promise<JsonObject> {
    return this.integrationRequest<JsonObject>(
      "DELETE",
      `/${encodePathSegment(projectId)}/unlink/${encodePathSegment(phone)}`
    );
  }

  async registerOpenAPITools(
    projectId: string,
    specUrl: string,
    options: {
      includePaths?: string[];
      excludePaths?: string[];
      targetCount?: number;
      autoDeploy?: boolean;
    } = {}
  ): Promise<JsonObject> {
    return this.adminRequest<JsonObject>(
      "POST",
      `/projects/${encodePathSegment(projectId)}/tools/from-openapi`,
      {
        json: {
          spec_url: specUrl,
          include_paths: options.includePaths ?? [],
          exclude_paths: options.excludePaths ?? [],
          target_count: options.targetCount ?? 15,
          auto_deploy: options.autoDeploy ?? true
        }
      }
    );
  }

  private async request<T>(
    method: string,
    path: string,
    options: RequestOptions = {}
  ): Promise<T> {
    let attempt = 0;
    // Transient failures (HTTP 429/5xx and connection/timeout errors) are
    // retried with exponential backoff + jitter, honoring Retry-After. Retries
    // are safe: the only non-idempotent endpoint (createResponse) always sends
    // an idempotency key.
    for (;;) {
      const timeout = requestTimeout(this.timeoutMs);
      try {
        const response = await this.sendFetch(method, path, options, timeout.signal);
        if (!response.ok) {
          if (RETRY_STATUS_CODES.has(response.status) && attempt < this.maxRetries) {
            const retryAfter = response.headers.get("Retry-After");
            // Drain the body so the connection can be reused before retrying.
            await response.text().catch(() => undefined);
            await sleep(retryDelayMs(attempt, retryAfter));
            attempt += 1;
            continue;
          }
          const errorInfo = await responseErrorInfo(response);
          throw new GeneralAugmentAPIError(response.status, errorInfo.detail, {
            error: errorInfo.error,
            requestId: errorInfo.requestId,
            rateLimit: errorInfo.rateLimit
          });
        }
        if (response.status === 204) {
          return undefined as T;
        }
        return await successJson<T>(response);
      } catch (error) {
        if (isRetriableTransportError(error, timeout.signal) && attempt < this.maxRetries) {
          await sleep(retryDelayMs(attempt, null));
          attempt += 1;
          continue;
        }
        throw requestFailureError(error);
      } finally {
        timeout.cancel();
      }
    }
  }

  private async sendFetch(
    method: string,
    path: string,
    options: RequestOptions,
    signal?: AbortSignal
  ): Promise<Response> {
    const url = new URL(`${this.baseUrl}${path}`);
    for (const [key, value] of Object.entries(options.params ?? {})) {
      if (value !== undefined) {
        url.searchParams.set(key, value);
      }
    }

    return this.fetchImpl(url, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(options.auth === "bearer"
          ? { Authorization: `Bearer ${this.apiKey}` }
          : { "X-Admin-Key": this.apiKey }),
        ...(options.headers ?? {})
      },
      body: options.json === undefined ? undefined : JSON.stringify(options.json),
      signal
    });
  }
}

export function responseOutputText(response: ResponseObject): string {
  if (typeof response.output_text === "string") {
    return response.output_text;
  }
  return responseContentParts(response)
    .filter((part) => {
      const type = part.type;
      return (type === "output_text" || type === "text") && typeof part.text === "string";
    })
    .map((part) => part.text as string)
    .join("");
}

export function responseStructuredOutput<T = unknown>(response: ResponseObject): T {
  if ("output_parsed" in response) {
    return response.output_parsed as T;
  }
  for (const part of responseContentParts(response)) {
    if ("parsed" in part) {
      return part.parsed as T;
    }
  }
  const text = responseOutputText(response).trim();
  if (!text) {
    throw new Error("Response output text is empty; no structured JSON to parse.");
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error("Response output text is not valid JSON.");
  }
}

function encodePathSegment(value: string): string {
  return encodeURIComponent(value);
}

type RequestOptions = {
  json?: JsonObject;
  params?: Record<string, string | undefined>;
  headers?: Record<string, string>;
  auth?: "admin" | "bearer";
};

type RequestTimeout = {
  signal?: AbortSignal;
  cancel: () => void;
};

function normalizeMaxRetries(value: number): number {
  if (!Number.isInteger(value) || value < 0) {
    throw new TypeError("General Augment maxRetries must be a non-negative integer.");
  }
  return value;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function retryDelayMs(attempt: number, retryAfter: string | null): number {
  const parsed = parseRetryAfter(retryAfter);
  if (parsed !== undefined) {
    return Math.min(parsed, MAX_BACKOFF_MS);
  }
  const base = Math.min(INITIAL_BACKOFF_MS * 2 ** attempt, MAX_BACKOFF_MS);
  return base / 2 + Math.random() * (base / 2);
}

// Parse a Retry-After header in delta-seconds OR HTTP-date form. The HTTP-date
// form previously produced NaN (Number(date) === NaN); this handles both.
function parseRetryAfter(value: string | null): number | undefined {
  if (value === null) {
    return undefined;
  }
  const trimmed = value.trim();
  if (trimmed === "") {
    return undefined;
  }
  if (/^\d+(\.\d+)?$/.test(trimmed)) {
    return Math.max(Number(trimmed) * 1000, 0);
  }
  const when = Date.parse(trimmed);
  if (Number.isNaN(when)) {
    return undefined;
  }
  return Math.max(when - Date.now(), 0);
}

function isRetriableTransportError(error: unknown, signal: AbortSignal | undefined): boolean {
  if (error instanceof GeneralAugmentAPIError) {
    return false;
  }
  // A deliberate client timeout aborts via `signal`; treat that as retriable
  // (a transient stall), but only if it was our timeout that fired.
  if (isAbortError(error)) {
    return signal?.aborted === true;
  }
  // fetch network failures surface as TypeError.
  return error instanceof TypeError;
}

function normalizeTimeoutMs(value: number | undefined): number | undefined {
  if (value === undefined || value === 0) {
    return undefined;
  }
  if (!Number.isFinite(value) || value < 0) {
    throw new TypeError("General Augment timeoutMs must be a positive finite number or 0.");
  }
  return value;
}

function requestTimeout(timeoutMs: number | undefined): RequestTimeout {
  if (timeoutMs === undefined) {
    return { cancel: () => undefined };
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return {
    signal: controller.signal,
    cancel: () => clearTimeout(timer)
  };
}

function requestFailureError(error: unknown): GeneralAugmentAPIError {
  if (error instanceof GeneralAugmentAPIError) {
    return error;
  }
  if (isAbortError(error)) {
    return new GeneralAugmentAPIError(0, "General Augment API request timed out.", {
      error: { reason: "request_timeout", message: "General Augment API request timed out." }
    });
  }
  return new GeneralAugmentAPIError(0, "General Augment API request failed.", {
    error: { reason: "request_failed", message: "General Augment API request failed." }
  });
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

function generateIdempotencyKey(): string {
  return crypto.randomUUID();
}

function responseHeaders(
  options: ResponseRequestOptions,
  config: { autoIdempotency?: boolean } = {}
): Record<string, string> {
  const headers: Record<string, string> = {};
  // Auto-generate an idempotency key for billable turns so retries are safe.
  // A caller-supplied key always wins.
  const idempotencyKey =
    options.idempotencyKey ?? (config.autoIdempotency ? generateIdempotencyKey() : undefined);
  if (idempotencyKey) {
    headers["X-Idempotency-Key"] = idempotencyKey;
  }
  if (options.requestId) {
    headers["X-Request-ID"] = options.requestId;
  }
  if (options.traceparent) {
    headers.traceparent = options.traceparent;
  }
  if (options.tracestate) {
    headers.tracestate = options.tracestate;
  }
  return headers;
}

type ResponseErrorInfo = {
  detail: string;
  error?: APIErrorDetail;
  requestId?: string;
  rateLimit: RateLimitInfo;
};

async function successJson<T>(response: Response): Promise<T> {
  try {
    return (await response.json()) as T;
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new GeneralAugmentAPIError(
      response.status,
      "General Augment API returned malformed JSON.",
      {
        error: {
          reason: "malformed_json",
          message: "General Augment API returned malformed JSON."
        },
        requestId: stringHeader(response.headers, "X-Request-ID")
      }
    );
  }
}

async function responseErrorInfo(response: Response): Promise<ResponseErrorInfo> {
  const text = await response.text();
  const rateLimit = rateLimitInfo(response.headers);
  const requestId = stringHeader(response.headers, "X-Request-ID");
  if (!text) {
    return { detail: response.statusText || "empty error response", requestId, rateLimit };
  }
  try {
    const payload = JSON.parse(text) as unknown;
    if (isJsonObject(payload)) {
      const error = structuredErrorDetail(payload);
      if (error !== undefined) {
        return { detail: JSON.stringify(error), error, requestId, rateLimit };
      }
      const detail = payload.detail;
      if (typeof detail === "string") {
        return { detail, requestId, rateLimit };
      }
      if (detail !== undefined) {
        return { detail: JSON.stringify(detail), requestId, rateLimit };
      }
      return { detail: JSON.stringify(payload), requestId, rateLimit };
    }
    return { detail: String(payload), requestId, rateLimit };
  } catch {
    return { detail: text, requestId, rateLimit };
  }
}

function structuredErrorDetail(payload: JsonObject): APIErrorDetail | undefined {
  if (isJsonObject(payload.detail)) {
    return mergeStructuredError(payload, payload.detail);
  }
  if (isJsonObject(payload.error)) {
    return mergeStructuredError(payload, payload.error);
  }
  if (
    typeof payload.code === "string" ||
    typeof payload.reason === "string" ||
    typeof payload.message === "string"
  ) {
    return payload as APIErrorDetail;
  }
  return undefined;
}

function mergeStructuredError(payload: JsonObject, error: JsonObject): APIErrorDetail {
  const merged: APIErrorDetail = { ...(error as APIErrorDetail) };
  for (const key of [
    "code",
    "reason",
    "request_id",
    "trace_id",
    "retry_after",
    "retry_after_seconds"
  ]) {
    if (merged[key] === undefined && payload[key] !== undefined) {
      merged[key] = payload[key];
    }
  }
  if (merged.message === undefined && typeof payload.message === "string") {
    merged.message = payload.message;
  }
  return merged;
}

function rateLimitInfo(headers: Headers): RateLimitInfo {
  return compactRateLimitInfo({
    retryAfter: numberHeader(headers, "Retry-After"),
    limit: numberHeader(headers, "X-RateLimit-Limit"),
    remaining: numberHeader(headers, "X-RateLimit-Remaining"),
    reset: numberHeader(headers, "X-RateLimit-Reset")
  });
}

function compactRateLimitInfo(info: RateLimitInfo): RateLimitInfo {
  const compact: RateLimitInfo = {};
  for (const [key, value] of Object.entries(info) as [keyof RateLimitInfo, number | undefined][]) {
    if (value !== undefined) {
      compact[key] = value;
    }
  }
  return compact;
}

function numberHeader(headers: Headers, name: string): number | undefined {
  return numberField(headers.get(name));
}

function numberParam(value: number | undefined): string | undefined {
  return value === undefined ? undefined : String(value);
}

function stringHeader(headers: Headers, name: string): string | undefined {
  return stringField(headers.get(name));
}

function numberField(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value !== "string" || value.trim() === "") {
    return undefined;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function stringField(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function responseContentParts(response: ResponseObject): JsonObject[] {
  if (!Array.isArray(response.output)) {
    return [];
  }
  const parts: JsonObject[] = [];
  for (const item of response.output) {
    if (!isJsonObject(item) || !Array.isArray(item.content)) {
      continue;
    }
    for (const part of item.content) {
      if (isJsonObject(part)) {
        parts.push(part);
      }
    }
  }
  return parts;
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function* parseSSE(
  body: ReadableStream<Uint8Array>,
  timeout: RequestTimeout
): AsyncIterable<ResponseStreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = sseBoundary(buffer);
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + sseSeparatorLength(buffer, boundary));
        const event = parseSSEBlock(block);
        if (event) {
          if (isSSEDone(event)) {
            return;
          }
          raiseForSSEError(event);
          yield event;
        }
        boundary = sseBoundary(buffer);
      }
      if (done) {
        const event = parseSSEBlock(buffer);
        if (event && !isSSEDone(event)) {
          raiseForSSEError(event);
          yield event;
        }
        return;
      }
    }
  } finally {
    // Always release the lock and cancel the stream on early break/throw/return
    // so the connection is not leaked.
    timeout.cancel();
    try {
      await reader.cancel();
    } catch {
      // ignore: stream may already be errored/closed.
    }
    reader.releaseLock();
  }
}

function isSSEDone(event: ResponseStreamEvent): boolean {
  return typeof event.data === "string" && event.data.trim() === "[DONE]";
}

function raiseForSSEError(event: ResponseStreamEvent): void {
  if (event.event !== "error") {
    return;
  }
  const data = event.data;
  const error = isJsonObject(data) ? (data as APIErrorDetail) : undefined;
  const detail =
    typeof data === "string"
      ? data
      : error
        ? JSON.stringify(error)
        : "Stream returned an error event.";
  throw new GeneralAugmentAPIError(0, detail, {
    error: error ?? { reason: "stream_error", message: detail }
  });
}

function sseBoundary(buffer: string): number {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf < 0) {
    return crlf;
  }
  if (crlf < 0) {
    return lf;
  }
  return Math.min(lf, crlf);
}

function sseSeparatorLength(buffer: string, boundary: number): number {
  return buffer.slice(boundary, boundary + 4) === "\r\n\r\n" ? 4 : 2;
}

function parseSSEBlock(block: string): ResponseStreamEvent | null {
  const normalized = block.replace(/\r\n/g, "\n");
  let event = "message";
  const dataLines: string[] = [];
  for (const line of normalized.split("\n")) {
    if (!line || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }
  if (dataLines.length === 0) {
    return null;
  }
  const data = dataLines.join("\n");
  return { event, data: parseSSEData(data) };
}

function parseSSEData(data: string): unknown {
  try {
    return JSON.parse(data) as unknown;
  } catch {
    return data;
  }
}
