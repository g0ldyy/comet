import { apiRequest } from "../../api/client";
import type {
  AuditPageData,
  SettingsMutationData,
  SettingsMutationRequest,
  SettingsSnapshotData,
} from "../../api/generated/contracts";

export function getSettings(): Promise<SettingsSnapshotData> {
  return apiRequest<SettingsSnapshotData>("/api/v1/admin/settings", {
    scope: "admin",
  });
}

export function saveSettings(body: SettingsMutationRequest): Promise<SettingsMutationData> {
  return apiRequest<SettingsMutationData>("/api/v1/admin/settings", {
    body: JSON.stringify(body),
    method: "PUT",
    scope: "admin",
  });
}

export function getSettingsAudit(): Promise<AuditPageData> {
  return apiRequest<AuditPageData>("/api/v1/admin/settings/audit?limit=50", {
    scope: "admin",
  });
}
