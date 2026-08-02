import { apiRequest, setCsrfToken } from "../../api/client";
import type { ConfigureSessionData, SessionData } from "../../api/generated/contracts";

export async function getAdminSession(): Promise<SessionData> {
  const session = await apiRequest<SessionData>("/api/v1/auth/session", {
    scope: "admin",
  });
  setCsrfToken("admin", session.csrf_token);
  return session;
}

export async function loginAdmin(password: string): Promise<SessionData> {
  const session = await apiRequest<SessionData>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ password }),
    scope: "public",
  });
  setCsrfToken("admin", session.csrf_token);
  return session;
}

export async function logoutAdmin(): Promise<void> {
  await apiRequest("/api/v1/auth/logout", {
    method: "POST",
    scope: "admin",
  });
  setCsrfToken("admin", null);
}

export async function getConfigureSession(): Promise<ConfigureSessionData> {
  const session = await apiRequest<ConfigureSessionData>("/api/v1/auth/configure/session", {
    scope: "configure",
  });
  setCsrfToken("configure", session.csrf_token);
  return session;
}

export async function loginConfigure(password: string): Promise<ConfigureSessionData> {
  const session = await apiRequest<ConfigureSessionData>("/api/v1/auth/configure/login", {
    method: "POST",
    body: JSON.stringify({ password }),
    scope: "public",
  });
  setCsrfToken("configure", session.csrf_token);
  return session;
}
