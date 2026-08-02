import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, apiRequest, setCsrfToken } from "./client";

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json", "X-Request-ID": "header-request" },
    status,
  });

describe("apiRequest", () => {
  beforeEach(() => {
    setCsrfToken("admin", null);
    setCsrfToken("configure", null);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns data from a valid API envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          data: { revision: 4 },
          meta: { request_id: "request-4" },
        }),
      ),
    );

    await expect(apiRequest("/api/v1/admin/settings")).resolves.toEqual({ revision: 4 });
  });

  it("sends session credentials and the current CSRF token for mutations", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response({
        data: { logged_out: true },
        meta: { request_id: "request-5" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("admin", "csrf-token");

    await apiRequest("/api/v1/auth/logout", { method: "POST", scope: "admin" });

    const calls = fetchMock.mock.calls as [[string, RequestInit]];
    const options = calls[0][1];
    const headers = options.headers as Headers;
    expect(options.credentials).toBe("same-origin");
    expect(headers.get("X-CSRF-Token")).toBe("csrf-token");
  });

  it("normalizes API errors and preserves their request ID", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response(
          {
            error: {
              code: "authentication_required",
              message: "Authentication required",
              request_id: "request-6",
            },
          },
          401,
        ),
      ),
    );

    const request = apiRequest("/api/v1/auth/session", { scope: "admin" });
    await expect(request).rejects.toMatchObject({
      code: "authentication_required",
      requestId: "request-6",
      status: 401,
    });
  });

  it("rejects malformed responses at the HTTP boundary", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>bad gateway</html>")));

    await expect(apiRequest("/api/v1/admin/settings")).rejects.toBeInstanceOf(ApiClientError);
  });
});
