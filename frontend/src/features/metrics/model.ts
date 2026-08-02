import type { CurrentMetricsData, MetricSampleData } from "../../api/generated/contracts";

export interface MetricFrame {
  at: number;
  samples: ReadonlyArray<MetricSampleData>;
}

export function sampleTotal(
  samples: ReadonlyArray<MetricSampleData>,
  name: string,
  labels: Readonly<Record<string, string>> = {},
): number {
  let total = 0;
  for (const sample of samples) {
    if (
      sample.name === name &&
      Object.entries(labels).every(([key, value]) => sample.labels[key] === value)
    ) {
      total += sample.value;
    }
  }
  return total;
}

function labelKey(labels: Readonly<Record<string, string>>): string {
  return Object.entries(labels)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}\u0000${value}`)
    .join("\u0001");
}

function lifetimeCounterRate(
  frame: MetricFrame,
  name: string,
  predicate: (sample: MetricSampleData) => boolean,
): number | null {
  const createdName = name.endsWith("_total")
    ? `${name.slice(0, -"_total".length)}_created`
    : `${name}_created`;
  const createdAt = new Map(
    frame.samples
      .filter((sample) => sample.name === createdName && predicate(sample))
      .map((sample) => [labelKey(sample.labels), sample.value]),
  );
  let rate = 0;
  let matched = false;
  for (const sample of frame.samples) {
    if (sample.name !== name || !predicate(sample)) continue;
    const startedAt = createdAt.get(labelKey(sample.labels));
    if (startedAt === undefined || startedAt >= frame.at) continue;
    rate += sample.value / (frame.at - startedAt);
    matched = true;
  }
  return matched ? rate : null;
}

export function counterRate(
  frames: ReadonlyArray<MetricFrame>,
  name: string,
  labels: Readonly<Record<string, string>> = {},
): number | null {
  const first = frames[0];
  const last = frames.at(-1);
  if (first === undefined || last === undefined) return null;
  if (first.at === last.at) {
    return lifetimeCounterRate(last, name, (sample) =>
      Object.entries(labels).every(([key, value]) => sample.labels[key] === value),
    );
  }
  const change = sampleTotal(last.samples, name, labels) - sampleTotal(first.samples, name, labels);
  return Math.max(0, change) / (last.at - first.at);
}

export function counterRateWhere(
  frames: ReadonlyArray<MetricFrame>,
  name: string,
  predicate: (sample: MetricSampleData) => boolean,
): number | null {
  const first = frames[0];
  const last = frames.at(-1);
  if (first === undefined || last === undefined) return null;
  if (first.at === last.at) return lifetimeCounterRate(last, name, predicate);
  const total = (samples: ReadonlyArray<MetricSampleData>) =>
    samples
      .filter((sample) => sample.name === name && predicate(sample))
      .reduce((sum, sample) => sum + sample.value, 0);
  return Math.max(0, total(last.samples) - total(first.samples)) / (last.at - first.at);
}

export function ratioRate(
  frames: ReadonlyArray<MetricFrame>,
  numerator: (samples: ReadonlyArray<MetricSampleData>) => number,
  denominator: (samples: ReadonlyArray<MetricSampleData>) => number,
): number | null {
  const first = frames[0];
  const last = frames.at(-1);
  if (first === undefined || last === undefined) return null;
  if (first.at === last.at) {
    const denominatorTotal = denominator(last.samples);
    return denominatorTotal > 0 ? Math.max(0, numerator(last.samples)) / denominatorTotal : null;
  }
  const numeratorChange = numerator(last.samples) - numerator(first.samples);
  const denominatorChange = denominator(last.samples) - denominator(first.samples);
  return denominatorChange > 0 ? Math.max(0, numeratorChange) / denominatorChange : null;
}

export function histogramQuantile(
  frames: ReadonlyArray<MetricFrame>,
  name: string,
  quantile: number,
  labels: Readonly<Record<string, string>> = {},
): number | null {
  const first = frames[0];
  const last = frames.at(-1);
  if (first === undefined || last === undefined) return null;
  const buckets = new Map<number, number>();
  for (const sample of last.samples) {
    if (
      sample.name !== `${name}_bucket` ||
      !Object.entries(labels).every(([key, value]) => sample.labels[key] === value)
    )
      continue;
    const boundary = Number(sample.labels.le);
    if (Number.isFinite(boundary)) {
      buckets.set(boundary, (buckets.get(boundary) ?? 0) + sample.value);
    }
  }
  if (first.at !== last.at) {
    for (const sample of first.samples) {
      if (
        sample.name !== `${name}_bucket` ||
        !Object.entries(labels).every(([key, value]) => sample.labels[key] === value)
      )
        continue;
      const boundary = Number(sample.labels.le);
      if (Number.isFinite(boundary)) {
        buckets.set(boundary, (buckets.get(boundary) ?? 0) - sample.value);
      }
    }
  }
  const sorted = [...buckets].sort(([left], [right]) => left - right);
  const count = sorted.at(-1)?.[1] ?? 0;
  if (count <= 0) return null;
  const target = count * quantile;
  return sorted.find(([, value]) => value >= target)?.[0] ?? null;
}

export function appendMetricFrame(
  frames: ReadonlyArray<MetricFrame>,
  snapshot: CurrentMetricsData,
  maximum = 120,
): MetricFrame[] {
  if (frames.at(-1)?.at === snapshot.collected_at) return [...frames];
  return [...frames, { at: snapshot.collected_at, samples: snapshot.samples }].slice(-maximum);
}

export function formatMetric(
  value: number | null,
  style: "bytes" | "bytesRate" | "number" | "percent" | "rate" | "seconds",
): string {
  if (value === null) return "—";
  if (style === "percent") return `${(value * 100).toFixed(1)}%`;
  if (style === "seconds") {
    return value < 1 ? `${Math.round(value * 1_000)} ms` : `${value.toFixed(2)} s`;
  }
  if (style === "bytes" || style === "bytesRate") {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let scaled = value;
    let unit = 0;
    while (scaled >= 1_024 && unit < units.length - 1) {
      scaled /= 1_024;
      unit += 1;
    }
    const formatted = `${scaled.toFixed(scaled >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
    return style === "bytesRate" ? `${formatted}/s` : formatted;
  }
  const formatted = new Intl.NumberFormat(undefined, {
    maximumFractionDigits: value < 10 ? 2 : 0,
  }).format(value);
  return style === "rate" ? `${formatted}/s` : formatted;
}
