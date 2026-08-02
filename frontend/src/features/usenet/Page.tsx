import { useMutation, useQuery } from "@tanstack/react-query";
import { Archive, Ban, CirclePlay, Database, ListRestart } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { UsenetEngineStats } from "../../api/generated/contracts";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { SettingsShortcut } from "../../components/SettingsShortcut";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { StreamActivityChart, type StreamActivityRange } from "../activity/StreamActivityChart";
import { MetricCard } from "../metrics/MetricCard";
import { formatMetric } from "../metrics/model";
import {
  cancelUsenetOperation,
  controlUsenetRuntime,
  getUsenetActivity,
  getUsenetArtifacts,
  getUsenetHistory,
  getUsenetSnapshot,
  pruneUsenetArtifact,
} from "./api";

function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3_600)}h ${Math.round((seconds % 3_600) / 60)}m`;
}

function sum(stats: ReadonlyArray<UsenetEngineStats>, field: keyof UsenetEngineStats): number {
  return stats.reduce((total, current) => total + Number(current[field]), 0);
}

export function UsenetPage() {
  const { t } = useTranslation();
  const [activityRange, setActivityRange] = useState<StreamActivityRange>("auto");
  const snapshot = useQuery({
    queryFn: getUsenetSnapshot,
    queryKey: ["admin", "usenet", "snapshot"],
    refetchInterval: 2_000,
  });
  const history = useQuery({
    queryFn: getUsenetHistory,
    queryKey: ["admin", "usenet", "history"],
    refetchInterval: 10_000,
  });
  const activity = useQuery({
    placeholderData: (previous) => previous,
    queryFn: () => getUsenetActivity(activityRange),
    queryKey: ["admin", "usenet", "activity", activityRange],
    refetchInterval: 5_000,
  });
  const artifacts = useQuery({
    queryFn: getUsenetArtifacts,
    queryKey: ["admin", "usenet", "artifacts"],
    refetchInterval: 10_000,
  });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["admin", "usenet", "snapshot"] }),
      queryClient.invalidateQueries({ queryKey: ["admin", "usenet", "history"] }),
      queryClient.invalidateQueries({ queryKey: ["admin", "usenet", "artifacts"] }),
    ]);
  };
  const cancel = useMutation({ mutationFn: cancelUsenetOperation, onSuccess: refresh });
  const control = useMutation({
    mutationFn: ({
      action,
      instanceId,
      processId,
    }: {
      action: "drain" | "resume";
      instanceId: string;
      processId: number;
    }) => controlUsenetRuntime(instanceId, processId, action),
    onSuccess: refresh,
  });
  const prune = useMutation({ mutationFn: pruneUsenetArtifact, onSuccess: refresh });
  const runtimeStats =
    snapshot.data?.runtimes.flatMap((runtime) => (runtime.stats ? [runtime.stats] : [])) ?? [];

  return (
    <section aria-labelledby="usenet-title" className="section-page dashboard-page operations-page">
      <header className="section-page__header">
        <div>
          <h1 id="usenet-title">{t("nav.usenet")}</h1>
        </div>
        <SettingsShortcut />
      </header>

      {snapshot.isError ? (
        <Alert title={t("usenet.errorTitle")} tone="danger">
          <ApiErrorDetails error={snapshot.error} fallback={t("usenet.errorDescription")} />
        </Alert>
      ) : null}
      {history.isError || artifacts.isError || activity.isError ? (
        <Alert title={t("usenet.errorTitle")} tone="warning">
          <ApiErrorDetails
            error={history.error ?? artifacts.error ?? activity.error}
            fallback={t("usenet.errorDescription")}
          />
        </Alert>
      ) : null}
      {cancel.isError || control.isError || prune.isError ? (
        <Alert title={t("usenet.actionError")} tone="danger">
          <ApiErrorDetails
            error={cancel.error ?? control.error ?? prune.error}
            fallback={t("usenet.actionErrorDescription")}
          />
        </Alert>
      ) : null}
      {snapshot.isPending ? (
        <Skeleton label={t("usenet.loading")} lines={10} />
      ) : snapshot.data ? (
        <>
          {!snapshot.data.enabled ? (
            <Alert title={t("usenet.disabledTitle")} tone="warning">
              {t("usenet.disabledDescription")}
            </Alert>
          ) : null}

          <div className="metric-grid metric-grid--hero">
            <MetricCard
              detail={t("usenet.rightNow")}
              label={t("usenet.activeStreams")}
              tone="usenet"
              value={formatMetric(snapshot.data.active.length, "number")}
            />
            <MetricCard
              detail={t("usenet.openConnections", {
                count: sum(runtimeStats, "nntp_connections_open"),
              })}
              label={t("usenet.nntpActive")}
              value={formatMetric(sum(runtimeStats, "nntp_connections_active"), "number")}
            />
            <MetricCard
              detail={t("usenet.memoryAndDisk")}
              label={t("usenet.cache")}
              value={formatMetric(
                sum(runtimeStats, "segment_cache_bytes") + sum(runtimeStats, "disk_cache_bytes"),
                "bytes",
              )}
            />
            <MetricCard
              detail={`${snapshot.data.history.streams_7d} ${t("usenet.recentStreams")} · ${snapshot.data.history.failed_7d} ${t("usenet.failed")}`}
              label={t("usenet.delivered")}
              value={formatMetric(snapshot.data.history.bytes_7d, "bytes")}
            />
          </div>

          <StreamActivityChart
            data={activity.data}
            isPending={activity.isPending || activity.isPlaceholderData}
            labels={{
              active: t("usenet.activeStreams"),
              auto: t("analytics.auto"),
              completed: t("usenet.recentStreams"),
              concurrency: t("usenet.activeStreams"),
              empty: t("usenet.noHistory"),
              failed: t("usenet.failed"),
              interrupted: t("analytics.interrupted"),
              outcomes: t("usenet.recentStreams"),
              range: t("analytics.range"),
              title: t("usenet.historyEyebrow"),
              volume: t("usenet.delivered"),
            }}
            onRangeChange={setActivityRange}
            range={activityRange}
            tone="usenet"
          />

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow eyebrow--usenet">{t("usenet.runtimeEyebrow")}</span>
                <h2>{t("usenet.runtimes")}</h2>
              </div>
              <span>{snapshot.data.runtimes.length}</span>
            </header>
            <div className="scraper-grid usenet-runtime-grid">
              {snapshot.data.runtimes.map((runtime) => {
                const stats = runtime.stats;
                return (
                  <div key={runtime.instance_id}>
                    <strong>
                      {runtime.instance_id.slice(0, 8)} · {runtime.process_id}
                    </strong>
                    <small>
                      {runtime.healthy ? runtime.mode : t("usenet.unavailable")}
                      {stats?.draining ? ` · ${t("usenet.draining")}` : ""}
                    </small>
                    {stats ? (
                      <dl>
                        <div>
                          <dt>{t("usenet.sessions")}</dt>
                          <dd>{stats.sessions}</dd>
                        </div>
                        <div>
                          <dt>{t("usenet.nntpPools")}</dt>
                          <dd>{stats.nntp_pools}</dd>
                        </div>
                        <div>
                          <dt>{t("usenet.queue")}</dt>
                          <dd>
                            {stats.nntp_queue_interactive +
                              stats.nntp_queue_preparation +
                              stats.nntp_queue_background}
                          </dd>
                        </div>
                        <div>
                          <dt>{t("usenet.jobs")}</dt>
                          <dd>{stats.archive_jobs_active + stats.repair_jobs_active}</dd>
                        </div>
                        <div>
                          <dt>{t("usenet.failovers")}</dt>
                          <dd>{stats.nntp_provider_failovers_total}</dd>
                        </div>
                        <div>
                          <dt>{t("usenet.openCircuits")}</dt>
                          <dd>
                            {stats.nntp_circuits_auth_open +
                              stats.nntp_circuits_transient_open +
                              stats.nntp_circuits_half_open}
                          </dd>
                        </div>
                      </dl>
                    ) : null}
                    {runtime.healthy && stats ? (
                      <Button
                        disabled={control.isPending}
                        onClick={() =>
                          control.mutate({
                            action: stats.draining ? "resume" : "drain",
                            instanceId: runtime.instance_id,
                            processId: runtime.process_id,
                          })
                        }
                        variant="secondary"
                      >
                        {stats.draining ? (
                          <CirclePlay aria-hidden="true" size={16} />
                        ) : (
                          <ListRestart aria-hidden="true" size={16} />
                        )}
                        {t(stats.draining ? "usenet.resume" : "usenet.drain")}
                      </Button>
                    ) : null}
                  </div>
                );
              })}
              {snapshot.data.runtimes.length === 0 ? (
                <p className="empty-state">{t("usenet.noRuntime")}</p>
              ) : null}
            </div>
          </article>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow eyebrow--usenet">{t("usenet.liveEyebrow")}</span>
                <h2>{t("usenet.liveStreams")}</h2>
              </div>
              <span>{snapshot.data.active.length}</span>
            </header>
            <div className="operations-table-wrap">
              <table className="operations-table">
                <thead>
                  <tr>
                    <th>{t("usenet.content")}</th>
                    <th>{t("usenet.client")}</th>
                    <th>{t("usenet.member")}</th>
                    <th>{t("usenet.progress")}</th>
                    <th>{t("usenet.duration")}</th>
                    <th>
                      <span className="visually-hidden">{t("usenet.actions")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.data.active.map((operation) => (
                    <tr key={operation.id}>
                      <td data-label={t("usenet.content")}>
                        <strong>{operation.title}</strong>
                        <small>{operation.content_id}</small>
                      </td>
                      <td data-label={t("usenet.client")}>
                        <code>{operation.client_ip}</code>
                      </td>
                      <td data-label={t("usenet.member")}>{operation.member_path}</td>
                      <td data-label={t("usenet.progress")}>
                        <progress max={operation.total_bytes} value={operation.bytes_transferred} />
                        <small>
                          {formatMetric(operation.bytes_transferred, "bytes")} /{" "}
                          {formatMetric(operation.total_bytes, "bytes")} ·{" "}
                          {Math.round((operation.bytes_transferred / operation.total_bytes) * 100)}%
                        </small>
                      </td>
                      <td data-label={t("usenet.duration")}>{duration(operation.duration)}</td>
                      <td data-label={t("usenet.actions")}>
                        <Button
                          disabled={operation.cancellation_pending || cancel.isPending}
                          onClick={() => cancel.mutate(operation.id)}
                          variant="danger"
                        >
                          <Ban aria-hidden="true" size={15} />
                          {t(
                            operation.cancellation_pending ? "usenet.cancelling" : "usenet.cancel",
                          )}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {snapshot.data.active.length === 0 ? (
                <p className="empty-state">{t("usenet.noActive")}</p>
              ) : null}
            </div>
          </article>

          <div className="operations-split">
            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow eyebrow--usenet">
                    <Archive aria-hidden="true" size={15} />
                    {t("usenet.preparationEyebrow")}
                  </span>
                  <h2>{t("usenet.preparations")}</h2>
                </div>
              </header>
              <div className="operations-list">
                {snapshot.data.preparations.map((preparation) => (
                  <div key={preparation.id}>
                    <span>
                      <strong>{preparation.title}</strong>
                      <small>
                        {preparation.media_id} · {preparation.provider_kind}
                      </small>
                    </span>
                    <span>{preparation.state}</span>
                  </div>
                ))}
                {snapshot.data.preparations.length === 0 ? (
                  <p className="empty-state">{t("usenet.noPreparations")}</p>
                ) : null}
              </div>
            </article>

            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow eyebrow--usenet">
                    <Database aria-hidden="true" size={15} />
                    {t("usenet.inventoryEyebrow")}
                  </span>
                  <h2>{t("usenet.inventory")}</h2>
                </div>
              </header>
              <div className="metric-grid">
                <MetricCard
                  label={t("usenet.artifacts")}
                  value={formatMetric(snapshot.data.inventory.artifacts, "number")}
                />
                <MetricCard
                  label={t("usenet.nzbStorage")}
                  value={formatMetric(snapshot.data.inventory.nzb_bytes, "bytes")}
                />
                <MetricCard
                  label={t("usenet.materialized")}
                  value={formatMetric(snapshot.data.inventory.materialized_bytes, "bytes")}
                />
                <MetricCard
                  label={t("usenet.readers")}
                  value={formatMetric(snapshot.data.inventory.active_readers, "number")}
                />
              </div>
            </article>
          </div>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow eyebrow--usenet">{t("usenet.artifactEyebrow")}</span>
                <h2>{t("usenet.artifactInventory")}</h2>
              </div>
              <span>
                {t("usenet.eligible", {
                  count: snapshot.data.inventory.eligible_for_prune,
                })}
              </span>
            </header>
            <div className="operations-table-wrap">
              <table className="operations-table">
                <thead>
                  <tr>
                    <th>{t("usenet.identity")}</th>
                    <th>{t("usenet.kind")}</th>
                    <th>{t("usenet.size")}</th>
                    <th>{t("usenet.references")}</th>
                    <th>{t("usenet.lastUsed")}</th>
                    <th>
                      <span className="visually-hidden">{t("usenet.actions")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {artifacts.data?.items.map((artifact) => (
                    <tr key={artifact.artifact_sha256}>
                      <td data-label={t("usenet.identity")}>
                        <code>{artifact.artifact_sha256.slice(0, 16)}</code>
                        <small>{artifact.publication_state}</small>
                      </td>
                      <td data-label={t("usenet.kind")}>{artifact.storage_kind}</td>
                      <td data-label={t("usenet.size")}>
                        {formatMetric(artifact.byte_size, "bytes")}
                      </td>
                      <td data-label={t("usenet.references")}>
                        {artifact.refcount + artifact.active_readers}
                      </td>
                      <td data-label={t("usenet.lastUsed")}>
                        {new Date(artifact.last_used_at * 1_000).toLocaleString()}
                      </td>
                      <td data-label={t("usenet.actions")}>
                        <Button
                          disabled={!artifact.eligible_for_prune || prune.isPending}
                          onClick={() => prune.mutate(artifact.artifact_sha256)}
                          variant="danger"
                        >
                          {t("usenet.prune")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {artifacts.data?.items.length === 0 ? (
                <p className="empty-state">{t("usenet.noArtifacts")}</p>
              ) : null}
            </div>
          </article>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow eyebrow--usenet">{t("usenet.historyEyebrow")}</span>
                <h2>{t("usenet.recentStreams")}</h2>
              </div>
              <span>
                {snapshot.data.history.streams_7d} · {snapshot.data.history.failed_7d}{" "}
                {t("usenet.failed")}
              </span>
            </header>
            <div className="operations-list">
              {history.data?.items.map((operation) => (
                <div key={operation.id}>
                  <span>
                    <strong>{operation.title}</strong>
                    <small>
                      {operation.member_path} · {operation.client_ip}
                    </small>
                  </span>
                  <span>
                    <strong>{formatMetric(operation.bytes_transferred, "bytes")}</strong>
                    <small className={`operation-outcome operation-outcome--${operation.outcome}`}>
                      {new Date(operation.finished_at * 1_000).toLocaleString()} ·{" "}
                      {duration(operation.duration)} · {operation.outcome}
                    </small>
                  </span>
                </div>
              ))}
              {history.data?.items.length === 0 ? (
                <p className="empty-state">{t("usenet.noHistory")}</p>
              ) : null}
            </div>
          </article>
        </>
      ) : null}
    </section>
  );
}
