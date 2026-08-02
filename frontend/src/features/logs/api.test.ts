import { describe, expect, it } from "vitest";
import type { OperationalEventData } from "../../api/generated/contracts";
import { emptyEventFilters, eventQuery, parseLiveEvent } from "./api";
import { mergeEvents } from "./useEvents";

function event(id: number): OperationalEventData {
  return {
    category: "SCRAPER",
    connection_id: null,
    created_at: 1_780_000_000 + id,
    details: { result_count: id },
    error_code: null,
    event: "search.completed",
    id,
    instance_id: "a".repeat(32),
    level: "INFO",
    media_type: "movie",
    message: "Search completed",
    outcome: "ok",
    process_id: 42,
    provider_name: null,
    request_id: "b".repeat(32),
    role: "web_worker",
    run_id: null,
  };
}

describe("operational event transport", () => {
  it("serializes only active filters with exact API names", () => {
    expect(
      eventQuery({
        ...emptyEventFilters,
        category: "SCRAPER",
        endedAt: "2026-07-31T12:30:00Z",
        providerName: "torrentio",
        startedAt: "2026-07-31T12:00:00Z",
      }),
    ).toBe("category=SCRAPER&provider_name=torrentio&started_at=1785499200&ended_at=1785501000");
  });

  it("validates the live JSON boundary", () => {
    expect(parseLiveEvent(JSON.stringify(event(7)))).toEqual(event(7));
    expect(() =>
      parseLiveEvent(JSON.stringify({ ...event(7), details: { nested: { value: 1 } } })),
    ).toThrow();
  });

  it("merges resumed history and live events once in descending order", () => {
    expect(mergeEvents([event(3), event(2), event(1)], [event(5), event(4), event(3)])).toEqual([
      event(5),
      event(4),
      event(3),
      event(2),
      event(1),
    ]);
  });
});
