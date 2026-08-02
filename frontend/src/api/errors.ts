import { ApiClientError } from "./client";

function detailMessage(detail: Readonly<Record<string, unknown>>): string | null {
  return typeof detail.message === "string" && detail.message !== "" ? detail.message : null;
}

export function apiErrorReason(error: unknown): string | null {
  if (!(error instanceof ApiClientError)) return null;
  for (const detail of error.details ?? []) {
    const message = detailMessage(detail);
    if (message !== null) return message;
  }
  return error.message;
}

export function apiErrorSummary(
  error: unknown,
  fallback: string,
  requestId: (id: string) => string,
): string {
  if (!(error instanceof ApiClientError)) return fallback;
  const summary = `${apiErrorReason(error) ?? fallback} · ${error.code}`;
  return error.requestId ? `${summary} · ${requestId(error.requestId)}` : summary;
}

export function apiValidationErrors(
  error: unknown,
  fallback: string,
): Readonly<Record<string, string>> {
  if (!(error instanceof ApiClientError)) return {};
  const errors: Record<string, string> = {};
  for (const detail of error.details ?? []) {
    const location = detail.location;
    const field = Array.isArray(location) ? location.at(-1) : undefined;
    if (typeof field === "string") errors[field] = detailMessage(detail) ?? fallback;
  }
  return errors;
}
