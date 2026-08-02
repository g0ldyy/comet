import { apiRequest } from "../../api/client";
import type {
  ApiV1Paths,
  CurrentMetricsData,
  DatabaseMetricsSnapshot,
  MetricRangeData,
} from "../../api/generated/contracts";
import { getSystemSnapshot } from "../system/api";

type RangeParameters = ApiV1Paths["/api/v1/admin/metrics/range/{metric}"]["get"]["parameters"];

export type MetricName = RangeParameters["path"]["metric"];
export type MetricRange = NonNullable<RangeParameters["query"]["range"]>;

export function getCurrentMetrics(): Promise<CurrentMetricsData> {
  return apiRequest<CurrentMetricsData>("/api/v1/admin/metrics/current", {
    scope: "admin",
  });
}

export function getDatabaseMetrics(): Promise<DatabaseMetricsSnapshot> {
  return apiRequest<DatabaseMetricsSnapshot>("/api/v1/admin/metrics/database", {
    scope: "admin",
  });
}

export function getMetricRange(metric: MetricName, range: MetricRange): Promise<MetricRangeData> {
  return apiRequest<MetricRangeData>(`/api/v1/admin/metrics/range/${metric}?range=${range}`, {
    scope: "admin",
  });
}

export { getSystemSnapshot };
