import { z } from "zod";
import { apiRequest } from "../../api/client";
import type { OperationalEventData, OperationalEventPageData } from "../../api/generated/contracts";

export interface EventFilters {
  search: string;
  category: string;
  connectionId: string;
  instanceId: string;
  level: string;
  mediaType: string;
  outcome: string;
  providerName: string;
  requestId: string;
  role: string;
  runId: string;
  startedAt: string;
  endedAt: string;
}

export const emptyEventFilters: EventFilters = {
  search: "",
  category: "",
  connectionId: "",
  instanceId: "",
  level: "",
  mediaType: "",
  outcome: "",
  providerName: "",
  requestId: "",
  role: "",
  runId: "",
  startedAt: "",
  endedAt: "",
};

const eventData = z.object({
  category: z.string(),
  connection_id: z.string().nullable(),
  created_at: z.number(),
  details: z.record(z.string(), z.union([z.string(), z.boolean(), z.number()])),
  error_code: z.string().nullable(),
  event: z.string(),
  id: z.number().int().positive(),
  instance_id: z.string(),
  level: z.enum(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
  media_type: z.string().nullable(),
  message: z.string(),
  outcome: z.string().nullable(),
  process_id: z.number().int().positive(),
  provider_name: z.string().nullable(),
  request_id: z.string().nullable(),
  role: z.string(),
  run_id: z.string().nullable(),
});

export function parseLiveEvent(value: string): OperationalEventData {
  return eventData.parse(JSON.parse(value));
}

export function eventQuery(filters: EventFilters): string {
  const parameters = new URLSearchParams();
  const values = {
    search: filters.search,
    category: filters.category,
    connection_id: filters.connectionId,
    instance_id: filters.instanceId,
    level: filters.level,
    media_type: filters.mediaType,
    outcome: filters.outcome,
    provider_name: filters.providerName,
    request_id: filters.requestId,
    role: filters.role,
    run_id: filters.runId,
  };
  for (const [key, value] of Object.entries(values)) {
    if (value) parameters.set(key, value);
  }
  if (filters.startedAt) {
    parameters.set("started_at", String(Date.parse(filters.startedAt) / 1000));
  }
  if (filters.endedAt) {
    parameters.set("ended_at", String(Date.parse(filters.endedAt) / 1000));
  }
  return parameters.toString();
}

export function getEvents(filterQuery: string, cursor?: number): Promise<OperationalEventPageData> {
  const query = new URLSearchParams(filterQuery);
  if (cursor !== undefined) query.set("cursor", String(cursor));
  const parameters = query.toString();
  return apiRequest<OperationalEventPageData>(
    `/api/v1/admin/logs${parameters ? `?${parameters}` : ""}`,
    { scope: "admin" },
  );
}

export function eventStreamUrl(filterQuery: string, cursor: number): string {
  const query = new URLSearchParams(filterQuery);
  query.set("cursor", String(cursor));
  return `/api/v1/admin/logs/stream?${query}`;
}

export function logExportUrl(filters: EventFilters, format: "jsonl" | "text"): string {
  const query = new URLSearchParams(eventQuery(filters));
  query.set("format", format);
  return `/api/v1/admin/logs/export?${query}`;
}
