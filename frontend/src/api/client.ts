type RequestScope = "admin" | "configure" | "public";

interface SuccessEnvelope<T> {
  data: T;
  meta: {
    request_id: string;
  };
}

interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    request_id: string;
    details?: ReadonlyArray<Record<string, unknown>> | null;
  };
}

const csrfTokens: Record<Exclude<RequestScope, "public">, string | null> = {
  admin: null,
  configure: null,
};

export class ApiClientError extends Error {
  readonly code: string;
  readonly status: number;
  readonly requestId: string | null;
  readonly details: ReadonlyArray<Record<string, unknown>> | null;

  constructor(response: Response, payload: ErrorEnvelope | null) {
    super(payload?.error.message ?? "The request could not be completed.");
    this.name = "ApiClientError";
    this.code = payload?.error.code ?? "request_failed";
    this.status = response.status;
    this.requestId = payload?.error.request_id ?? response.headers.get("x-request-id");
    this.details = payload?.error.details ?? null;
  }
}

export function setCsrfToken(scope: Exclude<RequestScope, "public">, token: string | null) {
  csrfTokens[scope] = token;
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = value.error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof error.code === "string" &&
    "message" in error &&
    typeof error.message === "string" &&
    "request_id" in error &&
    typeof error.request_id === "string"
  );
}

function isSuccessEnvelope<T>(value: unknown): value is SuccessEnvelope<T> {
  return typeof value === "object" && value !== null && "data" in value && "meta" in value;
}

async function requestJson(
  path: string,
  options: RequestInit & { scope?: RequestScope } = {},
): Promise<{ payload: unknown; response: Response; scope: RequestScope }> {
  const scope = options.scope ?? "public";
  const method = options.method?.toUpperCase() ?? "GET";
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (method !== "GET" && method !== "HEAD" && scope !== "public") {
    const token = csrfTokens[scope];
    if (token !== null) {
      headers.set("X-CSRF-Token", token);
    }
  }

  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers,
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiClientError(response, null);
  }
  return { payload, response, scope };
}

export async function apiRequest<T>(
  path: `/api/v1/${string}`,
  options: RequestInit & { scope?: RequestScope } = {},
): Promise<T> {
  const { payload, response, scope } = await requestJson(path, options);
  if (!response.ok) {
    if (response.status === 401 && scope !== "public") {
      setCsrfToken(scope, null);
    }
    throw new ApiClientError(response, isErrorEnvelope(payload) ? payload : null);
  }
  if (!isSuccessEnvelope<T>(payload)) {
    throw new ApiClientError(response, null);
  }
  return payload.data;
}

export async function rawJsonRequest(
  path: `/${string}`,
  options: RequestInit & { scope?: RequestScope } = {},
): Promise<{ ok: boolean; payload: unknown; status: number }> {
  const { payload, response } = await requestJson(path, options);
  return { ok: response.ok, payload, status: response.status };
}
