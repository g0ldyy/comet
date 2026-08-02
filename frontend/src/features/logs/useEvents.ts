import { type InfiniteData, useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import type { OperationalEventData, OperationalEventPageData } from "../../api/generated/contracts";
import { clearAdminSession } from "../auth/AdminBoundary";
import { type EventFilters, eventQuery, eventStreamUrl, getEvents, parseLiveEvent } from "./api";

const MAX_LIVE_EVENTS = 1_000;

export function mergeEvents(
  history: readonly OperationalEventData[],
  live: readonly OperationalEventData[],
): OperationalEventData[] {
  const byId = new Map<number, OperationalEventData>();
  for (const item of [...live, ...history]) byId.set(item.id, item);
  return [...byId.values()].sort((left, right) => right.id - left.id);
}

export function useEvents(filters: EventFilters, paused: boolean) {
  const filterQuery = eventQuery(filters);
  const history = useInfiniteQuery<
    OperationalEventPageData,
    Error,
    InfiniteData<OperationalEventPageData, number | undefined>,
    readonly ["events", string],
    number | undefined
  >({
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) => getEvents(filterQuery, pageParam),
    queryKey: ["events", filterQuery],
  });
  const [liveState, setLiveState] = useState({
    filterQuery,
    items: [] as OperationalEventData[],
  });
  const [streamState, setStreamState] = useState({
    filterQuery,
    live: false,
  });
  const live = liveState.filterQuery === filterQuery ? liveState.items : [];
  const cursor = useRef({ filterQuery, id: 0 });
  if (cursor.current.filterQuery !== filterQuery) {
    cursor.current = { filterQuery, id: 0 };
  }
  const historical = useMemo(
    () => history.data?.pages.flatMap(({ items }) => items) ?? [],
    [history.data],
  );
  const items = useMemo(() => mergeEvents(historical, live), [historical, live]);
  cursor.current.id = Math.max(cursor.current.id, items[0]?.id ?? 0);

  useEffect(() => {
    if (paused || !history.isSuccess) return;
    setStreamState({ filterQuery, live: false });
    const stream = new EventSource(eventStreamUrl(filterQuery, cursor.current.id));
    stream.addEventListener("open", () => setStreamState({ filterQuery, live: true }));
    stream.addEventListener("error", () => setStreamState({ filterQuery, live: false }));
    stream.addEventListener("operational_event", (message) => {
      try {
        const item = parseLiveEvent((message as MessageEvent<string>).data);
        if (item.id <= cursor.current.id) return;
        cursor.current.id = item.id;
        setLiveState((current) => ({
          filterQuery,
          items: [item, ...(current.filterQuery === filterQuery ? current.items : [])].slice(
            0,
            MAX_LIVE_EVENTS,
          ),
        }));
      } catch {
        stream.close();
      }
    });
    stream.addEventListener("session_expired", () => {
      stream.close();
      void clearAdminSession();
    });
    return () => stream.close();
  }, [filterQuery, history.isSuccess, paused]);

  return {
    ...history,
    droppedEvents: history.data?.pages[0]?.dropped_events ?? 0,
    filterQuery,
    items,
    streamLive: streamState.filterQuery === filterQuery && streamState.live,
  };
}
