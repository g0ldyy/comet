import { useMutation, useQuery } from "@tanstack/react-query";
import { Boxes, Database, HardDrive, RefreshCw, ServerCog, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { formatMetric } from "../metrics/model";
import {
  checkForUpdates,
  getSystemDetails,
  getSystemSnapshot,
  restartRuntime,
  runRetention,
} from "./api";

function date(value: string | number | null): string {
  if (value === null) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(typeof value === "number" ? value * 1_000 : value));
}

function uptime(startedAt: number): string {
  const seconds = Math.max(0, Date.now() / 1_000 - startedAt);
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)}h`;
  return `${Math.floor(seconds / 86_400)}d ${Math.floor((seconds % 86_400) / 3_600)}h`;
}

export function SystemPage() {
  const { t } = useTranslation();
  const snapshot = useQuery({
    queryFn: getSystemSnapshot,
    queryKey: ["admin", "system", "snapshot"],
    refetchInterval: 15_000,
  });
  const details = useQuery({
    queryFn: getSystemDetails,
    queryKey: ["admin", "system", "details"],
    refetchInterval: 30_000,
  });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["admin", "system", "snapshot"] }),
      queryClient.invalidateQueries({ queryKey: ["admin", "system", "details"] }),
    ]);
  };
  const update = useMutation({ mutationFn: checkForUpdates });
  const retention = useMutation({ mutationFn: runRetention, onSuccess: refresh });
  const restart = useMutation({
    mutationFn: restartRuntime,
    onSuccess: refresh,
  });
  const actionFailed = update.isError || retention.isError || restart.isError;

  return (
    <section aria-labelledby="system-title" className="section-page dashboard-page system-page">
      <header className="section-page__header">
        <div>
          <h1 id="system-title">{t("nav.system")}</h1>
        </div>
      </header>

      {snapshot.isError || details.isError ? (
        <Alert title={t("system.errorTitle")} tone="danger">
          <ApiErrorDetails
            error={snapshot.error ?? details.error}
            fallback={t("system.errorDescription")}
          />
        </Alert>
      ) : null}
      {actionFailed ? (
        <Alert title={t("system.actionError")} tone="danger">
          <ApiErrorDetails
            error={update.error ?? retention.error ?? restart.error}
            fallback={t("system.actionErrorDescription")}
          />
        </Alert>
      ) : null}
      {snapshot.isPending || details.isPending ? (
        <Skeleton label={t("system.loading")} lines={10} />
      ) : snapshot.data && details.data ? (
        <>
          <div className="revision-strip">
            <div>
              <span>{t("system.readiness")}</span>
              <strong className={`health-state health-state--${snapshot.data.readiness.state}`}>
                {t(`overview.readiness.${snapshot.data.readiness.state}`)}
              </strong>
            </div>
            <div>
              <span>{t("system.replicas")}</span>
              <strong>{snapshot.data.runtimes.length}</strong>
            </div>
            <div>
              <span>{t("system.revision")}</span>
              <strong>
                {snapshot.data.applied_revision} / {snapshot.data.stored_revision}
              </strong>
            </div>
          </div>

          <div className="dashboard-columns">
            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">
                    <Boxes aria-hidden="true" size={15} />
                    {t("system.buildEyebrow")}
                  </span>
                  <h2>{t("system.build")}</h2>
                </div>
                <Button
                  disabled={update.isPending}
                  onClick={() => update.mutate()}
                  variant="secondary"
                >
                  <RefreshCw aria-hidden="true" size={15} />
                  {t("system.checkUpdates")}
                </Button>
              </header>
              <dl className="health-list">
                <div>
                  <dt>{t("system.commit")}</dt>
                  <dd>
                    <code>{details.data.build.commit_hash ?? "—"}</code>
                  </dd>
                </div>
                <div>
                  <dt>{t("system.branch")}</dt>
                  <dd>{details.data.build.branch}</dd>
                </div>
                <div>
                  <dt>{t("system.buildDate")}</dt>
                  <dd>{date(details.data.build.build_date)}</dd>
                </div>
                <div>
                  <dt>{t("system.deployment")}</dt>
                  <dd>
                    {t(
                      details.data.build.container_image
                        ? "system.containerImage"
                        : "system.sourceCheckout",
                    )}
                  </dd>
                </div>
                <div>
                  <dt>{t("system.python")}</dt>
                  <dd>
                    {details.data.build.python_implementation} {details.data.build.python_version}
                  </dd>
                </div>
                <div>
                  <dt>{t("system.nativeEngine")}</dt>
                  <dd>
                    {details.data.build.native_engine_enabled
                      ? `API v${details.data.build.native_engine_api_version}`
                      : t("system.disabled")}
                  </dd>
                </div>
              </dl>
              {update.data ? (
                <Alert
                  tone={update.data.error ? "warning" : update.data.has_update ? "info" : "success"}
                >
                  {update.data.error
                    ? t("system.updateFailed")
                    : update.data.has_update
                      ? t("system.updateAvailable", {
                          commit: update.data.latest_commit_hash,
                        })
                      : t("system.upToDate")}
                  {update.data.latest_url ? (
                    <>
                      {" "}
                      <a href={update.data.latest_url} rel="noreferrer" target="_blank">
                        {t("system.openCommit")}
                      </a>
                    </>
                  ) : null}
                  {!update.data.error ? (
                    <small className="system-update-instructions">
                      {t(`system.install.${update.data.install_method}`)}
                    </small>
                  ) : null}
                </Alert>
              ) : null}
            </article>

            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">
                    <Database aria-hidden="true" size={15} />
                    {t("system.databaseEyebrow")}
                  </span>
                  <h2>{t("system.database")}</h2>
                </div>
              </header>
              <dl className="health-list">
                <div>
                  <dt>{t("system.backend")}</dt>
                  <dd>{details.data.database.backend}</dd>
                </div>
                <div>
                  <dt>{t("system.schema")}</dt>
                  <dd
                    className={
                      details.data.database.schema_current
                        ? "health-state"
                        : "health-state health-state--unavailable"
                    }
                  >
                    {details.data.database.schema_version}
                  </dd>
                </div>
                <div>
                  <dt>{t("system.primary")}</dt>
                  <dd>
                    {details.data.database.primary_connected
                      ? t("system.connected")
                      : t("system.disconnected")}
                  </dd>
                </div>
                <div>
                  <dt>{t("system.readReplicas")}</dt>
                  <dd>
                    {details.data.database.replicas_active} /{" "}
                    {details.data.database.replicas_configured}
                  </dd>
                </div>
                <div>
                  <dt>{t("system.unavailableReplicas")}</dt>
                  <dd>{details.data.database.replicas_unavailable}</dd>
                </div>
              </dl>
            </article>
          </div>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">
                  <ServerCog aria-hidden="true" size={15} />
                  {t("system.runtimeEyebrow")}
                </span>
                <h2>{t("system.runtimes")}</h2>
              </div>
            </header>
            <div className="system-runtime-grid">
              {snapshot.data.runtimes.map((runtime) => (
                <div key={runtime.instance_id}>
                  <header>
                    <div>
                      <strong>{runtime.alias ?? runtime.hostname}</strong>
                      <small>{runtime.instance_id.slice(0, 8)}</small>
                    </div>
                    <span className={`status-pill status-pill--${runtime.readiness.state}`}>
                      {t(`overview.readiness.${runtime.readiness.state}`)}
                    </span>
                  </header>
                  <dl>
                    <div>
                      <dt>{t("system.uptime")}</dt>
                      <dd>{uptime(runtime.started_at)}</dd>
                    </div>
                    <div>
                      <dt>{t("system.processes")}</dt>
                      <dd>{runtime.processes.map((process) => process.role).join(" · ")}</dd>
                    </div>
                    <div>
                      <dt>{t("system.branch")}</dt>
                      <dd>{runtime.branch}</dd>
                    </div>
                    <div>
                      <dt>{t("system.appliedRevision")}</dt>
                      <dd>{runtime.applied_revision}</dd>
                    </div>
                  </dl>
                  {runtime.restart_capable ? (
                    <Button
                      disabled={restart.isPending}
                      onClick={() => {
                        if (window.confirm(t("system.restartConfirm"))) {
                          restart.mutate(runtime.instance_id);
                        }
                      }}
                      variant="danger"
                    >
                      {t("system.restart")}
                    </Button>
                  ) : (
                    <small>{t("system.restartUnavailable")}</small>
                  )}
                </div>
              ))}
            </div>
          </article>

          <div className="dashboard-columns system-resources-layout">
            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">
                    <HardDrive aria-hidden="true" size={15} />
                    {t("system.storageEyebrow")}
                  </span>
                  <h2>{t("system.storage")}</h2>
                </div>
              </header>
              <div className="system-storage-list">
                {details.data.storage.map((volume) => {
                  const usedRatio = (volume.used_bytes / volume.capacity_bytes) * 100;
                  return (
                    <div key={volume.name}>
                      <span>
                        <strong>{t(`system.volumes.${volume.name}`)}</strong>
                        <small>
                          {formatMetric(volume.free_bytes, "bytes")} {t("system.free")}
                          {volume.configured_limit_bytes
                            ? ` · ${t("system.limit")} ${formatMetric(volume.configured_limit_bytes, "bytes")}`
                            : ""}
                        </small>
                      </span>
                      <div aria-hidden="true">
                        <i style={{ width: `${usedRatio}%` }} />
                      </div>
                      <strong>{Math.round(usedRatio)}%</strong>
                    </div>
                  );
                })}
                {details.data.storage.length === 0 ? (
                  <p className="empty-state">{t("system.noStorage")}</p>
                ) : null}
              </div>
            </article>

            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">
                    <ShieldCheck aria-hidden="true" size={15} />
                    {t("system.operationsEyebrow")}
                  </span>
                  <h2>{t("system.featuresMaintenance")}</h2>
                </div>
              </header>
              <div className="system-feature-grid">
                {Object.entries(details.data.features).map(([feature, enabled]) => (
                  <div key={feature}>
                    <span>{t(`system.features.${feature}`)}</span>
                    <strong className={enabled ? "health-state" : "health-state--disabled"}>
                      {enabled ? t("system.enabled") : t("system.disabled")}
                    </strong>
                  </div>
                ))}
              </div>
              <div className="system-maintenance">
                <p>
                  {t("system.lastRetention")}:{" "}
                  <strong>{date(details.data.maintenance.last_retention_at)}</strong>
                </p>
                <p>
                  {t("system.scheduledRetention")}:{" "}
                  <strong>
                    {details.data.maintenance.retention_enabled
                      ? t("system.enabled")
                      : t("system.disabled")}
                  </strong>
                </p>
                <Button
                  disabled={retention.isPending}
                  onClick={() => retention.mutate()}
                  variant="secondary"
                >
                  {t("system.runRetention")}
                </Button>
                <small>{t("system.retentionHint")}</small>
              </div>
            </article>
          </div>
        </>
      ) : null}
    </section>
  );
}
