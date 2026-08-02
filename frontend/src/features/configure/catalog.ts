import { NATIVE_USENET_PROVIDER } from "./model";

export interface OptionChoice {
  value: string;
}

export interface OptionField {
  choices?: readonly OptionChoice[];
  defaultValue?: boolean | number | string;
  key: string;
  max?: number;
  min?: number;
  required?: boolean;
  type?: "checkbox" | "indexer-identity" | "number" | "password" | "select" | "text" | "url";
}

export interface BindingSchema {
  fields: readonly OptionField[];
  serverMode?: "native" | "stremio";
}

export const newznabPresets = [
  { endpoint: "https://abnzb.com/api", id: "abnzb", label: "abNZB" },
  { endpoint: "https://api.althub.co.za/api", id: "althub", label: "altHUB" },
  { endpoint: "https://clubnzb.com/api", id: "clubnzb", label: "ClubNZB" },
  { endpoint: "https://api.dognzb.cr/api", id: "dognzb", label: "DOGnzb" },
  { endpoint: "https://drunkenslug.com/api", id: "drunkenslug", label: "DrunkenSlug" },
  {
    endpoint: "https://www.gingadaddy.com/api",
    id: "gingadaddy",
    label: "GingaDADDY",
  },
  { endpoint: "https://www.miatrix.com/api", id: "miatrix", label: "Miatrix" },
  {
    endpoint: "https://ninjacentral.co.za/api",
    id: "ninjacentral",
    label: "NinjaCentral",
  },
  { endpoint: "https://api.nzb.life/api", id: "nzb_life", label: "Nzb.life" },
  { endpoint: "https://nzb.su/api", id: "nzb_su", label: "NZB.su" },
  { endpoint: "https://nzbfinder.ws/api", id: "nzbfinder", label: "NZBFinder" },
  { endpoint: "https://api.nzbgeek.info/api", id: "nzbgeek", label: "NZBgeek" },
  { endpoint: "https://nzbnest.com/api", id: "nzbnest", label: "NzbNest" },
  { endpoint: "https://nzbnoob.com/api", id: "nzbnoob", label: "NzbNoob" },
  { endpoint: "https://api.nzbplanet.net/api", id: "nzbplanet", label: "NzbPlanet" },
  { endpoint: "https://nzbstars.com/api", id: "nzbstars", label: "NZBStars" },
  {
    endpoint: "https://treasure-maps.com/api",
    id: "treasure_maps",
    label: "Treasure Maps",
  },
  {
    endpoint: "https://www.tabula-rasa.pw/api/v1/api",
    id: "tabula_rasa",
    label: "Tabula Rasa",
  },
  {
    endpoint: "https://www.usenet-crawler.com/api",
    id: "usenet_crawler",
    label: "Usenet Crawler",
  },
] as const;

const newznabFields: readonly OptionField[] = [
  { key: "endpoint", required: true, type: "url" },
  { key: "apiKey", required: true, type: "password" },
  {
    defaultValue: "stealth",
    key: "userAgentMode",
    required: true,
    type: "indexer-identity",
  },
  { defaultValue: 100, key: "maxResults", max: 1000, min: 1, required: true, type: "number" },
  { defaultValue: 100, key: "pageSize", max: 100, min: 1, required: true, type: "number" },
  {
    defaultValue: 2,
    key: "requestsPerSecond",
    max: 100,
    min: 1,
    required: true,
    type: "number",
  },
  {
    defaultValue: 1000,
    key: "dailyQueryLimit",
    max: 1_000_000,
    min: 1,
    required: true,
    type: "number",
  },
  {
    defaultValue: 100,
    key: "dailyGrabLimit",
    max: 1_000_000,
    min: 1,
    required: true,
    type: "number",
  },
];

const easynewsSchema: BindingSchema = {
  fields: [
    { key: "username", required: true },
    { key: "password", required: true, type: "password" },
  ],
};

export const providerSchemas: Readonly<Record<string, BindingSchema>> = {
  altmount: {
    fields: [
      { key: "internalBaseUrl", required: true, type: "url" },
      { key: "apiKey", required: true, type: "password" },
      { key: "streamBaseUrl", type: "url" },
      { defaultValue: "stremio", key: "category", required: true },
    ],
  },
  [NATIVE_USENET_PROVIDER]: {
    fields: [
      {
        choices: [{ value: "instance_pool" }, { value: "personal_servers" }],
        defaultValue: "instance_pool",
        key: "source",
        required: true,
        type: "select",
      },
    ],
    serverMode: "native",
  },
  easynews: easynewsSchema,
  nzbdav: {
    fields: [
      { key: "internalBaseUrl", required: true, type: "url" },
      { key: "streamBaseUrl", type: "url" },
      { key: "sabApiKey", required: true, type: "password" },
      { key: "webdavUsername", required: true },
      { key: "webdavPassword", required: true, type: "password" },
      { defaultValue: "movies", key: "movieCategory", required: true },
      { defaultValue: "tv", key: "seriesCategory", required: true },
    ],
  },
  stremio_nntp: {
    fields: [],
    serverMode: "stremio",
  },
  stremthru_newz: {
    fields: [
      { key: "baseUrl", required: true, type: "url" },
      { key: "authToken", required: true, type: "password" },
    ],
  },
  torbox_usenet: {
    fields: [{ key: "apiKey", required: true, type: "password" }],
  },
};

export const sourceSchemas: Readonly<Record<string, BindingSchema>> = {
  easynews: easynewsSchema,
  newznab: { fields: newznabFields },
  nzbhydra2: { fields: newznabFields },
  prowlarr_usenet: { fields: newznabFields },
  stremio_addon: {
    fields: [
      { key: "baseUrl", type: "url" },
      { key: "manifestUrl", type: "url" },
      { key: "authorization", type: "password" },
      { defaultValue: 3, key: "maxResults", max: 10, min: 1, required: true, type: "number" },
    ],
  },
};

export const nntpFields: Readonly<Record<"native" | "stremio", readonly OptionField[]>> = {
  native: [
    { key: "name", required: true },
    { key: "host", required: true },
    { key: "port", min: 1, max: 65_535, required: true, type: "number" },
    {
      choices: [{ value: "implicit" }, { value: "starttls" }, { value: "plaintext" }],
      key: "tls_mode",
      required: true,
      type: "select",
    },
    { key: "username" },
    { key: "password", type: "password" },
    { key: "connections", max: 100, min: 1, required: true, type: "number" },
    { key: "priority", max: 1000, min: 0, required: true, type: "number" },
    { key: "pipeline", max: 16, min: 1, required: true, type: "number" },
    { key: "backup", type: "checkbox" },
  ],
  stremio: [
    { key: "host", required: true },
    { key: "port", min: 1, max: 65_535, required: true, type: "number" },
    {
      choices: [{ value: "implicit_tls" }, { value: "plaintext" }],
      key: "tls_mode",
      required: true,
      type: "select",
    },
    { key: "username", required: true },
    { key: "password", required: true, type: "password" },
    { key: "connections", max: 100, min: 1, required: true, type: "number" },
  ],
};

export function defaultOptions(schema: BindingSchema): Record<string, unknown> {
  return Object.fromEntries(
    schema.fields
      .filter((field) => field.defaultValue !== undefined || field.type === "checkbox")
      .map((field) => [field.key, field.defaultValue ?? false]),
  );
}
