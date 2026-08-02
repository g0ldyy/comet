import { useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import type { OperationalEventData } from "../../api/generated/contracts";

export function EventList({
  events,
  hasNextPage,
  isFetchingNextPage,
  onBottomReached,
  onSelect,
  wrap,
}: {
  events: OperationalEventData[];
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  onBottomReached: () => void;
  onSelect: (event: OperationalEventData) => void;
  wrap: boolean;
}) {
  const { t } = useTranslation();
  const previousScrollTop = useRef(0);
  const time = useMemo(
    () =>
      new Intl.DateTimeFormat(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
    [],
  );
  return (
    <div
      className="event-list event-list--logs"
      onScroll={(event) => {
        const container = event.currentTarget;
        const scrollingDown = container.scrollTop > previousScrollTop.current;
        previousScrollTop.current = container.scrollTop;
        if (
          scrollingDown &&
          container.scrollHeight - container.scrollTop - container.clientHeight <= 1
        ) {
          onBottomReached();
        }
      }}
    >
      {events.map((event) => (
        <button
          className={`event-row event-row--${event.level.toLowerCase()}`}
          key={event.id}
          onClick={() => onSelect(event)}
          type="button"
        >
          <time
            dateTime={new Date(event.created_at * 1000).toISOString()}
            title={new Date(event.created_at * 1000).toLocaleString()}
          >
            {time.format(event.created_at * 1000)}
          </time>
          <span className="event-row__content">
            <span className="event-row__title">
              <strong>{event.event}</strong>
              <span className="event-row__category">{event.category}</span>
            </span>
            <span
              className={
                wrap ? "event-row__message event-row__message--wrap" : "event-row__message"
              }
            >
              {event.message}
            </span>
          </span>
          <span className="event-row__meta">
            <span className="event-row__role">{event.role}</span>
            <span className={`event-row__outcome event-row__outcome--${event.level.toLowerCase()}`}>
              {event.outcome ?? t(`events.levels.${event.level.toLocaleLowerCase()}`)}
            </span>
          </span>
        </button>
      ))}
      {events.length === 0 ? <p className="empty-state">{t("events.empty")}</p> : null}
      {hasNextPage ? (
        <div aria-hidden="true" className="event-load-sentinel">
          {isFetchingNextPage ? <span /> : null}
        </div>
      ) : null}
    </div>
  );
}
