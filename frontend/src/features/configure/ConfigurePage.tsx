import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { Gauge, Languages, Magnet, RadioTower, SlidersHorizontal } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { ApiClientError } from "../../api/client";
import { apiErrorSummary } from "../../api/errors";
import type { ConfigModel, ConfiguratorBootstrapData } from "../../api/generated/contracts";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Brand } from "../../components/Brand";
import { CommunityLinks } from "../../components/CommunityLinks";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { Switch } from "../../components/ui/Switch";
import { Toast } from "../../components/ui/Toast";
import { LanguageSelector } from "../../i18n/LanguageSelector";
import {
  associateKodi,
  type CapabilityBindingResult,
  getConfiguratorBootstrap,
  testCapabilities,
  validateConfiguration,
} from "./api";
import { capabilityFailureMessage, capabilityReason } from "./capabilities";
import { decodeConfiguration, manifestLocation } from "./codec";
import {
  type ConfigureFormValues,
  configurationDocument,
  DIRECT_TORRENT_SERVICE,
  formValues,
} from "./model";
import { PlaybackStep } from "./PlaybackStep";
import { LanguageStep, PreferencesStep } from "./PreferencesStep";
import { ReviewStep } from "./ReviewStep";
import { UsenetStep } from "./UsenetStep";

const customHeaderTemplate = document.getElementById("comet-custom-header") as HTMLTemplateElement;
const hasCustomHeader =
  customHeaderTemplate.content.children.length > 0 ||
  customHeaderTemplate.content.textContent.trim() !== "";

function CustomHeader() {
  const host = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    host.current?.replaceChildren(customHeaderTemplate.content.cloneNode(true));
  }, []);

  return <div className="comet-custom-header" ref={host} />;
}

export function ConfigurePage() {
  const { t } = useTranslation();
  const { b64config } = useParams({ strict: false }) as { b64config?: string };
  const bootstrap = useQuery({
    queryFn: getConfiguratorBootstrap,
    queryKey: ["configure", "bootstrap"],
  });
  const existing = useQuery({
    enabled: bootstrap.isSuccess && b64config !== undefined,
    queryFn: async () => validateConfiguration(decodeConfiguration(b64config ?? "")),
    queryKey: ["configure", "document", b64config],
  });

  if (bootstrap.isPending || (b64config && existing.isPending)) {
    return (
      <main className="centered-state">
        <Skeleton label={t("app.loading")} lines={4} />
      </main>
    );
  }
  if (bootstrap.isError || existing.isError) {
    return (
      <main className="centered-state">
        <Alert tone="danger">
          <ApiErrorDetails
            error={bootstrap.error ?? existing.error}
            fallback={t("configure.messages.openError")}
          />
        </Alert>
        <Button onClick={() => window.location.assign("/configure")}>
          {t("configure.actions.new")}
        </Button>
      </main>
    );
  }

  const loaded = existing.data;
  return (
    <Configurator
      bootstrap={bootstrap.data}
      initial={loaded ?? bootstrap.data.default_configuration}
      key={b64config ?? "new"}
      {...(loaded ? { loaded } : {})}
    />
  );
}

type ConfigureSection = "languages" | "playback" | "results" | "usenet";

function Configurator({
  bootstrap,
  initial,
  loaded: initialLoaded,
}: {
  bootstrap: ConfiguratorBootstrapData;
  initial: ConfigModel;
  loaded?: ConfigModel;
}) {
  const { t } = useTranslation();
  const loaded = initialLoaded;
  const [activeSection, setActiveSection] = useState<ConfigureSection>("playback");
  const [busy, setBusy] = useState(false);
  const [capabilityResults, setCapabilityResults] = useState<
    Record<string, CapabilityBindingResult>
  >({});
  const [message, setMessage] = useState<{
    closing: boolean;
    text: string;
    tone: "danger" | "info" | "success" | "warning";
  } | null>(null);
  const dismissMessage = () =>
    setMessage((current) => (current ? { ...current, closing: true } : null));
  useEffect(() => {
    if (!message || message.closing) return;
    const timeout = window.setTimeout(
      () => setMessage((current) => (current ? { ...current, closing: true } : null)),
      4_600,
    );
    return () => window.clearTimeout(timeout);
  }, [message]);
  const {
    formState: { isDirty },
    getValues,
    setValue,
    watch,
  } = useForm<ConfigureFormValues>({
    defaultValues: formValues(initial, bootstrap),
  });
  const values = watch();
  const hasConfiguredDebridService = values.debridServices.some(
    ({ apiKey, service }) => service !== DIRECT_TORRENT_SERVICE && apiKey !== "",
  );
  const sections = [
    {
      id: "playback" as const,
      icon: Magnet,
      title: t("configure.sections.playback"),
    },
    ...(bootstrap.capabilities.usenet
      ? [
          {
            id: "usenet" as const,
            icon: RadioTower,
            title: t("configure.sections.usenet"),
          },
        ]
      : []),
    {
      id: "results" as const,
      icon: SlidersHorizontal,
      title: t("configure.sections.results"),
    },
    {
      id: "languages" as const,
      icon: Languages,
      title: t("configure.sections.languages"),
    },
  ];
  const active = sections.find(({ id }) => id === activeSection) as (typeof sections)[number];
  const change = <Key extends keyof ConfigureFormValues>(
    key: Key,
    value: ConfigureFormValues[Key],
  ) => {
    setCapabilityResults({});
    setValue(key, value as never, { shouldDirty: true });
  };

  const rememberCapabilityResults = (bindings: readonly CapabilityBindingResult[]) =>
    setCapabilityResults(
      Object.fromEntries(bindings.map((binding) => [binding.configuration_id, binding])),
    );

  const validate = async (testConnections: boolean): Promise<ConfigModel> => {
    const document = configurationDocument(getValues(), bootstrap, loaded);
    await validateConfiguration(document);
    if (testConnections && getValues("usenetEnabled")) {
      const result = await testCapabilities(document, bootstrap.capabilities.stremio_api_prefix);
      rememberCapabilityResults(result.bindings ?? []);
      if (!result.ok) {
        throw new Error(capabilityFailureMessage(t, result));
      }
    }
    return document;
  };

  const getManifestLocation = (configuration: ConfigModel, install: boolean) =>
    manifestLocation(
      loaded || isDirty ? configuration : undefined,
      bootstrap.capabilities.stremio_api_prefix,
      install,
    );

  const run = async (
    action: (configuration: ConfigModel) => Promise<string | null>,
    options: { success: string; testConnections?: boolean },
  ): Promise<boolean> => {
    setBusy(true);
    setMessage(null);
    try {
      const configuration = await validate(options.testConnections ?? false);
      const success = await action(configuration);
      setMessage({ closing: false, text: success ?? options.success, tone: "success" });
      return true;
    } catch (error) {
      const text =
        error instanceof ApiClientError
          ? `${error.code === "validation_failed" ? `${t("configure.messages.invalid")}: ` : ""}${apiErrorSummary(
              error,
              t("configure.messages.invalid"),
              (id) => t("errors.requestId", { id }),
            )}`
          : error instanceof Error && error.message === "configuration_too_large"
            ? t("configure.messages.invalid")
            : error instanceof Error && error.message === "kodi_association_failed"
              ? t("configure.actions.kodiFailed")
              : error instanceof Error
                ? error.message
                : t("configure.messages.invalid");
      setMessage({
        closing: false,
        text,
        tone: "danger",
      });
      return false;
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="configure-app">
      <div className="configure-language-utility">
        <Link className="configure-admin-link" to="/admin/overview">
          <Gauge aria-hidden="true" size={16} />
          <span>{t("nav.admin")}</span>
        </Link>
        <LanguageSelector />
      </div>
      <div className="configure-content">
        <div className={`configure-intro ${hasCustomHeader ? "configure-intro--custom" : ""}`}>
          <Brand />
          {hasCustomHeader ? <CustomHeader /> : null}
          <p className="configure-tagline">
            Stremio&apos;s fastest torrent/debrid/usenet search add-on.
          </p>
        </div>
        <div className="configure-workspace">
          <div className="configure-shell">
            <nav aria-label={t("app.name")} className="configuration-sections">
              {sections.map((section) => {
                const Icon = section.icon;
                return (
                  <div
                    aria-current={section.id === activeSection ? "step" : undefined}
                    className="configuration-section-row"
                    key={section.id}
                  >
                    <button
                      className="configuration-section"
                      onClick={() => setActiveSection(section.id)}
                      type="button"
                    >
                      <span className="configuration-section__icon">
                        <Icon aria-hidden="true" size={18} strokeWidth={1.7} />
                      </span>
                      <strong className="configuration-section__title">{section.title}</strong>
                    </button>
                    {section.id === "playback" ? (
                      <Switch
                        checked={values.bittorrentEnabled}
                        compact
                        label={t("configure.sections.playback")}
                        onCheckedChange={(enabled) => {
                          change("bittorrentEnabled", enabled);
                          change("schemaVersion", 2);
                        }}
                      />
                    ) : section.id === "usenet" ? (
                      <Switch
                        checked={values.usenetEnabled}
                        compact
                        label={t("configure.usenet.enable")}
                        onCheckedChange={(enabled) => {
                          change("usenetEnabled", enabled);
                          if (enabled) change("schemaVersion", 2);
                        }}
                      />
                    ) : null}
                  </div>
                );
              })}
            </nav>

            <section className="configuration-stage" key={active.id}>
              <header className="configuration-stage__header">
                <h1>{active.title}</h1>
              </header>
              <div className="configuration-section__content">
                {activeSection === "playback" ? (
                  <PlaybackStep
                    bootstrap={bootstrap}
                    debridServices={values.debridServices}
                    onDebridServicesChange={(entries) => change("debridServices", entries)}
                    onProxyPasswordChange={(password) => change("proxyPassword", password)}
                    onScrapeChange={(enabled) => change("scrapeDebridAccountTorrents", enabled)}
                    proxyPassword={values.proxyPassword}
                    scrape={values.scrapeDebridAccountTorrents}
                    showDebridOptions={hasConfiguredDebridService}
                  />
                ) : activeSection === "usenet" ? (
                  <UsenetStep
                    accounts={loaded?.accounts ?? {}}
                    bootstrap={bootstrap}
                    capabilityResults={capabilityResults}
                    nativeAccessToken={values.nativeAccessToken}
                    onNativeAccessTokenChange={(token) => change("nativeAccessToken", token)}
                    onProvidersChange={(providers) => change("usenetProviders", providers)}
                    onSourcesChange={(sources) => change("usenetSources", sources)}
                    onTestBinding={(configurationId) =>
                      run(
                        async (configuration) => {
                          const result = await testCapabilities(
                            configuration,
                            bootstrap.capabilities.stremio_api_prefix,
                            configurationId,
                          );
                          const binding = result.bindings?.find(
                            (entry) => entry.configuration_id === configurationId,
                          );
                          rememberCapabilityResults(result.bindings ?? []);
                          if (!binding?.eligible) {
                            throw new Error(capabilityFailureMessage(t, result));
                          }
                          return binding.degraded ? capabilityReason(t, binding) : null;
                        },
                        { success: t("configure.messages.connectionAvailable") },
                      )
                    }
                    providers={values.usenetProviders}
                    sources={values.usenetSources}
                  />
                ) : activeSection === "results" ? (
                  <PreferencesStep
                    bootstrap={bootstrap}
                    onChange={change}
                    showDebridOptions={hasConfiguredDebridService}
                    values={values}
                  />
                ) : (
                  <LanguageStep bootstrap={bootstrap} onChange={change} values={values} />
                )}
              </div>
            </section>
          </div>

          <ReviewStep
            busy={busy}
            onCopy={() =>
              run(
                async (configuration) => {
                  const location = getManifestLocation(configuration, false);
                  await navigator.clipboard.writeText(location.url);
                  return location.warnAboutRequestLine ? t("configure.messages.copiedLong") : null;
                },
                { success: t("configure.messages.copied"), testConnections: true },
              )
            }
            onInstall={() =>
              run(
                async (configuration) => {
                  window.location.assign(getManifestLocation(configuration, true).url);
                  return null;
                },
                { success: t("configure.messages.openingStremio"), testConnections: true },
              )
            }
            onKodi={(code) =>
              run(
                async (configuration) => {
                  await associateKodi(code, getManifestLocation(configuration, false).url);
                  return null;
                },
                { success: t("configure.messages.kodiPaired"), testConnections: true },
              )
            }
            onTest={() =>
              run(async () => null, {
                success: t("configure.messages.usenetAvailable"),
                testConnections: true,
              })
            }
            values={values}
          />
          <footer className="configure-footer">
            <CommunityLinks />
          </footer>
        </div>
      </div>
      {message ? (
        <Toast
          closing={message.closing}
          onClose={dismissMessage}
          onExited={() => setMessage(null)}
          tone={message.tone}
        >
          {message.text}
        </Toast>
      ) : null}
    </main>
  );
}
