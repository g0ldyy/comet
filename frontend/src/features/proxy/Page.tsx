import { useMutation, useQuery } from "@tanstack/react-query";
import { Ban } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { SettingsShortcut } from "../../components/SettingsShortcut";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { StreamActivityChart, type StreamActivityRange } from "../activity/StreamActivityChart";
import { MetricCard } from "../metrics/MetricCard";
import { formatMetric } from "../metrics/model";
import { cancelProxyConnection, getProxyActivity, getProxyHistory, getProxySnapshot } from "./api";

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3_600)}h ${Math.round((seconds % 3_600) / 60)}m`;
}

export function ProxyPage() {
  const { t } = useTranslation();
  const [activityRange, setActivityRange] = useState<StreamActivityRange>("auto");
  const snapshot = useQuery({
    queryFn: getProxySnapshot,
    queryKey: ["admin", "proxy", "snapshot"],
    refetchInterval: 2_000,
  });
  const history = useQuery({
    queryFn: getProxyHistory,
    queryKey: ["admin", "proxy", "history"],
    refetchInterval: 10_000,
  });
  const activity = useQuery({
    placeholderData: (previous) => previous,
    queryFn: () => getProxyActivity(activityRange),
    queryKey: ["admin", "proxy", "activity", activityRange],
    refetchInterval: 5_000,
  });
  const cancel = useMutation({
    mutationFn: cancelProxyConnection,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin", "proxy", "snapshot"] }),
        queryClient.invalidateQueries({ queryKey: ["admin", "proxy", "history"] }),
      ]);
    },
  });

  return (
    <section aria-labelledby="proxy-title" className="section-page dashboard-page operations-page">
      <header className="section-page__header">
        <div>
          <h1 id="proxy-title">{t("nav.proxy")}</h1>
        </div>
        <SettingsShortcut />
      </header>

      {snapshot.isError ? (
        <Alert title={t("proxy.errorTitle")} tone="danger">
          <ApiErrorDetails error={snapshot.error} fallback={t("proxy.errorDescription")} />
        </Alert>
      ) : null}
      {history.isError || activity.isError ? (
        <Alert title={t("proxy.errorTitle")} tone="warning">
          <ApiErrorDetails
            error={history.error ?? activity.error}
            fallback={t("proxy.errorDescription")}
          />
        </Alert>
      ) : null}
      {cancel.isError ? (
        <Alert title={t("proxy.cancelError")} tone="danger">
          <ApiErrorDetails error={cancel.error} fallback={t("proxy.cancelErrorDescription")} />
        </Alert>
      ) : null}
      {snapshot.isPending ? (
        <Skeleton label={t("proxy.loading")} lines={8} />
      ) : snapshot.data ? (
        <>
          {!snapshot.data.enabled ? (
            <Alert title={t("proxy.disabledTitle")} tone="warning">
              {t("proxy.disabledDescription")}
            </Alert>
          ) : null}
          <div className="metric-grid metric-grid--hero">
            <MetricCard
              detail={t("proxy.rightNow")}
              label={t("proxy.active")}
              tone="live"
              value={formatMetric(snapshot.data.summary.active_connections, "number")}
            />
            <MetricCard
              label={t("proxy.throughput")}
              value={formatMetric(snapshot.data.summary.current_speed, "bytesRate")}
            />
            <MetricCard
              detail={t("proxy.lastSevenDays")}
              label={t("proxy.transferred")}
              value={formatMetric(snapshot.data.summary.bytes_7d, "bytes")}
            />
            <MetricCard
              detail={`${snapshot.data.summary.failed_7d} ${t("proxy.failed")}`}
              label={t("proxy.completed")}
              value={formatMetric(snapshot.data.summary.completed_7d, "number")}
            />
          </div>

          <StreamActivityChart
            data={activity.data}
            isPending={activity.isPending || activity.isPlaceholderData}
            labels={{
              active: t("proxy.active"),
              auto: t("analytics.auto"),
              completed: t("proxy.completed"),
              concurrency: t("proxy.peakConcurrent"),
              empty: t("proxy.noHistory"),
              failed: t("proxy.failed"),
              interrupted: t("analytics.interrupted"),
              outcomes: t("proxy.recentTitle"),
              range: t("analytics.range"),
              title: t("proxy.historyTitle"),
              volume: t("proxy.transferred"),
            }}
            onRangeChange={setActivityRange}
            range={activityRange}
            tone="proxy"
          />

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">{t("proxy.liveEyebrow")}</span>
                <h2>{t("proxy.liveTitle")}</h2>
              </div>
              <span>{snapshot.data.active.length}</span>
            </header>
            <div className="operations-table-wrap">
              <table className="operations-table">
                <thead>
                  <tr>
                    <th>{t("proxy.content")}</th>
                    <th>{t("proxy.client")}</th>
                    <th>{t("proxy.service")}</th>
                    <th>{t("proxy.duration")}</th>
                    <th>{t("proxy.current")}</th>
                    <th>{t("proxy.average")}</th>
                    <th>{t("proxy.peak")}</th>
                    <th>{t("proxy.bytes")}</th>
                    <th>
                      <span className="visually-hidden">{t("proxy.actions")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.data.active.map((connection) => (
                    <tr key={connection.id}>
                      <td data-label={t("proxy.content")}>
                        <strong>{connection.content}</strong>
                        <small>
                          {connection.instance_id.slice(0, 8)} · {connection.process_id}
                        </small>
                      </td>
                      <td data-label={t("proxy.client")}>
                        <code>{connection.ip}</code>
                      </td>
                      <td data-label={t("proxy.service")}>{connection.service}</td>
                      <td data-label={t("proxy.duration")}>{duration(connection.duration)}</td>
                      <td data-label={t("proxy.current")}>
                        {formatMetric(connection.current_speed, "bytesRate")}
                      </td>
                      <td data-label={t("proxy.average")}>
                        {formatMetric(connection.average_speed, "bytesRate")}
                      </td>
                      <td data-label={t("proxy.peak")}>
                        {formatMetric(connection.peak_speed, "bytesRate")}
                      </td>
                      <td data-label={t("proxy.bytes")}>
                        {formatMetric(connection.bytes_transferred, "bytes")}
                      </td>
                      <td data-label={t("proxy.actions")}>
                        <Button
                          aria-label={t("proxy.cancelConnection", {
                            content: connection.content,
                          })}
                          disabled={connection.cancellation_pending || cancel.isPending}
                          onClick={() => cancel.mutate(connection.id)}
                          variant="danger"
                        >
                          <Ban aria-hidden="true" size={15} />
                          {connection.cancellation_pending
                            ? t("proxy.cancelling")
                            : t("proxy.cancel")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {snapshot.data.active.length === 0 ? (
                <p className="empty-state">{t("proxy.noActive")}</p>
              ) : null}
            </div>
          </article>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">{t("proxy.recentEyebrow")}</span>
                <h2>{t("proxy.recentTitle")}</h2>
              </div>
            </header>
            <div className="operations-list">
              {history.data?.items.map((connection) => (
                <div key={connection.id}>
                  <span>
                    <strong>{connection.content}</strong>
                    <small>
                      {connection.ip} · {connection.service} ·{" "}
                      {new Date(connection.finished_at * 1_000).toLocaleString()}
                    </small>
                  </span>
                  <span>
                    <strong>{formatMetric(connection.bytes_transferred, "bytes")}</strong>
                    <small className={`operation-outcome operation-outcome--${connection.outcome}`}>
                      {duration(connection.duration)} · {connection.outcome}
                    </small>
                  </span>
                </div>
              ))}
              {history.data?.items.length === 0 ? (
                <p className="empty-state">{t("proxy.noHistory")}</p>
              ) : null}
            </div>
          </article>
        </>
      ) : null}
    </section>
  );
}
