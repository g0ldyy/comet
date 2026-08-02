import { GripVertical, KeyRound, Plus, Trash2, UserPlus } from "lucide-react";
import { type KeyboardEvent, type PointerEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ConfiguratorBootstrapData } from "../../api/generated/contracts";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Switch } from "../../components/ui/Switch";
import { type DebridDraft, DIRECT_TORRENT_SERVICE } from "./model";

const DEBRID_RESOURCES: Partial<
  Record<string, { apiKeyUrl: string; login?: true; referralUrl?: string }>
> = {
  realdebrid: {
    apiKeyUrl: "https://real-debrid.com/apitoken",
    referralUrl: "https://real-debrid.com/?id=16161532",
  },
  torbox: {
    apiKeyUrl: "https://torbox.app/settings",
    referralUrl: "https://torbox.app/subscription?referral=1ffb2238-1c5f-402e-a2ce-3d7a86c52d02",
  },
  alldebrid: {
    apiKeyUrl: "https://alldebrid.com/apikeys",
  },
  debridlink: {
    apiKeyUrl: "https://debrid-link.com/webapp/apikey",
    referralUrl: "https://debrid-link.fr/id/G7mli",
  },
  premiumize: {
    apiKeyUrl: "https://premiumize.me/account",
  },
  debrider: {
    apiKeyUrl: "https://debrider.app/dashboard/account",
  },
  easydebrid: {
    apiKeyUrl: "https://paradise-cloud.com/products/easydebrid",
  },
  offcloud: {
    apiKeyUrl: "https://offcloud.com/#/account",
  },
  pikpak: {
    apiKeyUrl: "https://mypikpak.com",
    login: true,
  },
};

export function PlaybackStep({
  bootstrap,
  debridServices,
  onDebridServicesChange,
  onProxyPasswordChange,
  onScrapeChange,
  proxyPassword,
  scrape,
  showDebridOptions,
}: {
  bootstrap: ConfiguratorBootstrapData;
  debridServices: DebridDraft[];
  onDebridServicesChange: (services: DebridDraft[]) => void;
  onProxyPasswordChange: (password: string) => void;
  onScrapeChange: (enabled: boolean) => void;
  proxyPassword: string;
  scrape: boolean;
  showDebridOptions: boolean;
}) {
  const { t } = useTranslation();
  const [drag, setDrag] = useState<{
    configurationId: string;
    target: number;
    x: number;
    y: number;
  } | null>(null);
  const draggedService = drag
    ? debridServices.find((entry) => entry.configurationId === drag.configurationId)?.service
    : null;
  const selectedServices = new Set(debridServices.map(({ service }) => service));
  const supportedServices = bootstrap.capabilities.torrent_streams
    ? [...bootstrap.debrid_services, DIRECT_TORRENT_SERVICE]
    : bootstrap.debrid_services;
  const nextService = supportedServices.find((service) => !selectedServices.has(service));
  const move = (configurationId: string, target: number) => {
    const index = debridServices.findIndex((entry) => entry.configurationId === configurationId);
    if (index < 0 || target < 0 || target >= debridServices.length || index === target) return;
    const next = [...debridServices];
    const [entry] = next.splice(index, 1);
    if (!entry) return;
    next.splice(target, 0, entry);
    onDebridServicesChange(next);
  };
  const moveFromPointer = (event: PointerEvent<HTMLButtonElement>) => {
    if (!drag) return;
    event.preventDefault();
    const row = document
      .elementFromPoint(event.clientX, event.clientY)
      ?.closest<HTMLElement>("[data-debrid-id]");
    const targetId = row?.dataset.debridId;
    const target = targetId
      ? debridServices.findIndex((entry) => entry.configurationId === targetId)
      : drag.target;
    setDrag({
      ...drag,
      target: target >= 0 ? target : drag.target,
      x: event.clientX,
      y: event.clientY,
    });
  };
  const moveFromKeyboard = (event: KeyboardEvent<HTMLButtonElement>, configurationId: string) => {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const index = debridServices.findIndex((entry) => entry.configurationId === configurationId);
    move(configurationId, index + (event.key === "ArrowUp" ? -1 : 1));
  };

  return (
    <section className="configuration-fields">
      <div className={`debrid-list${drag ? " debrid-list--sorting" : ""}`}>
        {debridServices.map((entry, index) => {
          const resources = DEBRID_RESOURCES[entry.service];
          return (
            <article
              className={[
                "debrid-row",
                drag?.configurationId === entry.configurationId ? "debrid-row--dragging" : "",
                drag && drag.configurationId !== entry.configurationId && drag.target === index
                  ? drag.target <
                    debridServices.findIndex(
                      (current) => current.configurationId === drag.configurationId,
                    )
                    ? "debrid-row--drop-before"
                    : "debrid-row--drop-after"
                  : "",
              ]
                .filter(Boolean)
                .join(" ")}
              data-debrid-id={entry.configurationId}
              key={entry.configurationId}
            >
              <Button
                aria-label={t("configure.playback.reorderService")}
                aria-pressed={drag?.configurationId === entry.configurationId}
                className="debrid-row__handle"
                onKeyDown={(event) => moveFromKeyboard(event, entry.configurationId)}
                onLostPointerCapture={() => setDrag(null)}
                onPointerDown={(event) => {
                  if (event.button !== 0) return;
                  event.currentTarget.setPointerCapture(event.pointerId);
                  setDrag({
                    configurationId: entry.configurationId,
                    target: index,
                    x: event.clientX,
                    y: event.clientY,
                  });
                }}
                onPointerMove={moveFromPointer}
                onPointerUp={(event) => {
                  if (drag) move(drag.configurationId, drag.target);
                  event.currentTarget.releasePointerCapture(event.pointerId);
                  setDrag(null);
                }}
                title={t("configure.playback.reorderService")}
                variant="ghost"
              >
                <GripVertical aria-hidden="true" size={18} />
              </Button>
              <Select
                label={t("configure.playback.service")}
                labelHidden
                onValueChange={(service) =>
                  onDebridServicesChange(
                    debridServices.map((current, position) =>
                      position === index
                        ? {
                            ...current,
                            accountId:
                              service === DIRECT_TORRENT_SERVICE
                                ? ""
                                : current.service === DIRECT_TORRENT_SERVICE
                                  ? crypto.randomUUID()
                                  : current.accountId,
                            apiKey: service === DIRECT_TORRENT_SERVICE ? "" : current.apiKey,
                            service,
                          }
                        : current,
                    ),
                  )
                }
                value={entry.service}
              >
                {supportedServices
                  .filter((service) => service === entry.service || !selectedServices.has(service))
                  .map((service) => (
                    <option key={service} value={service}>
                      {t(
                        service === DIRECT_TORRENT_SERVICE
                          ? "configure.playback.enableTorrent"
                          : `configure.debridServices.${service}`,
                      )}
                    </option>
                  ))}
              </Select>
              {entry.service === DIRECT_TORRENT_SERVICE ? (
                <div className="debrid-row__p2p">{t("configure.playback.p2pNoKey")}</div>
              ) : (
                <div className="debrid-credentials">
                  <Input
                    autoComplete="off"
                    label={t("configure.playback.apiKey")}
                    labelHidden
                    onChange={(event) =>
                      onDebridServicesChange(
                        debridServices.map((current, position) =>
                          position === index ? { ...current, apiKey: event.target.value } : current,
                        ),
                      )
                    }
                    placeholder={t(
                      resources?.login
                        ? "configure.playback.pikpakFormat"
                        : "configure.playback.apiKey",
                    )}
                    type={resources?.login ? "text" : "password"}
                    value={entry.apiKey}
                  />
                  {resources ? (
                    <div className="debrid-credentials__links">
                      <a href={resources.apiKeyUrl} rel="noreferrer" target="_blank">
                        <KeyRound aria-hidden="true" size={13} />
                        {t("configure.playback.getApiKey")}
                      </a>
                      {resources.referralUrl ? (
                        <a href={resources.referralUrl} rel="noreferrer" target="_blank">
                          <UserPlus aria-hidden="true" size={13} />
                          {t("configure.playback.createAccount")}
                        </a>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              )}
              <Button
                aria-label={t("configure.playback.removeService")}
                onClick={() =>
                  onDebridServicesChange(debridServices.filter((_, position) => position !== index))
                }
                variant="ghost"
              >
                <Trash2 aria-hidden="true" size={17} />
              </Button>
            </article>
          );
        })}
        {nextService ? (
          <Button
            onClick={() =>
              onDebridServicesChange([
                ...debridServices,
                {
                  accountId: nextService === DIRECT_TORRENT_SERVICE ? "" : crypto.randomUUID(),
                  apiKey: "",
                  configurationId: crypto.randomUUID(),
                  service: nextService,
                },
              ])
            }
            variant="secondary"
          >
            <Plus aria-hidden="true" size={17} />
            {t("configure.playback.addService")}
          </Button>
        ) : null}
        {drag ? (
          <div
            aria-hidden="true"
            className="debrid-drag-preview"
            style={{ left: drag.x, top: drag.y }}
          >
            <GripVertical size={16} />
            {t(
              draggedService === DIRECT_TORRENT_SERVICE
                ? "configure.playback.enableTorrent"
                : `configure.debridServices.${draggedService}`,
            )}
          </div>
        ) : null}
      </div>
      {showDebridOptions ? (
        <div className="option-stack">
          <Switch
            checked={scrape}
            label={t("configure.playback.scrapeLibraries")}
            onCheckedChange={onScrapeChange}
          />
        </div>
      ) : null}
      {bootstrap.capabilities.proxy_debrid_stream ? (
        <Input
          autoComplete="off"
          label={t("configure.playback.proxyPassword")}
          onChange={(event) => onProxyPasswordChange(event.target.value)}
          type="password"
          value={proxyPassword}
        />
      ) : null}
    </section>
  );
}
