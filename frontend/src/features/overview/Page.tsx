import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowUpRight, Gauge, RadioTower, Server, Waves } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Skeleton } from "../../components/ui/Skeleton";
import { getEvents } from "../logs/api";
import { getSystemSnapshot } from "../metrics/api";
import { MetricCard } from "../metrics/MetricCard";
import {
  counterRate,
  formatMetric,
  histogramQuantile,
  type MetricFrame,
  ratioRate,
  sampleTotal,
} from "../metrics/model";
import { useLiveMetrics } from "../metrics/useLiveMetrics";

const errorTotal = (samples: Parameters<typeof sampleTotal>[0]) =>
  samples
    .filter(
      (sample) =>
        sample.name === "comet_http_requests_total" &&
        sample.labels.status?.startsWith("5") === true,
    )
    .reduce((total, sample) => total + sample.value, 0);

function counterSignal(frames: MetricFrame[], total: (frame: MetricFrame) => number): number[] {
  return frames.slice(1).map((frame, index) => {
    const previous = frames[index] as MetricFrame;
    return Math.max(0, total(frame) - total(previous)) / (frame.at - previous.at);
  });
}

function ratioSignal(
  frames: MetricFrame[],
  numerator: (frame: MetricFrame) => number,
  denominator: (frame: MetricFrame) => number,
): number[] {
  return frames.slice(1).map((frame, index) => {
    const previous = frames[index] as MetricFrame;
    const denominatorChange = denominator(frame) - denominator(previous);
    return denominatorChange > 0
      ? Math.max(0, numerator(frame) - numerator(previous)) / denominatorChange
      : 0;
  });
}

export function OverviewPage() {
  const { t } = useTranslation();
  const metrics = useLiveMetrics();
  const liveFrames = metrics.frames.slice(-13);
  const system = useQuery({
    queryFn: getSystemSnapshot,
    queryKey: ["admin", "system", "snapshot"],
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const incidents = useQuery({
    queryFn: () => getEvents("level=ERROR", undefined),
    queryKey: ["admin", "events", "incidents"],
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const latest = metrics.data?.samples ?? [];
  const requestRate = counterRate(liveFrames, "comet_http_requests_total");
  const errorRatio = ratioRate(liveFrames, errorTotal, (samples) =>
    sampleTotal(samples, "comet_http_requests_total"),
  );
  const cacheRatio = ratioRate(
    liveFrames,
    (samples) => sampleTotal(samples, "comet_torrent_cache_lookups_total", { result: "hit" }),
    (samples) => sampleTotal(samples, "comet_torrent_cache_lookups_total"),
  );
  const requestSignal = counterSignal(liveFrames, ({ samples }) =>
    sampleTotal(samples, "comet_http_requests_total"),
  );
  const errorSignal = ratioSignal(
    liveFrames,
    ({ samples }) => errorTotal(samples),
    ({ samples }) => sampleTotal(samples, "comet_http_requests_total"),
  );
  const cacheSignal = ratioSignal(
    liveFrames,
    ({ samples }) => sampleTotal(samples, "comet_torrent_cache_lookups_total", { result: "hit" }),
    ({ samples }) => sampleTotal(samples, "comet_torrent_cache_lookups_total"),
  );
  const latencySignal = liveFrames.flatMap((_, index) => {
    const value = histogramQuantile(
      liveFrames.slice(Math.max(0, index - 3), index + 1),
      "comet_http_request_duration_seconds",
      0.95,
    );
    return value === null ? [] : [value];
  });
  const readiness = system.data?.readiness;

  return (
    <section aria-labelledby="overview-title" className="section-page dashboard-page">
      <header className="section-page__header">
        <div>
          <h1 id="overview-title">{t("nav.overview")}</h1>
        </div>
      </header>

      {metrics.isError || system.isError ? (
        <Alert title={t("overview.errorTitle")} tone="danger">
          <ApiErrorDetails
            error={metrics.error ?? system.error}
            fallback={t("overview.errorDescription")}
          />
        </Alert>
      ) : null}
      {metrics.isPending ? (
        <Skeleton label={t("overview.loading")} lines={6} />
      ) : (
        <>
          <div className="metric-grid metric-grid--hero">
            <MetricCard
              detail={t("overview.window")}
              label={t("metrics.httpRequests")}
              signal={requestSignal}
              value={formatMetric(requestRate, "rate")}
            />
            <MetricCard
              detail="p95"
              label={t("metrics.httpLatency")}
              signal={latencySignal}
              tone="usenet"
              value={formatMetric(
                histogramQuantile(liveFrames, "comet_http_request_duration_seconds", 0.95),
                "seconds",
              )}
            />
            <MetricCard
              label={t("metrics.httpErrors")}
              signal={errorSignal}
              tone={errorRatio !== null && errorRatio > 0.05 ? "danger" : "default"}
              value={formatMetric(errorRatio, "percent")}
            />
            <MetricCard
              label={t("metrics.cacheHitRatio")}
              signal={cacheSignal}
              tone="live"
              value={formatMetric(cacheRatio, "percent")}
            />
          </div>

          <div className="dashboard-columns">
            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">
                    <Server aria-hidden="true" size={15} />
                    {t("overview.system")}
                  </span>
                  <h2>{t("overview.systemHealth")}</h2>
                </div>
                <Link to="/admin/system">
                  {t("overview.open")}
                  <ArrowUpRight aria-hidden="true" size={15} />
                </Link>
              </header>
              {system.isPending ? (
                <Skeleton label={t("overview.loading")} lines={3} />
              ) : (
                <dl className="health-list">
                  {Object.entries(readiness?.components ?? {}).map(([component, state]) => (
                    <div key={component}>
                      <dt>{component.replaceAll("_", " ")}</dt>
                      <dd className={`health-state health-state--${state}`}>{state}</dd>
                    </div>
                  ))}
                  <div>
                    <dt>{t("overview.revision")}</dt>
                    <dd>
                      {system.data?.applied_revision} / {system.data?.stored_revision}
                    </dd>
                  </div>
                  <div>
                    <dt>{t("overview.replicas")}</dt>
                    <dd>{system.data?.runtimes.length}</dd>
                  </div>
                </dl>
              )}
            </article>

            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">
                    <Gauge aria-hidden="true" size={15} />
                    {t("overview.work")}
                  </span>
                  <h2>{t("overview.activeWork")}</h2>
                </div>
                <Link to="/admin/analytics">
                  {t("overview.open")}
                  <ArrowUpRight aria-hidden="true" size={15} />
                </Link>
              </header>
              <div className="compact-metrics">
                <MetricCard
                  label={t("metrics.proxyActive")}
                  value={formatMetric(
                    sampleTotal(latest, "comet_proxy_stream_active_connections"),
                    "number",
                  )}
                />
                <MetricCard
                  label={t("metrics.backgroundQueue")}
                  value={formatMetric(
                    sampleTotal(latest, "comet_background_scraper_queue_items"),
                    "number",
                  )}
                />
                <MetricCard
                  label={t("metrics.usenetEngine")}
                  tone="usenet"
                  value={
                    sampleTotal(latest, "comet_usenet_engine_configured") === 0
                      ? t("overview.disabled")
                      : sampleTotal(latest, "comet_usenet_engine_up") > 0
                        ? t("overview.online")
                        : t("overview.offline")
                  }
                />
                <MetricCard
                  label={t("metrics.databaseFallbacks")}
                  value={formatMetric(
                    counterRate(liveFrames, "comet_database_replica_fallbacks_total"),
                    "rate",
                  )}
                />
              </div>
            </article>
          </div>

          <article className="dashboard-panel incident-panel">
            <header>
              <div>
                <span className="eyebrow">
                  <RadioTower aria-hidden="true" size={15} />
                  {t("overview.incidents")}
                </span>
                <h2>{t("overview.recentIncidents")}</h2>
              </div>
              <Link to="/admin/logs">
                {t("nav.logs")}
                <ArrowUpRight aria-hidden="true" size={15} />
              </Link>
            </header>
            <div className="incident-list">
              {incidents.isPending ? <Skeleton label={t("overview.loading")} lines={3} /> : null}
              {incidents.data?.items.slice(0, 6).map((event) => (
                <Link key={event.id} to="/admin/logs">
                  <Waves aria-hidden="true" size={16} />
                  <span>
                    <strong>{event.message}</strong>
                    <small>
                      {event.category} · {new Date(event.created_at * 1_000).toLocaleString()}
                    </small>
                  </span>
                  <code>{event.error_code ?? event.event}</code>
                </Link>
              ))}
              {incidents.data?.items.length === 0 ? (
                <p className="empty-state">{t("overview.noIncidents")}</p>
              ) : null}
            </div>
          </article>
        </>
      )}
    </section>
  );
}
