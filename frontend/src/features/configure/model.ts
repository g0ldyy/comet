import type {
  ConfigModel,
  ConfiguratorBootstrapData,
  DiscoverySourceEntry,
  PlaybackProviderEntry,
} from "../../api/generated/contracts";

export interface DebridDraft {
  accountId: string;
  apiKey: string;
  configurationId: string;
  service: string;
}

export interface BindingDraft {
  accountId?: string;
  configurationId: string;
  displayName: string;
  enabled: boolean;
  kind: string;
  options: Record<string, unknown>;
}

export interface ConfigureFormValues {
  allowEnglishInLanguages: boolean;
  bittorrentEnabled: boolean;
  cachedOnly: boolean;
  debridServices: DebridDraft[];
  excludedLanguages: string[];
  maxResultsPerResolution: number;
  maxSizeGb: number;
  nativeAccessToken: string;
  preferredLanguages: string[];
  proxyPassword: string;
  removeTrash: boolean;
  removeUnknownLanguages: boolean;
  requiredLanguages: string[];
  allowedLanguages: string[];
  resolutions: string[];
  resultFormat: string[];
  schemaVersion: 1 | 2;
  scrapeDebridAccountTorrents: boolean;
  usenetEnabled: boolean;
  usenetProviders: BindingDraft[];
  usenetSources: BindingDraft[];
}

export const DIRECT_TORRENT_SERVICE = "direct_torrent";
export const NATIVE_USENET_PROVIDER = "comet_native_usenet";

type MutableConfiguration = {
  -readonly [Key in keyof ConfigModel]: ConfigModel[Key];
};

function id(): string {
  return crypto.randomUUID();
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

export function formValues(
  configuration: ConfigModel,
  bootstrap: ConfiguratorBootstrapData,
): ConfigureFormValues {
  const accounts = configuration.accounts ?? {};
  const providers = configuration.playbackProviders ?? [];
  const debridKinds = new Set(bootstrap.debrid_services);
  const debridServices = providers
    .filter((provider) => debridKinds.has(provider.kind) && provider.enabled !== false)
    .map((provider) => ({
      accountId: provider.accountId ?? id(),
      apiKey: String(accounts[provider.accountId ?? ""]?.apiKey ?? ""),
      configurationId: provider.configurationId,
      service: provider.kind,
    }));

  if (debridServices.length === 0) {
    const legacy = configuration.debridServices?.length
      ? configuration.debridServices
      : configuration.debridService && configuration.debridService !== "torrent"
        ? [{ service: configuration.debridService, apiKey: configuration.debridApiKey ?? "" }]
        : [];
    debridServices.push(
      ...legacy.map((entry) => ({
        accountId: id(),
        apiKey: entry.apiKey ?? "",
        configurationId: id(),
        service: entry.service,
      })),
    );
  }

  const schemaVersion = configuration.schemaVersion === 2 ? 2 : 1;
  const directTorrent = providers.find(
    (provider) => provider.kind === DIRECT_TORRENT_SERVICE && provider.enabled !== false,
  );
  const directTorrentEnabled =
    schemaVersion === 2
      ? directTorrent !== undefined
      : configuration.enableTorrent === true || configuration.debridService === "torrent";
  if (directTorrentEnabled) {
    debridServices.push({
      accountId: "",
      apiKey: "",
      configurationId: directTorrent?.configurationId ?? id(),
      service: DIRECT_TORRENT_SERVICE,
    });
  }

  const languages = configuration.languages ?? {};
  const options = configuration.options ?? {};
  return {
    allowEnglishInLanguages: options.allow_english_in_languages === true,
    allowedLanguages: stringArray(languages.allowed),
    bittorrentEnabled:
      schemaVersion === 1
        ? bootstrap.capabilities.torrent_streams
        : (configuration.enabledTransports ?? []).includes("bittorrent"),
    cachedOnly: configuration.cachedOnly === true,
    debridServices,
    excludedLanguages: stringArray(languages.exclude),
    maxResultsPerResolution: configuration.maxResultsPerResolution ?? 0,
    maxSizeGb: (configuration.maxSize ?? 0) / 1_073_741_824,
    nativeAccessToken: configuration.nativeAccessToken ?? "",
    preferredLanguages: stringArray(languages.preferred),
    proxyPassword: configuration.debridStreamProxyPassword ?? "",
    removeTrash: configuration.removeTrash !== false,
    removeUnknownLanguages: options.remove_unknown_languages === true,
    requiredLanguages: stringArray(languages.required),
    resolutions: bootstrap.resolutions.filter(
      (resolution) => configuration.resolutions?.[resolution] !== false,
    ),
    resultFormat:
      configuration.resultFormat?.[0] === "all"
        ? [...bootstrap.result_formats]
        : [...(configuration.resultFormat ?? bootstrap.result_formats)],
    schemaVersion,
    scrapeDebridAccountTorrents: configuration.scrapeDebridAccountTorrents === true,
    usenetEnabled:
      schemaVersion === 2 && (configuration.enabledTransports ?? []).includes("usenet"),
    usenetProviders: providers
      .filter(
        (provider) => !debridKinds.has(provider.kind) && provider.kind !== DIRECT_TORRENT_SERVICE,
      )
      .map(bindingDraft),
    usenetSources: (configuration.discoverySources ?? []).map(bindingDraft),
  };
}

function bindingDraft(binding: PlaybackProviderEntry | DiscoverySourceEntry): BindingDraft {
  return {
    ...(binding.accountId ? { accountId: binding.accountId } : {}),
    configurationId: binding.configurationId,
    displayName: binding.displayName ?? binding.kind,
    enabled: binding.enabled !== false,
    kind: binding.kind,
    options: { ...binding.options },
  };
}

export function emptyBinding(kind: string, displayName: string): BindingDraft {
  return {
    configurationId: id(),
    displayName,
    enabled: true,
    kind,
    options: {},
  };
}

function compactResultFormat(selected: string[], all: readonly string[]): string[] {
  const selectedFields = new Set(selected);
  return selectedFields.size === all.length && all.every((value) => selectedFields.has(value))
    ? ["all"]
    : selected;
}

export function configurationDocument(
  values: ConfigureFormValues,
  bootstrap: ConfiguratorBootstrapData,
  loaded?: ConfigModel,
): ConfigModel {
  const debridServices = values.debridServices.filter(
    (entry) => entry.service !== DIRECT_TORRENT_SERVICE,
  );
  const directTorrent = values.debridServices.find(
    (entry) => entry.service === DIRECT_TORRENT_SERVICE,
  );
  const common: ConfigModel = {
    ...loaded,
    cachedOnly: values.cachedOnly,
    debridStreamProxyPassword: values.proxyPassword,
    languages: {
      allowed: values.allowedLanguages,
      exclude: values.excludedLanguages,
      preferred: values.preferredLanguages,
      required: values.requiredLanguages,
    },
    maxResultsPerResolution: values.maxResultsPerResolution,
    maxSize: values.maxSizeGb * 1_073_741_824,
    options: {
      allow_english_in_languages: values.allowEnglishInLanguages,
      remove_unknown_languages: values.removeUnknownLanguages,
    },
    removeTrash: values.removeTrash,
    resolutions: Object.fromEntries(
      bootstrap.resolutions
        .filter((resolution) => !values.resolutions.includes(resolution))
        .map((resolution) => [resolution, false]),
    ),
    resultFormat: compactResultFormat(values.resultFormat, bootstrap.result_formats),
    scrapeDebridAccountTorrents: values.scrapeDebridAccountTorrents,
  };

  if (values.schemaVersion === 1 && values.bittorrentEnabled && !values.usenetEnabled) {
    const document: MutableConfiguration = {
      ...common,
      debridServices: debridServices.map(({ apiKey, service }) => ({ apiKey, service })),
      enableTorrent: directTorrent !== undefined,
      schemaVersion: 1,
    };
    delete document.accounts;
    delete document.debridApiKey;
    delete document.debridService;
    delete document.discoverySources;
    delete document.enabledTransports;
    delete document.nativeAccessToken;
    delete document.playbackProviders;
    return document;
  }

  const accounts: Record<string, Record<string, unknown>> = {
    ...(loaded?.accounts ?? {}),
  };
  const playbackProviders: PlaybackProviderEntry[] = debridServices.map((entry) => {
    const previous = loaded?.playbackProviders?.find(
      ({ configurationId }) => configurationId === entry.configurationId,
    );
    accounts[entry.accountId] = { apiKey: entry.apiKey, kind: entry.service };
    return {
      accountId: entry.accountId,
      configurationId: entry.configurationId,
      displayName: previous?.displayName ?? entry.service,
      enabled: true,
      kind: entry.service,
      options: {},
    };
  });
  if (directTorrent) {
    playbackProviders.push({
      configurationId: directTorrent.configurationId,
      displayName: DIRECT_TORRENT_SERVICE,
      enabled: true,
      kind: DIRECT_TORRENT_SERVICE,
      options: {},
    });
  }
  const activePlaybackKinds = new Set(playbackProviders.map(({ kind }) => kind));
  playbackProviders.push(
    ...(loaded?.playbackProviders ?? []).filter(
      (provider) =>
        provider.enabled === false &&
        (bootstrap.debrid_services.includes(provider.kind) ||
          provider.kind === DIRECT_TORRENT_SERVICE) &&
        !activePlaybackKinds.has(provider.kind),
    ),
  );
  if (values.usenetEnabled) {
    playbackProviders.push(...values.usenetProviders.map(bindingDocument));
  }
  const discoverySources = values.usenetEnabled ? values.usenetSources.map(bindingDocument) : [];
  const referencedAccounts = new Set(
    [...playbackProviders, ...discoverySources]
      .map((binding) => binding.accountId)
      .filter((accountId): accountId is string => accountId !== null && accountId !== undefined),
  );
  const nativeAccessToken =
    values.usenetEnabled &&
    values.nativeAccessToken &&
    values.usenetProviders.some(({ kind }) => kind === NATIVE_USENET_PROVIDER)
      ? values.nativeAccessToken
      : "";

  const document: MutableConfiguration = {
    ...common,
    accounts: Object.fromEntries(
      Object.entries(accounts).filter(([accountId]) => referencedAccounts.has(accountId)),
    ),
    discoverySources,
    enabledTransports: [
      ...(values.bittorrentEnabled ? ["bittorrent"] : []),
      ...(values.usenetEnabled ? ["usenet"] : []),
    ],
    ...(nativeAccessToken ? { nativeAccessToken } : {}),
    playbackProviders,
    schemaVersion: 2,
  };
  delete document.debridApiKey;
  delete document.debridService;
  delete document.debridServices;
  delete document.enableTorrent;
  return document;
}

function bindingDocument(binding: BindingDraft): PlaybackProviderEntry {
  return {
    ...(binding.accountId ? { accountId: binding.accountId } : {}),
    configurationId: binding.configurationId,
    displayName: binding.displayName,
    enabled: binding.enabled,
    kind: binding.kind,
    options: binding.options,
  };
}
