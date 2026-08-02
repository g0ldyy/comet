import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronDown, Download, History, RotateCcw, Save, Search, Upload } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { apiValidationErrors } from "../../api/errors";
import type { SettingView } from "../../api/generated/contracts";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { getSystemSnapshot } from "../metrics/api";
import { restartRuntime } from "../system/api";
import { getSettings, getSettingsAudit, saveSettings } from "./api";
import {
  changed,
  draftError,
  exportSettings,
  initialDrafts,
  mutationDocument,
  parseSettingsImport,
  type SettingDraft,
  settingDraft,
} from "./model";
import { SettingField } from "./SettingField";

export function SettingsPage() {
  const { t } = useTranslation();
  const settings = useQuery({
    queryFn: getSettings,
    queryKey: ["admin", "settings"],
  });
  const audit = useQuery({
    queryFn: getSettingsAudit,
    queryKey: ["admin", "settings", "audit"],
  });
  const system = useQuery({
    queryFn: getSystemSnapshot,
    queryKey: ["admin", "system", "snapshot"],
    staleTime: 5_000,
  });
  const restart = useMutation({ mutationFn: restartRuntime });
  const [drafts, setDrafts] = useState<Record<string, SettingDraft>>({});
  const [draftRevision, setDraftRevision] = useState<number | null>(null);
  const [category, setCategory] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [message, setMessage] = useState<{
    text: string;
    tone: "danger" | "success" | "warning";
  } | null>(null);
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const importInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (settings.data !== undefined) {
      setDrafts(initialDrafts(settings.data.settings));
      setDraftRevision(settings.data.stored_revision);
    }
  }, [settings.data]);

  const save = useMutation({
    mutationFn: saveSettings,
    onSuccess: async (result) => {
      setServerErrors({});
      setMessage({
        text: t(result.restart_required ? "settings.savedRestart" : "settings.saved", {
          revision: result.revision,
        }),
        tone: result.restart_required ? "warning" : "success",
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin", "settings"] }),
        queryClient.invalidateQueries({ queryKey: ["admin", "settings", "audit"] }),
        queryClient.invalidateQueries({ queryKey: ["admin", "system", "snapshot"] }),
      ]);
    },
    onError: (error) => {
      const validationErrors = apiValidationErrors(error, t("settings.validationError"));
      setServerErrors(validationErrors);
      setMessage({
        text: t(
          Object.keys(validationErrors).length > 0
            ? "settings.validationError"
            : "settings.saveError",
        ),
        tone: "danger",
      });
    },
  });

  const allSettings =
    settings.data?.stored_revision === draftRevision ? settings.data.settings : [];
  const currentRuntime = system.data?.runtimes.find(
    (runtime) => runtime.instance_id === system.data?.current_instance_id,
  );
  const categoryCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const setting of allSettings) {
      counts.set(setting.catalog.category, (counts.get(setting.catalog.category) ?? 0) + 1);
    }
    return counts;
  }, [allSettings]);
  const categories = [...categoryCounts.keys()];
  const errors = useMemo(
    () =>
      new Map(
        allSettings.map((setting) => {
          const localError = draftError(setting, settingDraft(drafts, setting.catalog.key));
          return [
            setting.catalog.key,
            localError
              ? t(`settings.errors.${localError}`)
              : (serverErrors[setting.catalog.key] ?? null),
          ];
        }),
      ),
    [allSettings, drafts, serverErrors, t],
  );
  const changedSettings = allSettings.filter((setting) =>
    changed(setting, settingDraft(drafts, setting.catalog.key)),
  );
  const categorySettings = allSettings.filter(
    (setting) => category === "all" || setting.catalog.category === category,
  );
  const visible = allSettings.filter((setting) => {
    const matchCategory = category === "all" || setting.catalog.category === category;
    const needle = search.trim().toLocaleLowerCase();
    return (
      matchCategory && (needle === "" || setting.catalog.key.toLocaleLowerCase().includes(needle))
    );
  });
  const categoryLabel =
    category === "all" ? t("settings.allCategories") : t(`settings.categories.${category}`);

  const updateDraft = (setting: SettingView, draft: SettingDraft) => {
    setDrafts((current) => ({ ...current, [setting.catalog.key]: draft }));
    setServerErrors((current) => {
      const { [setting.catalog.key]: _discarded, ...remaining } = current;
      return remaining;
    });
    setMessage(null);
  };

  const saveChanges = () => {
    if (changedSettings.length === 0) return;
    if (changedSettings.some((setting) => errors.get(setting.catalog.key) !== null)) {
      setMessage({ text: t("settings.validationError"), tone: "danger" });
      return;
    }
    save.mutate(mutationDocument(allSettings, drafts));
  };

  const importFile = async (file: File) => {
    try {
      if (file.size > 256 * 1024) throw new Error("Settings import is too large");
      setDrafts(parseSettingsImport(await file.text(), allSettings));
      setServerErrors({});
      setMessage({ text: t("settings.imported"), tone: "success" });
    } catch {
      setMessage({ text: t("settings.importError"), tone: "danger" });
    } finally {
      if (importInput.current) importInput.current.value = "";
    }
  };

  const downloadExport = () => {
    const exportedAt = new Date()
      .toISOString()
      .replace(/\.\d{3}Z$/, "Z")
      .replaceAll(":", "-");
    const url = URL.createObjectURL(
      new Blob([exportSettings(allSettings)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `comet-settings-${exportedAt}-r${settings.data?.stored_revision}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const discardChanges = () => {
    setDrafts(initialDrafts(allSettings));
    setServerErrors({});
    setMessage(null);
  };

  return (
    <section aria-labelledby="settings-title" className="section-page settings-page">
      <header className="section-page__header">
        <div>
          <h1 id="settings-title">{t("nav.settings")}</h1>
        </div>
        <div className="settings-actions">
          <input
            accept="application/json,.json"
            className="visually-hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void importFile(file);
            }}
            ref={importInput}
            type="file"
          />
          <Button onClick={() => importInput.current?.click()} variant="secondary">
            <Upload aria-hidden="true" size={16} />
            {t("settings.import")}
          </Button>
          <Button disabled={!settings.data} onClick={downloadExport} variant="secondary">
            <Download aria-hidden="true" size={16} />
            {t("settings.export")}
          </Button>
        </div>
      </header>

      {settings.isPending ? <Skeleton label={t("settings.loading")} lines={10} /> : null}
      {settings.isError ? (
        <Alert title={t("settings.errorTitle")} tone="danger">
          <ApiErrorDetails error={settings.error} fallback={t("settings.errorDescription")} />
        </Alert>
      ) : null}
      {settings.data ? (
        <>
          <div className="revision-strip">
            <div>
              <span>{t("settings.storedRevision")}</span>
              <strong>{settings.data.stored_revision}</strong>
            </div>
            <div>
              <span>{t("settings.appliedRevision")}</span>
              <strong>{settings.data.applied_revision}</strong>
            </div>
            <div>
              <span>{t("settings.pendingChanges")}</span>
              <strong>{changedSettings.length}</strong>
            </div>
          </div>
          {system.data ? (
            <div className="replica-revisions">
              {system.data.runtimes.map((runtime) => (
                <div key={runtime.instance_id}>
                  <span>
                    <strong>{runtime.alias ?? runtime.hostname}</strong>
                    <small>{runtime.branch}</small>
                  </span>
                  <span>{t("settings.appliedRevision")}</span>
                  <strong
                    className={
                      runtime.applied_revision === settings.data.stored_revision
                        ? "revision-current"
                        : "revision-stale"
                    }
                  >
                    {runtime.applied_revision}
                  </strong>
                </div>
              ))}
            </div>
          ) : null}
          {settings.data.pending_restart_keys.length > 0 ? (
            <Alert title={t("settings.restartTitle")} tone="warning">
              <p>{t("settings.restartDescription")}</p>
              {currentRuntime?.restart_capable ? (
                <Button
                  disabled={restart.isPending}
                  onClick={() => {
                    if (window.confirm(t("system.restartConfirm"))) {
                      restart.mutate(currentRuntime.instance_id);
                    }
                  }}
                  variant="danger"
                >
                  {t("system.restart")}
                </Button>
              ) : (
                <small>{t("system.restartUnavailable")}</small>
              )}
            </Alert>
          ) : null}
          {restart.isError ? (
            <Alert title={t("system.actionError")} tone="danger">
              <ApiErrorDetails
                error={restart.error}
                fallback={t("system.actionErrorDescription")}
              />
            </Alert>
          ) : null}
          {message ? (
            <Alert tone={message.tone}>
              {message.tone === "danger" && save.error ? (
                <ApiErrorDetails error={save.error} fallback={message.text} />
              ) : (
                message.text
              )}
            </Alert>
          ) : null}

          <div className="settings-toolbar">
            <div className="settings-search">
              <Search aria-hidden="true" size={17} />
              <Input
                label={t("settings.search")}
                onChange={(event) => setSearch(event.target.value)}
                placeholder={t("settings.searchPlaceholder")}
                value={search}
              />
            </div>
          </div>

          <div className="settings-workspace">
            <nav aria-label={t("settings.categoryNavigation")} className="settings-categories">
              <button
                className={
                  category === "all" ? "category-tab category-tab--active" : "category-tab"
                }
                onClick={() => setCategory("all")}
                type="button"
              >
                <span>{t("settings.allCategories")}</span>
                <small>{allSettings.length}</small>
              </button>
              {categories.map((item) => (
                <button
                  className={
                    category === item ? "category-tab category-tab--active" : "category-tab"
                  }
                  key={item}
                  onClick={() => setCategory(item)}
                  type="button"
                >
                  <span>{t(`settings.categories.${item}`)}</span>
                  <small>{categoryCounts.get(item)}</small>
                </button>
              ))}
            </nav>
            <div className="settings-main">
              <header className="settings-main__header">
                <div>
                  <h2>{categoryLabel}</h2>
                  <p>
                    {t("settings.categorySummary", {
                      total: categorySettings.length,
                      visible: visible.length,
                    })}
                  </p>
                </div>
                {changedSettings.length > 0 ? (
                  <Button onClick={discardChanges} variant="ghost">
                    <RotateCcw aria-hidden="true" size={15} />
                    {t("settings.discardAll")}
                  </Button>
                ) : null}
              </header>
              <div className="settings-list">
                {visible.map((setting) => (
                  <SettingField
                    draft={settingDraft(drafts, setting.catalog.key)}
                    error={errors.get(setting.catalog.key) ?? null}
                    key={setting.catalog.key}
                    onChange={(draft) => updateDraft(setting, draft)}
                    setting={setting}
                  />
                ))}
                {visible.length === 0 ? <p className="empty-state">{t("settings.empty")}</p> : null}
              </div>
            </div>
          </div>

          {changedSettings.length > 0 ? (
            <aside className="settings-savebar">
              <div>
                <strong>{t("settings.modifiedCount", { count: changedSettings.length })}</strong>
                <span>{t("settings.pendingChanges")}</span>
              </div>
              <Button disabled={save.isPending} onClick={saveChanges}>
                <Save aria-hidden="true" size={16} />
                {t("settings.save", { count: changedSettings.length })}
              </Button>
            </aside>
          ) : null}

          {audit.isError || (audit.data?.items.length ?? 0) > 0 ? (
            <details className="dashboard-panel settings-audit">
              <summary>
                <History aria-hidden="true" size={15} />
                <span>{t("settings.recentChanges")}</span>
                <ChevronDown aria-hidden="true" className="settings-audit__chevron" size={16} />
              </summary>
              {audit.isError ? (
                <Alert tone="warning">
                  <ApiErrorDetails error={audit.error} fallback={t("settings.auditError")} />
                </Alert>
              ) : (
                <div className="audit-list">
                  {audit.data?.items.map((entry) => (
                    <div key={entry.id}>
                      <code>{entry.key}</code>
                      <span>{entry.action}</span>
                      <span>
                        {entry.previous_source ?? "—"} → {entry.next_source ?? "—"}
                      </span>
                      <time>{new Date(entry.changed_at * 1_000).toLocaleString()}</time>
                    </div>
                  ))}
                </div>
              )}
            </details>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
