import { apiRequest } from "../../api/client";
import type {
  MaintenanceResultData,
  RuntimeRestartData,
  SystemDetailsData,
  SystemSnapshotData,
  UpdateCheckData,
} from "../../api/generated/contracts";

export function getSystemSnapshot(): Promise<SystemSnapshotData> {
  return apiRequest<SystemSnapshotData>("/api/v1/admin/system/snapshot", {
    scope: "admin",
  });
}

export function getSystemDetails(): Promise<SystemDetailsData> {
  return apiRequest<SystemDetailsData>("/api/v1/admin/system/details", {
    scope: "admin",
  });
}

export function checkForUpdates(): Promise<UpdateCheckData> {
  return apiRequest<UpdateCheckData>("/api/v1/admin/system/update-check", {
    method: "POST",
    scope: "admin",
  });
}

export function runRetention(): Promise<MaintenanceResultData> {
  return apiRequest<MaintenanceResultData>("/api/v1/admin/system/maintenance/retention", {
    method: "POST",
    scope: "admin",
  });
}

export function restartRuntime(instanceId: string): Promise<RuntimeRestartData> {
  return apiRequest<RuntimeRestartData>(
    `/api/v1/admin/system/runtimes/${encodeURIComponent(instanceId)}/restart`,
    {
      method: "POST",
      scope: "admin",
    },
  );
}
