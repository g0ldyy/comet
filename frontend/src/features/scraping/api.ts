import { apiRequest } from "../../api/client";
import type {
  ScraperControlData,
  ScraperQueueMutationData,
  ScraperQueuePageData,
  ScraperRunsPageData,
  ScrapingSnapshotData,
} from "../../api/generated/contracts";

export type QueueKind = "item" | "episode";
export type QueueAction = "retry" | "defer" | "abandon";
export type ScraperControl = "start" | "stop" | "pause" | "resume" | "drain" | "cancel_drain";

export function getScrapingSnapshot(): Promise<ScrapingSnapshotData> {
  return apiRequest<ScrapingSnapshotData>("/api/v1/admin/scraping/snapshot", {
    scope: "admin",
  });
}

export function getScraperQueue(
  kind: QueueKind,
  status: string,
  search: string,
): Promise<ScraperQueuePageData> {
  const query = new URLSearchParams({ limit: "50" });
  if (status) query.set("status", status);
  if (search.trim()) query.set("search", search.trim());
  return apiRequest<ScraperQueuePageData>(`/api/v1/admin/scraping/queue/${kind}?${query}`, {
    scope: "admin",
  });
}

export function getScraperRuns(): Promise<ScraperRunsPageData> {
  return apiRequest<ScraperRunsPageData>("/api/v1/admin/scraping/runs?limit=30", {
    scope: "admin",
  });
}

export function mutateQueue(
  kind: QueueKind,
  resourceId: string,
  action: QueueAction,
): Promise<ScraperQueueMutationData> {
  return apiRequest<ScraperQueueMutationData>(
    `/api/v1/admin/scraping/queue/${kind}/${encodeURIComponent(resourceId)}/${action}`,
    { method: "POST", scope: "admin" },
  );
}

export function requeueDead(): Promise<ScraperQueueMutationData> {
  return apiRequest<ScraperQueueMutationData>("/api/v1/admin/scraping/queue/requeue-dead", {
    method: "POST",
    scope: "admin",
  });
}

export function controlScraper(action: ScraperControl): Promise<ScraperControlData> {
  return apiRequest<ScraperControlData>(`/api/v1/admin/scraping/control/${action}`, {
    method: "POST",
    scope: "admin",
  });
}
