import { Activity, BarChart3, Waves } from "lucide-react";
import { useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  type TooltipContentProps,
  XAxis,
  YAxis,
} from "recharts";
import type { StreamActivityData } from "../../api/generated/contracts";
import { Skeleton } from "../../components/ui/Skeleton";
import { formatMetric } from "../metrics/model";

export type StreamActivityRange = StreamActivityData["selection"];

type ChartMode = "concurrency" | "outcomes" | "volume";

interface ActivityLabels {
  active: string;
  auto: string;
  completed: string;
  concurrency: string;
  empty: string;
  failed: string;
  interrupted: string;
  outcomes: string;
  range: string;
  title: string;
  volume: string;
}

const ranges: ReadonlyArray<StreamActivityRange> = ["auto", "15m", "1h", "6h", "24h", "7d"];

function bucketLabel(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3_600) return `${seconds / 60}m`;
  return `${seconds / 3_600}h`;
}

function timeLabel(timestamp: number, span: number): string {
  const date = new Date(timestamp * 1_000);
  return span > 24 * 60 * 60
    ? date.toLocaleString(undefined, { day: "numeric", hour: "2-digit", month: "short" })
    : date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

function intervalLabel(timestamp: number, bucketSeconds: number): string {
  const options: Intl.DateTimeFormatOptions = {
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    month: "short",
  };
  const start = new Date(timestamp * 1_000).toLocaleString(undefined, options);
  const end = new Date((timestamp + bucketSeconds) * 1_000).toLocaleString(undefined, options);
  return `${start} – ${end}`;
}

function ActivityTooltip({
  active,
  data,
  labels,
  mode,
  payload,
}: TooltipContentProps & {
  data: StreamActivityData;
  labels: ActivityLabels;
  mode: ChartMode;
}) {
  if (!active || payload.length === 0) return null;
  const bucket = payload[0]?.payload as StreamActivityData["buckets"][number] | undefined;
  if (!bucket) return null;
  const values =
    mode === "volume"
      ? [
          {
            color: "var(--activity-color)",
            label: labels.volume,
            value: formatMetric(bucket.bytes_transferred, "bytes"),
          },
        ]
      : mode === "concurrency"
        ? [
            {
              color: "var(--activity-color)",
              label: labels.concurrency,
              value: formatMetric(bucket.peak_active, "number"),
            },
          ]
        : [
            {
              color: "var(--color-success)",
              label: labels.completed,
              value: formatMetric(bucket.completed, "number"),
            },
            {
              color: "var(--color-danger)",
              label: labels.failed,
              value: formatMetric(bucket.failed, "number"),
            },
            {
              color: "var(--color-warning)",
              label: labels.interrupted,
              value: formatMetric(bucket.interrupted, "number"),
            },
            {
              color: "var(--activity-color)",
              label: labels.active,
              value: formatMetric(bucket.active, "number"),
            },
          ];

  return (
    <div className="metric-chart__tooltip stream-activity__tooltip">
      <span>{intervalLabel(bucket.started_at, data.bucket_seconds)}</span>
      {values.map((item) => (
        <div key={item.label}>
          <i style={{ background: item.color }} />
          <span>{item.label}</span>
          <strong>{item.value}</strong>
        </div>
      ))}
    </div>
  );
}

export function StreamActivityChart({
  data,
  isPending,
  labels,
  onRangeChange,
  range,
  tone,
}: {
  data: StreamActivityData | undefined;
  isPending: boolean;
  labels: ActivityLabels;
  onRangeChange: (range: StreamActivityRange) => void;
  range: StreamActivityRange;
  tone: "proxy" | "usenet";
}) {
  const [mode, setMode] = useState<ChartMode>("volume");
  const totals = data?.buckets.reduce(
    (result, bucket) => ({
      active: result.active + bucket.active,
      bytes: result.bytes + bucket.bytes_transferred,
      completed: result.completed + bucket.completed,
      failed: result.failed + bucket.failed,
      interrupted: result.interrupted + bucket.interrupted,
    }),
    { active: 0, bytes: 0, completed: 0, failed: 0, interrupted: 0 },
  );
  const hasConcurrency = data?.buckets.some((bucket) => bucket.peak_active !== null) === true;
  const concurrencyPoints =
    data?.buckets.reduce((count, bucket) => count + Number(bucket.peak_active !== null), 0) ?? 0;
  const hasActivity =
    totals !== undefined &&
    (totals.bytes + totals.completed + totals.failed + totals.interrupted + totals.active > 0 ||
      data?.buckets.some((bucket) => (bucket.peak_active ?? 0) > 0) === true);
  const span = data ? data.window_ended_at - data.window_started_at : 0;
  const chartData = data
    ? data.buckets.map((bucket) => ({
        ...bucket,
        plotted_at:
          mode === "concurrency"
            ? bucket.started_at
            : bucket.started_at +
              Math.min(data.bucket_seconds, data.window_ended_at - bucket.started_at) / 2,
      }))
    : [];

  return (
    <article className={`dashboard-panel stream-activity stream-activity--${tone}`}>
      <header>
        <div>
          <span className="eyebrow">
            <Waves aria-hidden="true" size={15} />
            {data
              ? `${timeLabel(data.window_started_at, span)} – ${timeLabel(data.window_ended_at, span)} · ${bucketLabel(data.bucket_seconds)}`
              : labels.volume}
          </span>
          <h2>{labels.title}</h2>
        </div>
        <fieldset aria-label={labels.range} className="chart-range">
          <legend className="visually-hidden">{labels.range}</legend>
          {ranges.map((item) => (
            <button
              aria-pressed={range === item}
              className={
                range === item
                  ? "chart-range__option chart-range__option--active"
                  : "chart-range__option"
              }
              key={item}
              onClick={() => onRangeChange(item)}
              type="button"
            >
              {item === "auto" ? labels.auto : item}
            </button>
          ))}
        </fieldset>
      </header>

      <div className="stream-activity__toolbar">
        <div className="stream-activity__modes">
          <button aria-pressed={mode === "volume"} onClick={() => setMode("volume")} type="button">
            <Waves aria-hidden="true" size={14} />
            {labels.volume}
          </button>
          <button
            aria-pressed={mode === "outcomes"}
            onClick={() => setMode("outcomes")}
            type="button"
          >
            <BarChart3 aria-hidden="true" size={14} />
            {labels.outcomes}
          </button>
          {hasConcurrency ? (
            <button
              aria-pressed={mode === "concurrency"}
              onClick={() => setMode("concurrency")}
              type="button"
            >
              <Activity aria-hidden="true" size={14} />
              {labels.concurrency}
            </button>
          ) : null}
        </div>
        {totals ? (
          <dl className="stream-activity__summary">
            <div>
              <dt>{labels.volume}</dt>
              <dd>{formatMetric(totals.bytes, "bytes")}</dd>
            </div>
            <div>
              <dt>{labels.completed}</dt>
              <dd>{formatMetric(totals.completed, "number")}</dd>
            </div>
            <div className={totals.failed > 0 ? "stream-activity__summary--danger" : undefined}>
              <dt>{labels.failed}</dt>
              <dd>{formatMetric(totals.failed, "number")}</dd>
            </div>
            {totals.interrupted > 0 ? (
              <div className="stream-activity__summary--warning">
                <dt>{labels.interrupted}</dt>
                <dd>{formatMetric(totals.interrupted, "number")}</dd>
              </div>
            ) : null}
            {totals.active > 0 ? (
              <div className="stream-activity__summary--live">
                <dt>{labels.active}</dt>
                <dd>{formatMetric(totals.active, "number")}</dd>
              </div>
            ) : null}
          </dl>
        ) : null}
      </div>

      <div
        aria-label={labels.title}
        aria-busy={isPending}
        className="stream-activity__canvas"
        role="img"
      >
        {isPending ? (
          <Skeleton label={labels.title} lines={5} />
        ) : !data || !hasActivity ? (
          <p className="empty-state">{labels.empty}</p>
        ) : (
          <ResponsiveContainer height="100%" width="100%">
            <ComposedChart
              accessibilityLayer={false}
              data={chartData}
              margin={{ bottom: 4, left: 4, right: 12, top: 8 }}
            >
              <defs>
                <linearGradient id={`activity-${tone}`} x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor="var(--activity-color)" stopOpacity={0.9} />
                  <stop offset="100%" stopColor="var(--activity-color)" stopOpacity={0.28} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="plotted_at"
                domain={[data.window_started_at, data.window_ended_at]}
                minTickGap={44}
                scale="time"
                tickFormatter={(value: number) => timeLabel(value, span)}
                tickMargin={10}
                type="number"
              />
              <YAxis
                allowDecimals={false}
                tickFormatter={(value: number) =>
                  formatMetric(value, mode === "volume" ? "bytes" : "number")
                }
                tickMargin={8}
                width={76}
              />
              <Tooltip
                content={(props) => (
                  <ActivityTooltip {...props} data={data} labels={labels} mode={mode} />
                )}
                cursor={{ fill: "var(--color-surface-3)" }}
                isAnimationActive={false}
              />
              {mode === "volume" ? (
                <Bar
                  dataKey="bytes_transferred"
                  fill={`url(#activity-${tone})`}
                  maxBarSize={42}
                  minPointSize={2}
                  name={labels.volume}
                  radius={[5, 5, 1, 1]}
                />
              ) : null}
              {mode === "outcomes" ? (
                <>
                  <Bar
                    dataKey="completed"
                    fill="var(--color-success)"
                    name={labels.completed}
                    stackId="outcomes"
                  />
                  <Bar
                    dataKey="failed"
                    fill="var(--color-danger)"
                    name={labels.failed}
                    stackId="outcomes"
                  />
                  <Bar
                    dataKey="interrupted"
                    fill="var(--color-warning)"
                    name={labels.interrupted}
                    stackId="outcomes"
                  />
                  <Bar
                    dataKey="active"
                    fill="var(--activity-color)"
                    name={labels.active}
                    radius={[5, 5, 0, 0]}
                    stackId="outcomes"
                  />
                </>
              ) : null}
              {mode === "concurrency" ? (
                <Line
                  connectNulls
                  dataKey="peak_active"
                  dot={concurrencyPoints === 1}
                  name={labels.concurrency}
                  stroke="var(--activity-color)"
                  strokeWidth={2.5}
                  type="stepAfter"
                />
              ) : null}
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </article>
  );
}
