import { useQuery } from "@tanstack/react-query";
import { ChartNoAxesCombined } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  type XAxisTickContentProps,
  YAxis,
} from "recharts";
import type { DistributionMetric, MetricSampleData } from "../../api/generated/contracts";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Skeleton } from "../../components/ui/Skeleton";
import {
  getDatabaseMetrics,
  getMetricRange,
  type MetricName,
  type MetricRange,
} from "../metrics/api";
import { MetricCard } from "../metrics/MetricCard";
import {
  counterRate,
  counterRateWhere,
  formatMetric,
  histogramQuantile,
  type MetricFrame,
  ratioRate,
  sampleTotal,
} from "../metrics/model";
import { useLiveMetrics } from "../metrics/useLiveMetrics";

type MetricStyle = Parameters<typeof formatMetric>[1];

interface CatalogMetric {
  group:
    | "background"
    | "cache"
    | "database"
    | "debrid"
    | "http"
    | "proxy"
    | "scraping"
    | "streams"
    | "usenet";
  name: MetricName;
  style: MetricStyle;
}

const catalog: ReadonlyArray<CatalogMetric> = [
  { group: "background", name: "background_oldest", style: "seconds" },
  { group: "background", name: "background_queue", style: "number" },
  { group: "background", name: "background_runs", style: "rate" },
  { group: "background", name: "background_torrents", style: "rate" },
  { group: "http", name: "http_requests", style: "rate" },
  { group: "http", name: "http_error_ratio", style: "percent" },
  { group: "http", name: "http_p95", style: "seconds" },
  { group: "http", name: "http_response_size_p95", style: "bytes" },
  { group: "http", name: "http_in_flight", style: "number" },
  { group: "streams", name: "stream_requests", style: "rate" },
  { group: "streams", name: "stream_results", style: "number" },
  { group: "streams", name: "search_rejections", style: "rate" },
  { group: "cache", name: "cache_hit_ratio", style: "percent" },
  { group: "cache", name: "cache_results", style: "number" },
  { group: "scraping", name: "scraper_requests", style: "rate" },
  { group: "scraping", name: "scraper_p95", style: "seconds" },
  { group: "scraping", name: "scraper_results", style: "rate" },
  { group: "debrid", name: "debrid_requests", style: "rate" },
  { group: "debrid", name: "debrid_p95", style: "seconds" },
  { group: "debrid", name: "debrid_results", style: "rate" },
  { group: "database", name: "database_errors", style: "rate" },
  { group: "database", name: "database_p95", style: "seconds" },
  { group: "database", name: "replica_fallbacks", style: "rate" },
  { group: "proxy", name: "proxy_active", style: "number" },
  { group: "proxy", name: "proxy_bytes", style: "bytesRate" },
  { group: "proxy", name: "proxy_connections", style: "rate" },
  { group: "proxy", name: "proxy_p95", style: "seconds" },
  { group: "usenet", name: "usenet_engine_up", style: "number" },
  { group: "usenet", name: "usenet_nntp_bytes", style: "bytesRate" },
  { group: "usenet", name: "usenet_snapshot_age", style: "seconds" },
];

const ranges: ReadonlyArray<MetricRange> = ["15m", "1h", "6h", "24h", "7d", "30d"];
const colors = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

function labelMatch(sample: MetricSampleData, label: string, value: string): boolean {
  return sample.labels[label] === value;
}

function metricValue(name: MetricName, frames: ReadonlyArray<MetricFrame>): number | null {
  const latest = frames.at(-1)?.samples ?? [];
  switch (name) {
    case "background_oldest":
      return sampleTotal(latest, "comet_background_scraper_oldest_queue_item_age_seconds");
    case "background_queue":
      return sampleTotal(latest, "comet_background_scraper_queue_items");
    case "background_runs":
      return counterRate(frames, "comet_background_scraper_runs_total");
    case "background_torrents":
      return counterRate(frames, "comet_background_scraper_torrents_total");
    case "cache_hit_ratio":
      return ratioRate(
        frames,
        (samples) =>
          samples
            .filter(
              (sample) =>
                sample.name === "comet_torrent_cache_lookups_total" &&
                labelMatch(sample, "result", "hit"),
            )
            .reduce((total, sample) => total + sample.value, 0),
        (samples) => sampleTotal(samples, "comet_torrent_cache_lookups_total"),
      );
    case "cache_results":
      return histogramQuantile(frames, "comet_torrent_cache_results", 0.95);
    case "database_errors":
      return counterRateWhere(
        frames,
        "comet_database_operations_total",
        (sample) => sample.labels.outcome === "error",
      );
    case "database_p95":
      return histogramQuantile(frames, "comet_database_operation_duration_seconds", 0.95);
    case "debrid_p95":
      return histogramQuantile(frames, "comet_debrid_request_duration_seconds", 0.95);
    case "debrid_requests":
      return counterRate(frames, "comet_debrid_requests_total");
    case "debrid_results":
      return counterRate(frames, "comet_debrid_results_total");
    case "http_error_ratio":
      return ratioRate(
        frames,
        (samples) =>
          samples
            .filter(
              (sample) =>
                sample.name === "comet_http_requests_total" &&
                sample.labels.status?.startsWith("5") === true,
            )
            .reduce((total, sample) => total + sample.value, 0),
        (samples) => sampleTotal(samples, "comet_http_requests_total"),
      );
    case "http_in_flight":
      return sampleTotal(latest, "comet_http_requests_in_progress");
    case "http_p95":
      return histogramQuantile(frames, "comet_http_request_duration_seconds", 0.95);
    case "http_requests":
      return counterRate(frames, "comet_http_requests_total");
    case "http_response_size_p95":
      return histogramQuantile(frames, "comet_http_response_size_bytes", 0.95);
    case "proxy_active":
      return sampleTotal(latest, "comet_proxy_stream_active_connections");
    case "proxy_bytes":
      return counterRate(frames, "comet_proxy_stream_bytes_total");
    case "proxy_connections":
      return counterRate(frames, "comet_proxy_stream_connections_total");
    case "proxy_p95":
      return histogramQuantile(frames, "comet_proxy_stream_duration_seconds", 0.95);
    case "replica_fallbacks":
      return counterRate(frames, "comet_database_replica_fallbacks_total");
    case "scraper_p95":
      return histogramQuantile(frames, "comet_scraper_request_duration_seconds", 0.95);
    case "scraper_requests":
      return counterRate(frames, "comet_scraper_requests_total");
    case "scraper_results":
      return counterRate(frames, "comet_scraper_torrents_total");
    case "search_rejections":
      return counterRate(frames, "comet_search_rejections_total");
    case "stream_requests":
      return counterRate(frames, "comet_stream_requests_total");
    case "stream_results":
      return histogramQuantile(frames, "comet_stream_results", 0.95);
    case "usenet_engine_up":
      return sampleTotal(latest, "comet_usenet_engine_up");
    case "usenet_nntp_bytes":
      return counterRateWhere(
        frames,
        "comet_usenet_engine_stat",
        (sample) => sample.labels.stat?.includes("bytes") === true,
      );
    case "usenet_snapshot_age": {
      const timestamp = sampleTotal(latest, "comet_usenet_engine_last_snapshot_timestamp_seconds");
      return timestamp > 0 ? Math.max(0, Date.now() / 1_000 - timestamp) : null;
    }
  }
}

export function liveChartData(frames: ReadonlyArray<MetricFrame>, metric: MetricName) {
  const data: Array<{ at: string; value: number | null }> = [];
  let previous: MetricFrame | undefined;
  for (const frame of frames) {
    data.push({
      at: new Date(frame.at * 1_000).toLocaleTimeString(),
      value: metricValue(metric, previous === undefined ? [frame] : [previous, frame]),
    });
    previous = frame;
  }
  const firstMeasuredPoint = data.findIndex(({ value }) => value !== null);
  return firstMeasuredPoint === -1 ? [] : data.slice(firstMeasuredPoint);
}

function seriesLabel(labels: Readonly<Record<string, string>>, fallback: string): string {
  const values = Object.values(labels);
  return values.length > 0 ? values.join(" · ") : fallback;
}

function chartColor(index: number): string {
  return colors[index % colors.length] as string;
}

function MetricChartTooltip({
  active,
  label,
  payload,
  style,
}: TooltipContentProps & { style: MetricStyle }) {
  if (!active || payload.length === 0) return null;
  return (
    <div className="metric-chart__tooltip">
      <span>{label}</span>
      {payload.map((item) => (
        <div key={String(item.dataKey)}>
          <i style={{ background: item.color }} />
          <span>{item.name}</span>
          <strong>
            {typeof item.value === "number" ? formatMetric(item.value, style) : String(item.value)}
          </strong>
        </div>
      ))}
    </div>
  );
}

function TimeAxisTick({ fill, index, payload, visibleTicksCount, x, y }: XAxisTickContentProps) {
  const textAnchor = index === 0 ? "start" : index === visibleTicksCount - 1 ? "end" : "middle";

  return (
    <text
      className="metric-chart__axis-tick"
      dy="0.9em"
      fill={fill}
      textAnchor={textAnchor}
      x={x}
      y={y}
    >
      {String(payload.value)}
    </text>
  );
}

function DistributionDonut({ items }: { items: ReadonlyArray<DistributionMetric> }) {
  const { t } = useTranslation();
  const total = items.reduce((sum, item) => sum + item.count, 0);
  if (items.length === 0) return <p className="empty-state">{t("analytics.waiting")}</p>;
  return (
    <div className="distribution-donut">
      <div aria-hidden="true">
        <ResponsiveContainer height="100%" width="100%">
          <PieChart accessibilityLayer={false}>
            <Pie
              data={items}
              dataKey="count"
              innerRadius="68%"
              nameKey="label"
              outerRadius="94%"
              paddingAngle={2}
              stroke="none"
            >
              {items.map((item, index) => (
                <Cell fill={chartColor(index)} key={item.label} />
              ))}
            </Pie>
          </PieChart>
        </ResponsiveContainer>
        <strong>{formatMetric(total, "number")}</strong>
      </div>
      <ul>
        {items.map((item, index) => (
          <li key={item.label}>
            <i style={{ background: chartColor(index) }} />
            <span>{item.label}</span>
            <strong>{formatMetric(item.count, "number")}</strong>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DistributionChart({ items }: { items: ReadonlyArray<DistributionMetric> }) {
  const { t } = useTranslation();
  if (items.length === 0) return <p className="empty-state">{t("analytics.waiting")}</p>;
  return (
    <div aria-hidden="true" className="distribution-chart">
      <ResponsiveContainer height="100%" width="100%">
        <BarChart
          accessibilityLayer={false}
          data={items}
          layout="vertical"
          margin={{ bottom: 0, left: 4, right: 16, top: 0 }}
        >
          <CartesianGrid horizontal={false} stroke="var(--color-border)" strokeDasharray="3 3" />
          <XAxis axisLine={false} stroke="var(--color-text-subtle)" type="number" />
          <YAxis
            axisLine={false}
            dataKey="label"
            stroke="var(--color-text-muted)"
            type="category"
            width={92}
          />
          <Bar dataKey="count" fill="var(--chart-1)" radius={[0, 5, 5, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function AnalyticsPage() {
  const { t } = useTranslation();
  const live = useLiveMetrics();
  const databaseMetrics = useQuery({
    queryFn: getDatabaseMetrics,
    queryKey: ["admin", "metrics", "database"],
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const [metric, setMetric] = useState<MetricName>("http_requests");
  const [range, setRange] = useState<"live" | MetricRange>("live");
  const selected = catalog.find((item) => item.name === metric) as CatalogMetric;
  const [activeGroup, setActiveGroup] = useState<CatalogMetric["group"]>(selected.group);
  const historical = useQuery({
    enabled: range !== "live" && live.data?.history_available === true,
    queryFn: () => getMetricRange(metric, range as MetricRange),
    queryKey: ["admin", "metrics", "range", metric, range],
  });
  const chart = useMemo(() => {
    if (range === "live") {
      return {
        data: liveChartData(live.frames, metric),
        lines: [{ dataKey: "value", label: t(`analytics.metrics.${metric}`) }],
      };
    }
    const items = historical.data?.series ?? [];
    const points = new Map<number, Record<string, number | string | null>>();
    items.forEach((series, seriesIndex) => {
      const dataKey = `series-${seriesIndex}`;
      for (const point of series.points) {
        const row = points.get(point.timestamp) ?? {
          at: new Date(point.timestamp * 1_000).toLocaleString(),
        };
        row[dataKey] = point.value;
        points.set(point.timestamp, row);
      }
    });
    return {
      data: [...points.values()],
      lines: items.map((series, index) => ({
        dataKey: `series-${index}`,
        label: seriesLabel(series.labels, t("analytics.series", { count: index + 1 })),
      })),
    };
  }, [historical.data, live.frames, metric, range, t]);
  const groups = [...new Set(catalog.map(({ group }) => group))];

  return (
    <section aria-labelledby="analytics-title" className="section-page dashboard-page">
      <header className="section-page__header">
        <div>
          <h1 id="analytics-title">{t("nav.analytics")}</h1>
          <p>{t("analytics.overviewDescription")}</p>
        </div>
      </header>

      {live.isPending ? <Skeleton label={t("analytics.loading")} lines={8} /> : null}
      {live.isError ? (
        <Alert title={t("analytics.errorTitle")} tone="danger">
          <ApiErrorDetails error={live.error} fallback={t("analytics.errorDescription")} />
        </Alert>
      ) : null}
      {live.data ? (
        <>
          <article className="dashboard-panel analytics-chart">
            <header>
              <div>
                <span className="eyebrow">
                  <ChartNoAxesCombined aria-hidden="true" size={15} />
                  {range === "live" ? t("analytics.live") : range}
                </span>
                <h2>{t(`analytics.metrics.${metric}`)}</h2>
              </div>
              <fieldset
                className="chart-range"
                title={!live.data.history_available ? t("analytics.historyUnavailable") : undefined}
              >
                <legend className="visually-hidden">{t("analytics.range")}</legend>
                {["live", ...ranges].map((item) => (
                  <button
                    className={
                      range === item
                        ? "chart-range__option chart-range__option--active"
                        : "chart-range__option"
                    }
                    aria-disabled={item !== "live" && !live.data.history_available}
                    key={item}
                    onClick={() => {
                      if (item === "live" || live.data.history_available) {
                        setRange(item as "live" | MetricRange);
                      }
                    }}
                    type="button"
                  >
                    {item === "live" ? t("analytics.live") : item}
                  </button>
                ))}
              </fieldset>
            </header>
            <div className="metric-browser">
              <nav aria-label={t("analytics.metric")} className="metric-browser__groups">
                {groups.map((group) => (
                  <button
                    aria-pressed={activeGroup === group}
                    className={
                      activeGroup === group
                        ? "metric-browser__group metric-browser__group--active"
                        : "metric-browser__group"
                    }
                    key={group}
                    onClick={() => {
                      setActiveGroup(group);
                      const first = catalog.find((item) => item.group === group);
                      if (first) setMetric(first.name);
                    }}
                    type="button"
                  >
                    {t(`analytics.groups.${group}`)}
                  </button>
                ))}
              </nav>
              <div className="metric-browser__metrics">
                {catalog
                  .filter((item) => item.group === activeGroup)
                  .map((item) => (
                    <button
                      className={
                        metric === item.name
                          ? "metric-browser__metric metric-browser__metric--active"
                          : "metric-browser__metric"
                      }
                      key={item.name}
                      onClick={() => setMetric(item.name)}
                      type="button"
                    >
                      <span>{t(`analytics.metrics.${item.name}`)}</span>
                      <strong>
                        {formatMetric(metricValue(item.name, live.frames.slice(-13)), item.style)}
                      </strong>
                    </button>
                  ))}
              </div>
            </div>
            {historical.isError ? (
              <Alert title={t("analytics.historyError")} tone="warning">
                <ApiErrorDetails
                  error={historical.error}
                  fallback={t("analytics.historyErrorDescription")}
                />
              </Alert>
            ) : null}
            <div
              aria-label={t(`analytics.metrics.${metric}`)}
              className="metric-chart"
              onPointerDown={(event) => event.preventDefault()}
              role="img"
            >
              {historical.isPending && range !== "live" ? (
                <Skeleton label={t("analytics.loadingHistory")} lines={5} />
              ) : chart.data.length === 0 ? (
                <p className="empty-state">{t("analytics.waiting")}</p>
              ) : (
                <ResponsiveContainer height="100%" width="100%">
                  <AreaChart
                    accessibilityLayer={false}
                    data={chart.data}
                    margin={{ bottom: 4, left: 8, right: 12, top: 4 }}
                  >
                    <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" />
                    <XAxis
                      dataKey="at"
                      minTickGap={36}
                      stroke="var(--color-text-subtle)"
                      tick={TimeAxisTick}
                      tickMargin={10}
                    />
                    <YAxis
                      stroke="var(--color-text-subtle)"
                      tickMargin={8}
                      tickFormatter={(value: number) => formatMetric(value, selected.style)}
                      width={78}
                    />
                    <Legend />
                    <Tooltip
                      content={(props) => <MetricChartTooltip {...props} style={selected.style} />}
                      cursor={false}
                      isAnimationActive={false}
                      wrapperStyle={{ transition: "none" }}
                    />
                    {chart.lines.map((line, index) => (
                      <Area
                        activeDot={{
                          fill: "var(--color-text)",
                          r: 4,
                          stroke: chartColor(index),
                          strokeWidth: 2,
                        }}
                        connectNulls
                        dataKey={line.dataKey}
                        dot={
                          chart.data.length === 1
                            ? {
                                fill: chartColor(index),
                                r: 3,
                                stroke: "var(--color-surface-1)",
                                strokeWidth: 2,
                              }
                            : false
                        }
                        fill={chartColor(index)}
                        fillOpacity={index === 0 ? 0.16 : 0.05}
                        animationDuration={500}
                        animationEasing="ease-out"
                        isAnimationActive
                        key={line.dataKey}
                        name={line.label}
                        stroke={chartColor(index)}
                        strokeWidth={2}
                        type="monotone"
                      />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </article>

          {databaseMetrics.data ? (
            <section className="inventory-section">
              <div>
                <h2>{t("analytics.inventory.title")}</h2>
                <p>{t("analytics.inventory.description")}</p>
              </div>
              <div className="metric-grid metric-grid--hero">
                <MetricCard
                  label={t("analytics.inventory.torrents")}
                  tone="live"
                  value={formatMetric(databaseMetrics.data.torrents.total, "number")}
                />
                <MetricCard
                  label={t("analytics.inventory.uniqueMedia")}
                  value={formatMetric(databaseMetrics.data.torrents.summary.unique_media, "number")}
                />
                <MetricCard
                  detail={t("analytics.inventory.last24h")}
                  label={t("analytics.inventory.recentTorrents")}
                  value={formatMetric(databaseMetrics.data.torrents.summary.seen_24h, "number")}
                />
                <MetricCard
                  label={t("analytics.inventory.debridItems")}
                  value={formatMetric(databaseMetrics.data.debrid_cache.total, "number")}
                />
                <MetricCard
                  label={t("analytics.inventory.uniqueSearches")}
                  value={formatMetric(databaseMetrics.data.searches.total_unique, "number")}
                />
                <MetricCard
                  detail={t("analytics.inventory.last24h")}
                  label={t("analytics.inventory.recentSearches")}
                  value={formatMetric(databaseMetrics.data.searches.last_24h, "number")}
                />
                <MetricCard
                  label={t("analytics.inventory.activeLocks")}
                  value={formatMetric(databaseMetrics.data.scrapers.active_locks, "number")}
                />
                <MetricCard
                  label={t("analytics.inventory.averageSize")}
                  value={formatMetric(databaseMetrics.data.torrents.summary.average_size, "bytes")}
                />
              </div>
              <div className="dashboard-columns">
                <article className="dashboard-panel">
                  <header>
                    <h3>{t("analytics.inventory.mediaDistribution")}</h3>
                  </header>
                  <DistributionDonut items={databaseMetrics.data.torrents.media_distribution} />
                </article>
                <article className="dashboard-panel">
                  <header>
                    <h3>{t("analytics.inventory.sizeDistribution")}</h3>
                  </header>
                  <DistributionChart items={databaseMetrics.data.torrents.size_distribution} />
                </article>
              </div>
              <article className="dashboard-panel">
                <header>
                  <h3>{t("analytics.inventory.debridServices")}</h3>
                </header>
                <div className="debrid-service-list">
                  {databaseMetrics.data.debrid_cache.by_service.map((service) => (
                    <div key={service.service}>
                      <strong>{service.service}</strong>
                      <span>
                        {formatMetric(service.count, "number")} ·{" "}
                        {formatMetric(service.total_size, "bytes")}
                      </span>
                      <small>
                        {t("analytics.inventory.average")}{" "}
                        {formatMetric(service.average_size, "bytes")}
                      </small>
                    </div>
                  ))}
                  {databaseMetrics.data.debrid_cache.by_service.length === 0 ? (
                    <p className="empty-state">{t("analytics.inventory.emptyDebrid")}</p>
                  ) : null}
                </div>
              </article>
            </section>
          ) : null}
          {databaseMetrics.isError ? (
            <Alert title={t("analytics.inventory.error")} tone="warning">
              <ApiErrorDetails
                error={databaseMetrics.error}
                fallback={t("analytics.inventory.errorDescription")}
              />
            </Alert>
          ) : null}

          {groups.map((group) => (
            <section className="metric-section" key={group}>
              <h2>{t(`analytics.groups.${group}`)}</h2>
              <div className="metric-grid">
                {catalog
                  .filter((item) => item.group === group)
                  .map((item) => (
                    <MetricCard
                      key={item.name}
                      label={t(`analytics.metrics.${item.name}`)}
                      tone={item.group === "usenet" ? "usenet" : "default"}
                      value={formatMetric(
                        metricValue(item.name, live.frames.slice(-13)),
                        item.style,
                      )}
                    />
                  ))}
              </div>
            </section>
          ))}
        </>
      ) : null}
    </section>
  );
}
