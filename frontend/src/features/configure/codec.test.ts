import { describe, expect, it } from "vitest";
import type { ConfigModel, ConfiguratorBootstrapData } from "../../api/generated/contracts";
import { decodeConfiguration, encodeConfiguration, manifestLocation } from "./codec";
import { configurationDocument, DIRECT_TORRENT_SERVICE, formValues } from "./model";

const bootstrap = {
  capabilities: {
    native_usenet: true,
    proxy_debrid_stream: true,
    stremio_api_prefix: "",
    torrent_streams: true,
    usenet: true,
  },
  debrid_services: ["realdebrid", "torbox"],
  default_configuration: {
    cachedOnly: false,
    debridServices: [],
    debridStreamProxyPassword: "",
    enableTorrent: true,
    languages: { allowed: [], exclude: [], preferred: [], required: [] },
    maxResultsPerResolution: 0,
    maxSize: 0,
    options: {
      allow_english_in_languages: false,
      remove_unknown_languages: false,
    },
    removeTrash: true,
    resolutions: {},
    resultFormat: ["all"],
    schemaVersion: 1,
    scrapeDebridAccountTorrents: false,
  },
  languages: { en: "🇬🇧", fr: "🇫🇷" },
  native_usenet_sources: ["instance_pool", "personal_servers"],
  resolutions: ["r2160p", "r1080p", "unknown"],
  result_formats: ["title", "size"],
  usenet_provider_kinds: ["torbox_usenet", "comet_native_usenet"],
  usenet_source_kinds: ["newznab"],
} as ConfiguratorBootstrapData;

describe("configuration codec", () => {
  it("round trips URL-safe UTF-8 documents without padding", () => {
    const configuration = {
      debridApiKey: "clé-déjà-installée",
      debridService: "realdebrid",
    };
    const { encoded } = encodeConfiguration(configuration);

    expect(encoded).not.toMatch(/[+/=]/);
    expect(decodeConfiguration(encoded)).toEqual(configuration);
  });

  it("decodes historical development base64 configurations", () => {
    const configuration = {
      debridApiKey: "existing-development-install-key",
      debridService: "realdebrid",
      enableTorrent: true,
      schemaVersion: 1,
    } satisfies ConfigModel;
    const bytes = new TextEncoder().encode(JSON.stringify(configuration));
    const historical = btoa(String.fromCharCode(...bytes));

    expect(decodeConfiguration(historical)).toEqual(configuration);
  });

  it("compresses realistic self-contained configurations", () => {
    const configuration = {
      accounts: {
        debrid: { apiKey: "debrid-secret-credential", kind: "realdebrid" },
        indexer: {
          apiKey: "indexer-secret-credential",
          endpoint: "https://indexer.example/api",
          kind: "indexer",
        },
      },
      discoverySources: [
        {
          accountId: "indexer",
          configurationId: "33333333-3333-4333-8333-333333333333",
          displayName: "My indexer",
          enabled: true,
          kind: "newznab",
          options: { endpoint: "https://indexer.example/api" },
        },
      ],
      enabledTransports: ["bittorrent", "usenet"],
      languages: { allowed: [], exclude: [], preferred: ["en"], required: ["fr"] },
      playbackProviders: [
        {
          accountId: "debrid",
          configurationId: "11111111-1111-4111-8111-111111111111",
          displayName: "Living room",
          enabled: true,
          kind: "realdebrid",
          options: {},
        },
      ],
      resultFormat: ["title", "video_info", "audio_info", "size"],
      schemaVersion: 2,
    } satisfies ConfigModel;
    const legacyLength = Math.ceil(
      new TextEncoder().encode(JSON.stringify(configuration)).length * (4 / 3),
    );

    const { encoded } = encodeConfiguration(configuration);

    expect(encoded).toMatch(/^z1\.[A-Za-z0-9_-]+$/);
    expect(encoded.length).toBeLessThan(legacyLength * 0.45);
    expect(decodeConfiguration(encoded)).toEqual(configuration);
  });

  it("uses the public manifest when no custom configuration is provided", () => {
    const configuration = bootstrap.default_configuration;

    expect(manifestLocation(undefined, "", false).url).toBe("http://localhost/manifest.json");
    expect(manifestLocation(configuration, "", true).url).toMatch(
      /^stremio:\/\/localhost\/z1\.[A-Za-z0-9_-]+\/manifest\.json$/,
    );
  });
});

describe("configuration form mapping", () => {
  it("preserves v2 identifiers, linked accounts, disabled entries and unknown kinds", () => {
    const loaded = {
      accounts: {
        debrid: { apiKey: "debrid-secret", kind: "realdebrid" },
        unknown: { kind: "future-account", token: "future-secret" },
      },
      discoverySources: [
        {
          accountId: "unknown",
          configurationId: "33333333-3333-4333-8333-333333333333",
          displayName: "Future source",
          enabled: false,
          kind: "future_source",
          options: { mode: "future" },
        },
      ],
      enabledTransports: ["bittorrent", "usenet"],
      playbackProviders: [
        {
          accountId: "debrid",
          configurationId: "11111111-1111-4111-8111-111111111111",
          displayName: "Living room",
          enabled: true,
          kind: "realdebrid",
          options: {},
        },
        {
          configurationId: "22222222-2222-4222-8222-222222222222",
          displayName: "Dormant TorBox",
          enabled: false,
          kind: "torbox",
          options: {},
        },
        {
          accountId: "unknown",
          configurationId: "44444444-4444-4444-8444-444444444444",
          displayName: "Future playback",
          enabled: true,
          kind: "future_provider",
          options: { mode: "future" },
        },
      ],
      schemaVersion: 2,
    } as ConfigModel;

    const values = formValues(loaded, bootstrap);
    const rebuilt = configurationDocument(values, bootstrap, loaded);

    expect(rebuilt.playbackProviders).toEqual(loaded.playbackProviders);
    expect(rebuilt.discoverySources).toEqual(loaded.discoverySources);
    expect(rebuilt.accounts).toEqual(loaded.accounts);
  });

  it("replaces a dormant debrid provider instead of duplicating its service", () => {
    const loaded = {
      enabledTransports: ["bittorrent"],
      playbackProviders: [
        {
          configurationId: "11111111-1111-4111-8111-111111111111",
          displayName: "Dormant TorBox",
          enabled: false,
          kind: "torbox",
          options: {},
        },
      ],
      schemaVersion: 2,
    } as ConfigModel;
    const values = formValues(loaded, bootstrap);
    values.debridServices.push({
      accountId: "torbox-account",
      apiKey: "secret",
      configurationId: "22222222-2222-4222-8222-222222222222",
      service: "torbox",
    });

    const rebuilt = configurationDocument(values, bootstrap, loaded);

    expect(rebuilt.playbackProviders).toHaveLength(1);
    expect(rebuilt.playbackProviders?.[0]).toMatchObject({
      configurationId: "22222222-2222-4222-8222-222222222222",
      enabled: true,
      kind: "torbox",
    });
  });

  it("keeps legacy v1 output compact and ordered", () => {
    const legacy = {
      ...bootstrap.default_configuration,
      debridApiKey: "old-secret",
      debridService: "realdebrid",
    } as ConfigModel;
    const values = formValues(legacy, bootstrap);
    values.debridServices = [
      {
        accountId: "account",
        apiKey: "secret",
        configurationId: "11111111-1111-4111-8111-111111111111",
        service: "realdebrid",
      },
    ];

    const rebuilt = configurationDocument(values, bootstrap, legacy);
    expect(rebuilt.schemaVersion).toBe(1);
    expect(rebuilt.debridServices).toEqual([{ apiKey: "secret", service: "realdebrid" }]);
    expect(rebuilt.playbackProviders).toBeUndefined();
    expect(rebuilt.debridApiKey).toBeUndefined();
    expect(rebuilt.debridService).toBeUndefined();
  });

  it("reactivates a preserved direct torrent provider without duplicating its identifier", () => {
    const loaded = {
      ...bootstrap.default_configuration,
      enabledTransports: ["bittorrent"],
      playbackProviders: [
        {
          configurationId: "22222222-2222-4222-8222-222222222222",
          displayName: "Direct torrent",
          enabled: false,
          kind: "direct_torrent",
          options: {},
        },
      ],
      schemaVersion: 2,
    } as ConfigModel;
    const values = formValues(loaded, bootstrap);
    values.debridServices.push({
      accountId: "",
      apiKey: "",
      configurationId: "22222222-2222-4222-8222-222222222222",
      service: DIRECT_TORRENT_SERVICE,
    });
    const rebuilt = configurationDocument(values, bootstrap, loaded);

    expect(rebuilt.playbackProviders?.filter(({ kind }) => kind === "direct_torrent")).toHaveLength(
      1,
    );
    expect(rebuilt.playbackProviders?.[0]?.configurationId).toBe(
      "22222222-2222-4222-8222-222222222222",
    );
  });

  it("drops inactive Usenet bindings after Usenet is disabled", () => {
    const loaded = {
      accounts: {
        debrid: { apiKey: "secret", kind: "realdebrid" },
      },
      discoverySources: [
        {
          configurationId: "33333333-3333-4333-8333-333333333333",
          displayName: "Indexer",
          enabled: true,
          kind: "newznab",
          options: { apiKey: "indexer-secret", endpoint: "https://indexer.test/api" },
        },
      ],
      enabledTransports: ["bittorrent", "usenet"],
      playbackProviders: [
        {
          accountId: "debrid",
          configurationId: "11111111-1111-4111-8111-111111111111",
          displayName: "Real-Debrid",
          enabled: true,
          kind: "realdebrid",
          options: {},
        },
        {
          configurationId: "22222222-2222-4222-8222-222222222222",
          displayName: "Incomplete Usenet provider",
          enabled: true,
          kind: "torbox_usenet",
          options: { apiKey: "" },
        },
      ],
      schemaVersion: 2,
    } as ConfigModel;
    const values = formValues(loaded, bootstrap);
    values.usenetEnabled = false;

    const rebuilt = configurationDocument(values, bootstrap, loaded);

    expect(rebuilt.enabledTransports).toEqual(["bittorrent"]);
    expect(rebuilt.playbackProviders).toHaveLength(1);
    expect(rebuilt.playbackProviders?.[0]?.kind).toBe("realdebrid");
    expect(rebuilt.discoverySources).toEqual([]);
    expect(rebuilt.accounts).toEqual({ debrid: { apiKey: "secret", kind: "realdebrid" } });
  });

  it("emits the native token only for a configured Comet Usenet provider", () => {
    const values = formValues(bootstrap.default_configuration, bootstrap);
    values.schemaVersion = 2;
    values.usenetEnabled = true;
    values.nativeAccessToken = "native-secret";

    expect(configurationDocument(values, bootstrap).nativeAccessToken).toBeUndefined();

    values.usenetProviders.push({
      configurationId: "22222222-2222-4222-8222-222222222222",
      displayName: "Comet Usenet",
      enabled: true,
      kind: "comet_native_usenet",
      options: { source: "instance_pool" },
    });
    expect(configurationDocument(values, bootstrap).nativeAccessToken).toBe("native-secret");
  });
});
