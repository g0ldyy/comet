import { Check, Fingerprint, Plus, RadioTower, ShieldCheck, Trash2, UserRound } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Switch } from "../../components/ui/Switch";
import type { CapabilityBindingResult } from "./api";
import { capabilityReason } from "./capabilities";
import {
  defaultOptions,
  newznabPresets,
  nntpFields,
  type OptionField,
  providerSchemas,
  sourceSchemas,
} from "./catalog";
import { type BindingDraft, emptyBinding, NATIVE_USENET_PROVIDER } from "./model";

interface BindingEditorProps {
  accounts: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
  bindings: BindingDraft[];
  kinds: readonly string[];
  nativeAccess?: {
    onChange: (value: string) => void;
    sources: readonly string[];
    value: string;
  };
  capabilityResults?: Readonly<Record<string, CapabilityBindingResult>>;
  onChange: (bindings: BindingDraft[]) => void;
  onTest: (configurationId: string) => Promise<boolean>;
  source?: boolean;
}

export function BindingEditor({
  accounts,
  bindings,
  capabilityResults = {},
  kinds,
  nativeAccess,
  onChange,
  onTest,
  source = false,
}: BindingEditorProps) {
  const { t } = useTranslation();
  const schemas = source ? sourceSchemas : providerSchemas;
  const label = (kind: string) =>
    t(source ? `configure.discoverySources.${kind}` : `configure.playbackProviders.${kind}`);
  const update = (index: number, binding: BindingDraft) => {
    onChange(bindings.map((current, position) => (position === index ? binding : current)));
  };
  const bindingOptions = (kind: string) => {
    const schema = schemas[kind];
    const options = schema ? defaultOptions(schema) : {};
    const nativeSource = nativeAccess?.sources[0];
    if (kind === NATIVE_USENET_PROVIDER && nativeSource) options.source = nativeSource;
    return options;
  };
  const nativeBindingIndex = bindings.findIndex(({ kind }) => kind === NATIVE_USENET_PROVIDER);

  return (
    <div className="binding-list">
      {bindings.map((binding, index) => {
        const schema = schemas[binding.kind];
        const account = binding.accountId ? accounts[binding.accountId] : undefined;
        const isNative = binding.kind === NATIVE_USENET_PROVIDER;
        const capability = capabilityResults[binding.configurationId];
        const selectedNewznabPreset =
          binding.kind === "newznab"
            ? newznabPresets.find((preset) => preset.endpoint === binding.options.endpoint)?.id
            : undefined;
        return (
          <article className="binding-card" key={binding.configurationId}>
            <div className="binding-card__header">
              <Select
                label={t(source ? "configure.binding.source" : "configure.binding.provider")}
                onValueChange={(kind) => {
                  update(index, {
                    ...emptyBinding(kind, label(kind)),
                    configurationId: binding.configurationId,
                    options: bindingOptions(kind),
                  });
                }}
                value={binding.kind}
              >
                {[...new Set([...kinds, binding.kind])].map((kind) => (
                  <option key={kind} value={kind}>
                    {label(kind)}
                  </option>
                ))}
              </Select>
              <Input
                label={t("configure.binding.displayName")}
                maxLength={64}
                onChange={(event) => update(index, { ...binding, displayName: event.target.value })}
                value={binding.displayName}
              />
              <Switch
                checked={binding.enabled}
                label={t("configure.binding.enabled")}
                onCheckedChange={(enabled) => update(index, { ...binding, enabled })}
              />
              <div className="binding-card__actions">
                <Button
                  aria-label={t("configure.binding.test")}
                  disabled={!binding.enabled}
                  onClick={() => void onTest(binding.configurationId)}
                  title={t("configure.binding.test")}
                  variant="ghost"
                >
                  <RadioTower aria-hidden="true" size={17} />
                </Button>
                <Button
                  aria-label={t("configure.binding.remove")}
                  onClick={() => onChange(bindings.filter((_, position) => position !== index))}
                  title={t("configure.binding.remove")}
                  variant="ghost"
                >
                  <Trash2 aria-hidden="true" size={17} />
                </Button>
              </div>
            </div>
            {capability ? (
              <Alert
                tone={
                  capability.eligible ? (capability.degraded ? "warning" : "success") : "danger"
                }
              >
                {capabilityReason(t, capability)}
              </Alert>
            ) : null}
            {binding.kind === "newznab" ? (
              <Select
                className="binding-card__preset"
                label={t("configure.binding.indexerPreset")}
                onValueChange={(presetId) => {
                  if (presetId === "custom") {
                    update(index, {
                      ...binding,
                      options: { ...binding.options, endpoint: "" },
                    });
                    return;
                  }
                  const preset = newznabPresets.find(
                    (candidate) => candidate.id === presetId,
                  ) as (typeof newznabPresets)[number];
                  update(index, {
                    ...binding,
                    displayName: preset.label,
                    options: { ...binding.options, endpoint: preset.endpoint },
                  });
                }}
                value={selectedNewznabPreset ?? "custom"}
              >
                <option value="custom">{t("configure.binding.customIndexer")}</option>
                {newznabPresets.map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.label}
                  </option>
                ))}
              </Select>
            ) : null}
            {schema ? (
              <>
                {account ? (
                  <p className="binding-card__account">{t("configure.binding.linkedAccount")}</p>
                ) : null}
                <div className="binding-card__fields">
                  {isNative && index === nativeBindingIndex && nativeAccess ? (
                    <Input
                      autoComplete="off"
                      label={t("configure.usenet.accessToken")}
                      onChange={(event) => nativeAccess.onChange(event.target.value)}
                      type="password"
                      value={nativeAccess.value}
                    />
                  ) : null}
                  {schema.fields.map((field) =>
                    field.type === "indexer-identity" ? (
                      <IndexerIdentityControl
                        disabled={account?.userAgentMode !== undefined}
                        key={field.key}
                        onChange={(identity) => {
                          const {
                            grabUserAgent: _grabUserAgent,
                            queryUserAgent: _queryUserAgent,
                            userAgentMode: _userAgentMode,
                            ...options
                          } = binding.options;
                          update(index, {
                            ...binding,
                            options: { ...options, ...identity },
                          });
                        }}
                        options={{ ...binding.options, ...account }}
                      />
                    ) : (
                      <OptionControl
                        {...(isNative && field.key === "source" && nativeAccess
                          ? { allowedChoices: nativeAccess.sources }
                          : {})}
                        disabled={account?.[field.key] !== undefined}
                        field={field}
                        key={field.key}
                        onChange={(value) =>
                          update(index, {
                            ...binding,
                            options: { ...binding.options, [field.key]: value },
                          })
                        }
                        value={account?.[field.key] ?? binding.options[field.key]}
                      />
                    ),
                  )}
                </div>
                {schema.serverMode &&
                (schema.serverMode !== "native" ||
                  binding.options.source === "personal_servers") ? (
                  <ServerEditor
                    disabled={account?.servers !== undefined}
                    mode={schema.serverMode}
                    onChange={(servers) =>
                      update(index, {
                        ...binding,
                        options: { ...binding.options, servers },
                      })
                    }
                    servers={
                      (account?.servers ?? binding.options.servers ?? []) as Record<
                        string,
                        unknown
                      >[]
                    }
                  />
                ) : null}
              </>
            ) : (
              <JsonOptionsEditor
                onChange={(options) => update(index, { ...binding, options })}
                options={binding.options}
              />
            )}
          </article>
        );
      })}
      <Button
        onClick={() => {
          const kind = kinds[0];
          if (!kind) return;
          onChange([
            ...bindings,
            {
              ...emptyBinding(kind, label(kind)),
              options: bindingOptions(kind),
            },
          ]);
        }}
        variant="secondary"
      >
        <Plus aria-hidden="true" size={17} />
        {t(source ? "configure.binding.addSource" : "configure.binding.addProvider")}
      </Button>
    </div>
  );
}

type IndexerIdentity = {
  grabUserAgent?: string;
  queryUserAgent?: string;
  userAgentMode: "browser" | "custom" | "stealth";
};

function IndexerIdentityControl({
  disabled,
  onChange,
  options,
}: {
  disabled: boolean;
  onChange: (identity: IndexerIdentity) => void;
  options: Readonly<Record<string, unknown>>;
}) {
  const { t } = useTranslation();
  const mode =
    options.userAgentMode === "browser" || options.userAgentMode === "custom"
      ? options.userAgentMode
      : "stealth";
  const browserUserAgent = navigator.userAgent;
  const queryUserAgent = String(options.queryUserAgent ?? browserUserAgent);
  const grabUserAgent = String(options.grabUserAgent ?? browserUserAgent);
  const choose = (nextMode: IndexerIdentity["userAgentMode"]) => {
    if (nextMode === "stealth") {
      onChange({ userAgentMode: "stealth" });
      return;
    }
    const captured = nextMode === "browser" ? browserUserAgent : queryUserAgent;
    onChange({
      grabUserAgent: nextMode === "browser" ? browserUserAgent : grabUserAgent,
      queryUserAgent: captured,
      userAgentMode: nextMode,
    });
  };
  const choices = [
    {
      description: t("configure.binding.stealthDescription"),
      icon: ShieldCheck,
      label: t("configure.binding.stealth"),
      value: "stealth" as const,
    },
    {
      description: t("configure.binding.browserUserAgentDescription"),
      icon: UserRound,
      label: t("configure.binding.browserUserAgent"),
      value: "browser" as const,
    },
    {
      description: t("configure.binding.customUserAgentDescription"),
      icon: Fingerprint,
      label: t("configure.binding.customUserAgent"),
      value: "custom" as const,
    },
  ];

  return (
    <fieldset className="indexer-identity">
      <legend>{t("configure.binding.indexerIdentity")}</legend>
      <div className="indexer-identity__choices">
        {choices.map((choice) => {
          const Icon = choice.icon;
          const selected = mode === choice.value;
          return (
            <button
              aria-pressed={selected}
              className="indexer-identity__choice"
              disabled={disabled}
              key={choice.value}
              onClick={() => choose(choice.value)}
              type="button"
            >
              <Icon aria-hidden="true" size={19} />
              <span>
                <strong>{choice.label}</strong>
                <small>{choice.description}</small>
              </span>
              {selected ? <Check aria-hidden="true" size={17} /> : null}
            </button>
          );
        })}
      </div>
      {mode === "browser" ? (
        <code className="indexer-identity__captured">{browserUserAgent}</code>
      ) : null}
      {mode === "custom" ? (
        <div className="indexer-identity__custom">
          <Input
            disabled={disabled}
            label={t("configure.fields.queryUserAgent")}
            maxLength={512}
            onChange={(event) =>
              onChange({
                grabUserAgent,
                queryUserAgent: event.target.value,
                userAgentMode: "custom",
              })
            }
            required
            value={queryUserAgent}
          />
          <Input
            disabled={disabled}
            label={t("configure.fields.grabUserAgent")}
            maxLength={512}
            onChange={(event) =>
              onChange({
                grabUserAgent: event.target.value,
                queryUserAgent,
                userAgentMode: "custom",
              })
            }
            required
            value={grabUserAgent}
          />
        </div>
      ) : null}
    </fieldset>
  );
}

function JsonOptionsEditor({
  onChange,
  options,
}: {
  onChange: (options: Record<string, unknown>) => void;
  options: Record<string, unknown>;
}) {
  const { t } = useTranslation();
  const [raw, setRaw] = useState(() => JSON.stringify(options, null, 2));
  const [error, setError] = useState(false);
  return (
    <label className="field">
      <span className="field__label">{t("configure.binding.optionsJson")}</span>
      <textarea
        aria-invalid={error}
        onBlur={() => {
          try {
            const parsed: unknown = JSON.parse(raw);
            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
              setError(true);
              return;
            }
            setError(false);
            onChange(parsed as Record<string, unknown>);
          } catch {
            setError(true);
          }
        }}
        onChange={(event) => {
          setRaw(event.target.value);
          setError(false);
        }}
        rows={5}
        value={raw}
      />
      {error ? <span className="field__error">{t("configure.binding.jsonError")}</span> : null}
    </label>
  );
}

function OptionControl({
  allowedChoices,
  disabled,
  field,
  onChange,
  value,
}: {
  allowedChoices?: readonly string[];
  disabled: boolean;
  field: OptionField;
  onChange: (value: unknown) => void;
  value: unknown;
}) {
  const { t } = useTranslation();
  const label = t(`configure.fields.${field.key}`);
  if (field.type === "checkbox") {
    return (
      <Switch
        checked={value === true}
        className={field.key === "backup" ? "server-editor__backup" : undefined}
        disabled={disabled}
        label={label}
        onCheckedChange={onChange}
      />
    );
  }
  if (field.type === "select") {
    const choices = allowedChoices
      ? field.choices?.filter(({ value }) => allowedChoices.includes(value))
      : field.choices;
    return (
      <Select
        disabled={disabled}
        label={label}
        onValueChange={onChange}
        required={field.required}
        value={String(value ?? field.defaultValue ?? "")}
      >
        {choices?.map((choice) => (
          <option key={choice.value} value={choice.value}>
            {t(`configure.options.${choice.value}`)}
          </option>
        ))}
      </Select>
    );
  }
  return (
    <Input
      disabled={disabled}
      label={label}
      max={field.max}
      min={field.min}
      onChange={(event) =>
        onChange(field.type === "number" ? event.target.valueAsNumber : event.target.value)
      }
      required={field.required}
      type={field.type === "text" || !field.type ? "text" : field.type}
      value={
        typeof value === "number" || typeof value === "string"
          ? value
          : typeof field.defaultValue === "boolean"
            ? ""
            : (field.defaultValue ?? "")
      }
    />
  );
}

function ServerEditor({
  disabled,
  mode,
  onChange,
  servers,
}: {
  disabled: boolean;
  mode: "native" | "stremio";
  onChange: (servers: Record<string, unknown>[]) => void;
  servers: Record<string, unknown>[];
}) {
  const { t } = useTranslation();
  const add = (presetId = "custom") => {
    const index = servers.length;
    const preset = NNTP_PRESETS[presetId as keyof typeof NNTP_PRESETS];
    onChange([
      ...servers,
      mode === "native"
        ? {
            backup: false,
            connections: preset.connections,
            host: preset.host,
            name: `${presetId.replaceAll("_", "-")}-${index + 1}`,
            password: "",
            pipeline: 16,
            port: 563,
            priority: index,
            tls_mode: "implicit",
            username: "",
          }
        : {
            connections: preset.connections,
            host: preset.host,
            password: "",
            port: 563,
            tls_mode: "implicit_tls",
            username: "",
          },
    ]);
  };
  return (
    <div className="server-editor">
      <h4>{t("configure.binding.servers")}</h4>
      {servers.map((server, index) => (
        // Server rows can only be appended or removed; their position is their stable identity.
        // biome-ignore lint/suspicious/noArrayIndexKey: server rows are not reordered
        <div className="server-editor__row" key={index}>
          <div className="server-editor__row-header">
            <strong>
              {String(server.name || server.host || t("configure.binding.customServer"))}
            </strong>
            <Button
              aria-label={t("configure.binding.removeServer")}
              disabled={disabled}
              onClick={() => {
                onChange(servers.filter((_, position) => position !== index));
              }}
              title={t("configure.binding.removeServer")}
              variant="ghost"
            >
              <Trash2 aria-hidden="true" size={16} />
            </Button>
          </div>
          <div className="server-editor__fields">
            {nntpFields[mode].map((field) => (
              <OptionControl
                disabled={disabled}
                field={field}
                key={field.key}
                onChange={(value) =>
                  onChange(
                    servers.map((current, position) =>
                      position === index ? { ...current, [field.key]: value } : current,
                    ),
                  )
                }
                value={server[field.key]}
              />
            ))}
          </div>
        </div>
      ))}
      <div className="server-editor__add">
        <Select
          disabled={disabled || servers.length >= (mode === "native" ? 16 : 8)}
          label={t("configure.binding.serverPreset")}
          onValueChange={add}
          value="custom"
        >
          {Object.keys(NNTP_PRESETS).map((presetId) => (
            <option key={presetId} value={presetId}>
              {t(
                presetId === "custom"
                  ? "configure.binding.customServer"
                  : `configure.nntpPresets.${presetId}`,
              )}
            </option>
          ))}
        </Select>
        <Button
          disabled={disabled || servers.length >= (mode === "native" ? 16 : 8)}
          onClick={() => add()}
        >
          <Plus aria-hidden="true" size={16} />
          {t("configure.binding.addCustomServer")}
        </Button>
      </div>
    </div>
  );
}

const CUSTOM_NNTP_PRESET = { connections: 4, host: "" } as const;
const NNTP_PRESETS = {
  custom: CUSTOM_NNTP_PRESET,
  newshosting: { connections: 8, host: "news.newshosting.com" },
  eweka: { connections: 8, host: "news.eweka.nl" },
  usenetserver: { connections: 8, host: "news.usenetserver.com" },
  giganews: { connections: 8, host: "news.giganews.com" },
  easynews: { connections: 8, host: "news.easynews.com" },
  newsdemon: { connections: 8, host: "news.newsdemon.com" },
  frugalusenet: { connections: 8, host: "news.frugalusenet.com" },
  usenetexpress: { connections: 8, host: "news.usenetexpress.com" },
  usenet_farm: { connections: 8, host: "news.usenet.farm" },
  vipernews: { connections: 8, host: "news.vipernews.com" },
} as const;
