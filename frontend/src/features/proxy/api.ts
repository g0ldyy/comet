import { apiRequest } from "../../api/client";
import type {
  CommandResultData,
  ProxyHistoryPageData,
  ProxySnapshotData,
  StreamActivityData,
} from "../../api/generated/contracts";

export function getProxySnapshot(): Promise<ProxySnapshotData> {
  return apiRequest<ProxySnapshotData>("/api/v1/admin/proxy/snapshot", {
    scope: "admin",
  });
}

export function getProxyHistory(): Promise<ProxyHistoryPageData> {
  return apiRequest<ProxyHistoryPageData>("/api/v1/admin/proxy/history?limit=30", {
    scope: "admin",
  });
}

export function getProxyActivity(
  range: StreamActivityData["selection"],
): Promise<StreamActivityData> {
  return apiRequest<StreamActivityData>(`/api/v1/admin/proxy/activity?range=${range}`, {
    scope: "admin",
  });
}

export function cancelProxyConnection(connectionId: string): Promise<CommandResultData> {
  return apiRequest<CommandResultData>(`/api/v1/admin/proxy/connections/${connectionId}/cancel`, {
    method: "POST",
    scope: "admin",
  });
}
