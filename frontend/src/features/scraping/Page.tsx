import { useMutation, useQuery } from "@tanstack/react-query";
import { CirclePause, CirclePlay, ListRestart, OctagonX, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ScraperQueueEntry } from "../../api/generated/contracts";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { SettingsShortcut } from "../../components/SettingsShortcut";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { MetricCard } from "../metrics/MetricCard";
import { counterRate, formatMetric, histogramQuantile, type MetricFrame } from "../metrics/model";
import { useLiveMetrics } from "../metrics/useLiveMetrics";
import {
  controlScraper,
  getScraperQueue,
  getScraperRuns,
  getScrapingSnapshot,
  mutateQueue,
  type QueueAction,
  type QueueKind,
  requeueDead,
  type ScraperControl,
} from "./api";

const statuses = ["", "discovered", "running", "success", "failed", "deferred", "dead"];

function scraperNames(frames: ReadonlyArray<MetricFrame>): string[] {
  const names = new Set<string>();
  for (const sample of frames.at(-1)?.samples ?? []) {
    if (
      (sample.name === "comet_scraper_requests_total" ||
        sample.name === "comet_scraper_torrents_total") &&
      sample.labels.scraper
    ) {
      names.add(sample.labels.scraper);
    }
  }
  return [...names].sort();
}

function ago(timestamp: number | null): string {
  if (timestamp === null) return "—";
  const seconds = Math.max(0, Date.now() / 1_000 - timestamp);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3_600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3_600)}h`;
}

function QueueActions({
  entry,
  pending,
  run,
  t,
}: {
  entry: ScraperQueueEntry;
  pending: boolean;
  run: (entry: ScraperQueueEntry, action: QueueAction) => void;
  t: (key: string) => string;
}) {
  return (
    <div className="row-actions">
      <Button
        disabled={entry.status === "running" || pending}
        onClick={() => run(entry, "retry")}
        variant="secondary"
      >
        {t("scraping.retry")}
      </Button>
      <Button
        disabled={entry.status === "running" || pending}
        onClick={() => run(entry, "defer")}
        variant="ghost"
      >
        {t("scraping.defer")}
      </Button>
      <Button
        disabled={entry.status === "running" || pending}
        onClick={() => run(entry, "abandon")}
        variant="danger"
      >
        {t("scraping.abandon")}
      </Button>
    </div>
  );
}

export function ScrapingPage() {
  const { t } = useTranslation();
  const [kind, setKind] = useState<QueueKind>("item");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const live = useLiveMetrics();
  const frames = live.frames.slice(-13);
  const snapshot = useQuery({
    queryFn: getScrapingSnapshot,
    queryKey: ["admin", "scraping", "snapshot"],
    refetchInterval: 2_000,
  });
  const queue = useQuery({
    queryFn: () => getScraperQueue(kind, status, search),
    queryKey: ["admin", "scraping", "queue", kind, status, search],
    staleTime: 1_000,
  });
  const runs = useQuery({
    queryFn: getScraperRuns,
    queryKey: ["admin", "scraping", "runs"],
    refetchInterval: 10_000,
  });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["admin", "scraping", "snapshot"] }),
      queryClient.invalidateQueries({ queryKey: ["admin", "scraping", "queue"] }),
      queryClient.invalidateQueries({ queryKey: ["admin", "scraping", "runs"] }),
    ]);
  };
  const control = useMutation({ mutationFn: controlScraper, onSuccess: refresh });
  const queueAction = useMutation({
    mutationFn: ({ action, entry }: { action: QueueAction; entry: ScraperQueueEntry }) =>
      mutateQueue(entry.kind, entry.id, action),
    onSuccess: refresh,
  });
  const requeue = useMutation({ mutationFn: requeueDead, onSuccess: refresh });
  const activeRuntimes = snapshot.data?.runtimes.filter(({ state }) => state !== "stopped") ?? [];
  const paused =
    activeRuntimes.length > 0 && activeRuntimes.every(({ state }) => state === "paused");
  const draining = activeRuntimes.some((runtime) => runtime.draining);
  const names = useMemo(() => scraperNames(frames), [frames]);

  const executeControl = (action: ScraperControl) => control.mutate(action);

  return (
    <section
      aria-labelledby="scraping-title"
      className="section-page dashboard-page operations-page"
    >
      <header className="section-page__header">
        <div>
          <h1 id="scraping-title">{t("nav.scraping")}</h1>
        </div>
        <SettingsShortcut />
      </header>

      {snapshot.isError || queue.isError || runs.isError || live.isError ? (
        <Alert title={t("scraping.errorTitle")} tone="danger">
          <ApiErrorDetails
            error={snapshot.error ?? queue.error ?? runs.error ?? live.error}
            fallback={t("scraping.errorDescription")}
          />
        </Alert>
      ) : null}
      {control.isError || queueAction.isError || requeue.isError ? (
        <Alert title={t("scraping.actionError")} tone="danger">
          <ApiErrorDetails
            error={control.error ?? queueAction.error ?? requeue.error}
            fallback={t("scraping.actionErrorDescription")}
          />
        </Alert>
      ) : null}
      {snapshot.isPending ? (
        <Skeleton label={t("scraping.loading")} lines={9} />
      ) : snapshot.data ? (
        <>
          <div className="control-strip">
            <div>
              <span>{t("scraping.runtimeState")}</span>
              <strong>
                {activeRuntimes.length === 0
                  ? t("scraping.stopped")
                  : draining
                    ? t("scraping.draining")
                    : paused
                      ? t("scraping.paused")
                      : t("scraping.running")}
              </strong>
              <small>{t("scraping.ownerCount", { count: activeRuntimes.length })}</small>
            </div>
            <div className="control-strip__actions">
              {activeRuntimes.length === 0 ? (
                <Button disabled={control.isPending} onClick={() => executeControl("start")}>
                  <CirclePlay aria-hidden="true" size={16} />
                  {t("scraping.start")}
                </Button>
              ) : (
                <>
                  <Button
                    disabled={control.isPending}
                    onClick={() => executeControl(paused ? "resume" : "pause")}
                    variant="secondary"
                  >
                    {paused ? (
                      <CirclePlay aria-hidden="true" size={16} />
                    ) : (
                      <CirclePause aria-hidden="true" size={16} />
                    )}
                    {t(paused ? "scraping.resume" : "scraping.pause")}
                  </Button>
                  <Button
                    disabled={control.isPending}
                    onClick={() => executeControl(draining ? "cancel_drain" : "drain")}
                    variant="secondary"
                  >
                    <ListRestart aria-hidden="true" size={16} />
                    {t(draining ? "scraping.cancelDrain" : "scraping.drain")}
                  </Button>
                  <Button
                    disabled={control.isPending}
                    onClick={() => executeControl("stop")}
                    variant="danger"
                  >
                    <OctagonX aria-hidden="true" size={16} />
                    {t("scraping.stop")}
                  </Button>
                </>
              )}
            </div>
          </div>

          <div className="metric-grid metric-grid--hero">
            <MetricCard
              detail={t("scraping.itemsEpisodes", {
                episodes: snapshot.data.queue.episodes,
                items: snapshot.data.queue.items,
              })}
              label={t("scraping.ready")}
              tone="live"
              value={formatMetric(snapshot.data.queue.ready, "number")}
            />
            <MetricCard
              detail={t("scraping.oldest", {
                age: ago(snapshot.data.queue.oldest_ready_at),
              })}
              label={t("scraping.backlog")}
              tone={
                snapshot.data.queue.high_watermark > 0 &&
                snapshot.data.queue.ready >= snapshot.data.queue.high_watermark
                  ? "warning"
                  : "default"
              }
              value={formatMetric(
                snapshot.data.queue.items + snapshot.data.queue.episodes,
                "number",
              )}
            />
            <MetricCard
              detail={t("scraping.last24h")}
              label={t("scraping.processed")}
              value={formatMetric(snapshot.data.processed_24h, "number")}
            />
            <MetricCard
              detail={`${snapshot.data.failed_24h} ${t("scraping.failed")}`}
              label={t("scraping.torrentsFound")}
              value={formatMetric(snapshot.data.torrents_found_24h, "number")}
            />
          </div>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">{t("scraping.liveAnalytics")}</span>
                <h2>{t("scraping.scrapers")}</h2>
              </div>
            </header>
            <div className="scraper-grid">
              {names.map((name) => (
                <div key={name}>
                  <strong>{name}</strong>
                  <dl>
                    <div>
                      <dt>{t("scraping.requestRate")}</dt>
                      <dd>
                        {formatMetric(
                          counterRate(frames, "comet_scraper_requests_total", {
                            scraper: name,
                          }),
                          "rate",
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>{t("scraping.resultRate")}</dt>
                      <dd>
                        {formatMetric(
                          counterRate(frames, "comet_scraper_torrents_total", {
                            scraper: name,
                          }),
                          "rate",
                        )}
                      </dd>
                    </div>
                    <div>
                      <dt>{t("scraping.p95Latency")}</dt>
                      <dd>
                        {formatMetric(
                          histogramQuantile(
                            frames,
                            "comet_scraper_request_duration_seconds",
                            0.95,
                            { scraper: name },
                          ),
                          "seconds",
                        )}
                      </dd>
                    </div>
                  </dl>
                </div>
              ))}
              {names.length === 0 ? (
                <p className="empty-state">{t("scraping.noScraperActivity")}</p>
              ) : null}
            </div>
          </article>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">{t("scraping.queueEyebrow")}</span>
                <h2>{t("scraping.queueTitle")}</h2>
              </div>
              <Button
                disabled={snapshot.data.queue.dead === 0 || requeue.isPending}
                onClick={() => requeue.mutate()}
                variant="secondary"
              >
                <ListRestart aria-hidden="true" size={16} />
                {t("scraping.requeueDead", { count: snapshot.data.queue.dead })}
              </Button>
            </header>
            <div className="operations-toolbar">
              <Select
                label={t("scraping.entity")}
                onValueChange={(value) => setKind(value as QueueKind)}
                value={kind}
              >
                <option value="item">{t("scraping.mediaItems")}</option>
                <option value="episode">{t("scraping.episodes")}</option>
              </Select>
              <Select label={t("scraping.status")} onValueChange={setStatus} value={status}>
                {statuses.map((value) => (
                  <option key={value || "all"} value={value}>
                    {value || t("scraping.allStatuses")}
                  </option>
                ))}
              </Select>
              <Input
                label={t("scraping.search")}
                onChange={(event) => setSearch(event.target.value)}
                placeholder={t("scraping.searchPlaceholder")}
                type="search"
                value={search}
              />
              <Search aria-hidden="true" className="operations-toolbar__icon" size={18} />
            </div>
            {queue.isPending ? <Skeleton label={t("scraping.loadingQueue")} lines={5} /> : null}
            <div className="operations-table-wrap">
              <table className="operations-table">
                <thead>
                  <tr>
                    <th>{t("scraping.media")}</th>
                    <th>{t("scraping.status")}</th>
                    <th>{t("scraping.priority")}</th>
                    <th>{t("scraping.failures")}</th>
                    <th>{t("scraping.lastScraped")}</th>
                    <th>{t("scraping.nextRetry")}</th>
                    <th>{t("scraping.results")}</th>
                    <th>{t("scraping.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {queue.data?.items.map((entry) => (
                    <tr key={entry.id}>
                      <td data-label={t("scraping.media")}>
                        <strong>{entry.title}</strong>
                        <small>
                          {entry.id}
                          {entry.season === null
                            ? ` · ${entry.year}`
                            : ` · S${entry.season}E${entry.episode}`}
                        </small>
                      </td>
                      <td data-label={t("scraping.status")}>
                        <span className={`status-pill status-pill--${entry.status}`}>
                          {entry.status}
                        </span>
                      </td>
                      <td data-label={t("scraping.priority")}>{entry.priority_score.toFixed(1)}</td>
                      <td data-label={t("scraping.failures")}>{entry.consecutive_failures}</td>
                      <td data-label={t("scraping.lastScraped")}>{ago(entry.last_scraped_at)}</td>
                      <td data-label={t("scraping.nextRetry")}>
                        {entry.next_retry_at === null
                          ? "—"
                          : new Date(entry.next_retry_at * 1_000).toLocaleString()}
                      </td>
                      <td data-label={t("scraping.results")}>{entry.total_torrents_found}</td>
                      <td data-label={t("scraping.actions")}>
                        <QueueActions
                          entry={entry}
                          pending={queueAction.isPending}
                          run={(selected, action) =>
                            queueAction.mutate({ action, entry: selected })
                          }
                          t={t}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {queue.data?.items.length === 0 ? (
                <p className="empty-state">{t("scraping.noQueueEntries")}</p>
              ) : null}
            </div>
          </article>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">{t("scraping.runsEyebrow")}</span>
                <h2>{t("scraping.runsTitle")}</h2>
              </div>
            </header>
            <div className="operations-list">
              {runs.data?.items.map((run) => (
                <div key={run.run_id}>
                  <span>
                    <strong>{run.status}</strong>
                    <small>
                      {new Date(run.started_at * 1_000).toLocaleString()} · {run.worker_count}{" "}
                      {t("scraping.workers")}
                    </small>
                  </span>
                  <span>
                    <strong>
                      {run.torrents_found} {t("scraping.torrents")}
                    </strong>
                    <small>
                      {run.processed} {t("scraping.processed").toLocaleLowerCase()} · {run.failed}{" "}
                      {t("scraping.failed")}
                    </small>
                  </span>
                </div>
              ))}
              {runs.data?.items.length === 0 ? (
                <p className="empty-state">{t("scraping.noRuns")}</p>
              ) : null}
            </div>
          </article>
        </>
      ) : null}
    </section>
  );
}
