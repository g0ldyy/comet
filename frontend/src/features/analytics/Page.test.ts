import { describe, expect, it } from "vitest";
import type { MetricFrame } from "../metrics/model";
import { liveChartData } from "./Page";

describe("analytics live chart", () => {
  it("starts counter series at the first measurable point", () => {
    const frames: MetricFrame[] = [
      {
        at: 100,
        samples: [
          {
            labels: { outcome: "error" },
            name: "comet_database_operations_total",
            value: 10,
          },
          {
            labels: { outcome: "success" },
            name: "comet_database_operations_total",
            value: 1_000,
          },
        ],
      },
      {
        at: 105,
        samples: [
          {
            labels: { outcome: "error" },
            name: "comet_database_operations_total",
            value: 15,
          },
          {
            labels: { outcome: "success" },
            name: "comet_database_operations_total",
            value: 2_000,
          },
        ],
      },
    ];

    expect(liveChartData(frames, "database_errors")).toEqual([
      expect.objectContaining({ value: 1 }),
    ]);
  });

  it("keeps the first point for immediately measurable series", () => {
    const frames: MetricFrame[] = [
      {
        at: 100,
        samples: [
          {
            labels: {},
            name: "comet_background_scraper_queue_items",
            value: 4,
          },
        ],
      },
    ];

    expect(liveChartData(frames, "background_queue")).toEqual([
      expect.objectContaining({ value: 4 }),
    ]);
  });
});
