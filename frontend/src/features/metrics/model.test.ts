import { describe, expect, it } from "vitest";
import type { CurrentMetricsData, MetricSampleData } from "../../api/generated/contracts";
import {
  appendMetricFrame,
  counterRate,
  formatMetric,
  histogramQuantile,
  type MetricFrame,
  ratioRate,
  sampleTotal,
} from "./model";

function sample(
  name: string,
  value: number,
  labels: Record<string, string> = {},
): MetricSampleData {
  return { labels, name, value };
}

describe("metric model", () => {
  const frames: readonly [MetricFrame, MetricFrame] = [
    {
      at: 100,
      samples: [
        sample("requests_total", 5),
        sample("requests_total", 4, { outcome: "error" }),
        sample("duration_bucket", 5, { le: "1" }),
        sample("duration_bucket", 10, { le: "2" }),
      ],
    },
    {
      at: 110,
      samples: [
        sample("requests_total", 25),
        sample("requests_total", 9, { outcome: "error" }),
        sample("duration_bucket", 8, { le: "1" }),
        sample("duration_bucket", 20, { le: "2" }),
      ],
    },
  ];

  it("aggregates labels and computes counter rates", () => {
    expect(sampleTotal(frames[1].samples, "requests_total")).toBe(34);
    expect(counterRate(frames, "requests_total")).toBe(2.5);
    expect(counterRate(frames, "requests_total", { outcome: "error" })).toBe(0.5);
  });

  it("keeps histogram quantiles isolated by scraper label", () => {
    const frames = [
      {
        at: 10,
        samples: [
          {
            labels: { le: "1", scraper: "fast" },
            name: "duration_bucket",
            value: 2,
          },
          {
            labels: { le: "10", scraper: "slow" },
            name: "duration_bucket",
            value: 4,
          },
        ],
      },
      {
        at: 20,
        samples: [
          {
            labels: { le: "1", scraper: "fast" },
            name: "duration_bucket",
            value: 4,
          },
          {
            labels: { le: "10", scraper: "slow" },
            name: "duration_bucket",
            value: 8,
          },
        ],
      },
    ];

    expect(histogramQuantile(frames, "duration", 0.95, { scraper: "fast" })).toBe(1);
    expect(histogramQuantile(frames, "duration", 0.95, { scraper: "slow" })).toBe(10);
  });

  it("computes ratios and histogram quantiles from the sampled window", () => {
    expect(
      ratioRate(
        frames,
        (samples) => sampleTotal(samples, "requests_total", { outcome: "error" }),
        (samples) => sampleTotal(samples, "requests_total"),
      ),
    ).toBeCloseTo(0.2);
    expect(histogramQuantile(frames, "duration", 0.5)).toBe(2);
  });

  it("uses cumulative Prometheus data for the first immediate frame", () => {
    const frame: MetricFrame = {
      at: 110,
      samples: [
        sample("requests_total", 20),
        sample("requests_created", 100),
        sample("requests_total", 5, { outcome: "error" }),
        sample("requests_created", 100, { outcome: "error" }),
        sample("duration_bucket", 8, { le: "1" }),
        sample("duration_bucket", 10, { le: "2" }),
      ],
    };

    expect(counterRate([frame], "requests_total")).toBe(2.5);
    expect(
      ratioRate(
        [frame],
        (samples) => sampleTotal(samples, "requests_total", { outcome: "error" }),
        (samples) => sampleTotal(samples, "requests_total"),
      ),
    ).toBe(0.2);
    expect(histogramQuantile([frame], "duration", 0.5)).toBe(1);
  });

  it("bounds session history and formats throughput", () => {
    const snapshot = (collectedAt: number): CurrentMetricsData => ({
      collected_at: collectedAt,
      history_available: false,
      history_ranges: [],
      samples: [],
    });
    const history = appendMetricFrame(appendMetricFrame([], snapshot(1), 2), snapshot(2), 2);
    expect(appendMetricFrame(history, snapshot(2), 2)).toEqual(history);
    expect(appendMetricFrame(history, snapshot(3), 2).map(({ at }) => at)).toEqual([2, 3]);
    expect(formatMetric(2_048, "bytesRate")).toBe("2.0 KiB/s");
  });
});
