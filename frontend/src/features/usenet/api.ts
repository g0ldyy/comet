import { apiRequest } from "../../api/client";
import type {
  CommandResultData,
  StreamActivityData,
  UsenetArtifactPageData,
  UsenetArtifactPruneData,
  UsenetControlData,
  UsenetHistoryPageData,
  UsenetSnapshotData,
} from "../../api/generated/contracts";

export function getUsenetSnapshot(): Promise<UsenetSnapshotData> {
  return apiRequest<UsenetSnapshotData>("/api/v1/admin/usenet/snapshot", {
    scope: "admin",
  });
}

export function getUsenetHistory(): Promise<UsenetHistoryPageData> {
  return apiRequest<UsenetHistoryPageData>("/api/v1/admin/usenet/history?limit=30", {
    scope: "admin",
  });
}

export function getUsenetActivity(
  range: StreamActivityData["selection"],
): Promise<StreamActivityData> {
  return apiRequest<StreamActivityData>(`/api/v1/admin/usenet/activity?range=${range}`, {
    scope: "admin",
  });
}

export function getUsenetArtifacts(): Promise<UsenetArtifactPageData> {
  return apiRequest<UsenetArtifactPageData>("/api/v1/admin/usenet/artifacts?limit=50", {
    scope: "admin",
  });
}

export function cancelUsenetOperation(operationId: string): Promise<CommandResultData> {
  return apiRequest<CommandResultData>(`/api/v1/admin/usenet/operations/${operationId}/cancel`, {
    method: "POST",
    scope: "admin",
  });
}

export function controlUsenetRuntime(
  instanceId: string,
  processId: number,
  action: "drain" | "resume",
): Promise<UsenetControlData> {
  return apiRequest<UsenetControlData>(
    `/api/v1/admin/usenet/runtimes/${instanceId}/${processId}/${action}`,
    {
      method: "POST",
      scope: "admin",
    },
  );
}

export function pruneUsenetArtifact(artifactId: string): Promise<UsenetArtifactPruneData> {
  return apiRequest<UsenetArtifactPruneData>(`/api/v1/admin/usenet/artifacts/${artifactId}/prune`, {
    method: "POST",
    scope: "admin",
  });
}
