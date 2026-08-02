import * as Popover from "@radix-ui/react-popover";
import {
  ChevronDown,
  Download,
  FileJson,
  FileText,
  Pause,
  Play,
  RadioTower,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { OperationalEventData } from "../../api/generated/contracts";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { emptyEventFilters, logExportUrl } from "./api";
import { EventDetail } from "./EventDetail";
import { EventFiltersBar } from "./EventFiltersBar";
import { EventList } from "./EventList";
import { useEvents } from "./useEvents";

export function EventWorkspace() {
  const { t } = useTranslation();
  const [filters, setFilters] = useState(emptyEventFilters);
  const [paused, setPaused] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [selected, setSelected] = useState<OperationalEventData | null>(null);
  const fetchingOlder = useRef<string | null>(null);
  useEffect(() => {
    const timeout = window.setTimeout(() => setSearchQuery(search.trim()), 250);
    return () => window.clearTimeout(timeout);
  }, [search]);
  const queryFilters = useMemo(() => ({ ...filters, search: searchQuery }), [filters, searchQuery]);
  const events = useEvents(queryFilters, paused);
  const activeFilterCount = Object.entries(filters).filter(
    ([key, value]) => key !== "search" && Boolean(value),
  ).length;

  const loadOlderEvents = async () => {
    if (fetchingOlder.current === events.filterQuery || !events.hasNextPage) return;
    const filterQuery = events.filterQuery;
    fetchingOlder.current = filterQuery;
    try {
      await events.fetchNextPage();
    } finally {
      if (fetchingOlder.current === filterQuery) fetchingOlder.current = null;
    }
  };
  const related = useMemo(
    () =>
      selected
        ? events.items.filter(
            (event) =>
              event.id !== selected.id &&
              ((selected.request_id !== null && event.request_id === selected.request_id) ||
                (selected.run_id !== null && event.run_id === selected.run_id) ||
                (selected.connection_id !== null &&
                  event.connection_id === selected.connection_id)),
          )
        : [],
    [events.items, selected],
  );
  return (
    <section aria-labelledby="logs-title" className="section-page event-page">
      <header className="section-page__header">
        <div>
          <span className="eyebrow">
            <RadioTower aria-hidden="true" size={15} />
            {t(paused ? "events.paused" : events.streamLive ? "events.live" : "events.connecting")}
          </span>
          <h1 id="logs-title">{t("nav.logs")}</h1>
        </div>
        <div className="event-actions">
          <Button onClick={() => setPaused((current) => !current)} variant="secondary">
            {paused ? (
              <Play aria-hidden="true" size={17} />
            ) : (
              <Pause aria-hidden="true" size={17} />
            )}
            {t(paused ? "events.resume" : "events.pause")}
          </Button>
          <Popover.Root>
            <Popover.Trigger asChild>
              <Button variant="secondary">
                <Download aria-hidden="true" size={17} />
                {t("events.export")}
                <ChevronDown aria-hidden="true" size={15} />
              </Button>
            </Popover.Trigger>
            <Popover.Portal>
              <Popover.Content align="end" className="event-export-menu" sideOffset={6}>
                <Popover.Close asChild>
                  <a href={logExportUrl(queryFilters, "jsonl")}>
                    <FileJson aria-hidden="true" size={16} />
                    {t("events.exportJsonl")}
                  </a>
                </Popover.Close>
                <Popover.Close asChild>
                  <a href={logExportUrl(queryFilters, "text")}>
                    <FileText aria-hidden="true" size={16} />
                    {t("events.exportText")}
                  </a>
                </Popover.Close>
              </Popover.Content>
            </Popover.Portal>
          </Popover.Root>
        </div>
      </header>
      <div className="event-console">
        <div className="event-console__toolbar">
          <div className="event-search">
            <Search aria-hidden="true" size={17} />
            <Input
              label={t("events.searchLabel")}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={t("events.searchPlaceholder")}
              value={search}
            />
            {search ? (
              <Button
                aria-label={t("events.clearSearch")}
                className="event-search__clear"
                onClick={() => setSearch("")}
                variant="ghost"
              >
                <X aria-hidden="true" size={15} />
              </Button>
            ) : null}
          </div>
          <Button
            className={
              filtersOpen
                ? "event-filter-toggle event-filter-toggle--active"
                : "event-filter-toggle"
            }
            onClick={() => setFiltersOpen((current) => !current)}
            variant="secondary"
          >
            <SlidersHorizontal aria-hidden="true" size={16} />
            {t(filtersOpen ? "events.filters.hideFilters" : "events.filters.showFilters")}
            {activeFilterCount > 0 ? <span>{activeFilterCount}</span> : null}
          </Button>
          <Checkbox
            checked={wrap}
            label={t("events.wrap")}
            onChange={(event) => setWrap(event.target.checked)}
          />
        </div>
        {filtersOpen ? (
          <div className="event-filter-panel">
            <EventFiltersBar filters={filters} onChange={setFilters} />
            {activeFilterCount > 0 ? (
              <Button onClick={() => setFilters(emptyEventFilters)} variant="ghost">
                <X aria-hidden="true" size={15} />
                {t("events.filters.clear")}
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
      {events.droppedEvents > 0 ? (
        <Alert tone="warning">
          {t("events.dropped", { count: events.droppedEvents.toLocaleString() })}
        </Alert>
      ) : null}
      {events.isPending ? <Skeleton label={t("events.loading")} lines={8} /> : null}
      {events.isError ? (
        <Alert title={t("events.errorTitle")} tone="danger">
          <ApiErrorDetails error={events.error} fallback={t("events.errorDescription")} />
        </Alert>
      ) : null}
      {events.isSuccess ? (
        <>
          <div className="event-results-summary">
            <strong>{t("events.results", { count: events.items.length })}</strong>
            <span>{t("events.newestFirst")}</span>
          </div>
          <EventList
            events={events.items}
            hasNextPage={events.hasNextPage}
            isFetchingNextPage={events.isFetchingNextPage}
            key={events.filterQuery}
            onBottomReached={() => void loadOlderEvents()}
            onSelect={setSelected}
            wrap={wrap}
          />
        </>
      ) : null}
      <EventDetail event={selected} onClose={() => setSelected(null)} related={related} />
    </section>
  );
}
