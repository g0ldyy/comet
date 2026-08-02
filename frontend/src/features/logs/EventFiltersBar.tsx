import { useTranslation } from "react-i18next";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import type { EventFilters } from "./api";

export function EventFiltersBar({
  filters,
  onChange,
}: {
  filters: EventFilters;
  onChange: (filters: EventFilters) => void;
}) {
  const { t } = useTranslation();
  const change = (key: keyof EventFilters, value: string) => onChange({ ...filters, [key]: value });
  return (
    <div className="event-filters">
      <Select
        label={t("events.filters.level")}
        onValueChange={(value) => change("level", value)}
        value={filters.level}
      >
        <option value="">{t("events.filters.allLevels")}</option>
        {["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((level) => (
          <option key={level} value={level}>
            {t(`events.levels.${level.toLocaleLowerCase()}`)}
          </option>
        ))}
      </Select>
      <Input
        label={t("events.filters.category")}
        onChange={(event) => change("category", event.target.value.toUpperCase())}
        value={filters.category}
      />
      <Input
        label={t("events.filters.role")}
        onChange={(event) => change("role", event.target.value)}
        value={filters.role}
      />
      <Input
        label={t("events.filters.outcome")}
        onChange={(event) => change("outcome", event.target.value)}
        value={filters.outcome}
      />
      <Input
        label={t("events.filters.media")}
        onChange={(event) => change("mediaType", event.target.value)}
        value={filters.mediaType}
      />
      <Input
        label={t("events.filters.provider")}
        onChange={(event) => change("providerName", event.target.value)}
        value={filters.providerName}
      />
      <Input
        label={t("events.filters.replica")}
        maxLength={32}
        onChange={(event) => change("instanceId", event.target.value)}
        value={filters.instanceId}
      />
      <Input
        label={t("events.filters.request")}
        maxLength={32}
        onChange={(event) => change("requestId", event.target.value)}
        value={filters.requestId}
      />
      <Input
        label={t("events.filters.run")}
        maxLength={32}
        onChange={(event) => change("runId", event.target.value)}
        value={filters.runId}
      />
      <Input
        label={t("events.filters.connection")}
        maxLength={32}
        onChange={(event) => change("connectionId", event.target.value)}
        value={filters.connectionId}
      />
      <Input
        label={t("events.filters.from")}
        onChange={(event) => change("startedAt", event.target.value)}
        type="datetime-local"
        value={filters.startedAt}
      />
      <Input
        label={t("events.filters.to")}
        onChange={(event) => change("endedAt", event.target.value)}
        type="datetime-local"
        value={filters.endedAt}
      />
    </div>
  );
}
