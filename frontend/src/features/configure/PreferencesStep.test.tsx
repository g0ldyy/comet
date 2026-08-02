import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConfiguratorBootstrapData } from "../../api/generated/contracts";
import type { ConfigureFormValues } from "./model";
import { PreferencesStep } from "./PreferencesStep";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(cleanup);

const bootstrap = {
  capabilities: {
    native_usenet: false,
    proxy_debrid_stream: false,
    stremio_api_prefix: "",
    torrent_streams: true,
    usenet: false,
  },
  debrid_services: [],
  default_configuration: {},
  languages: {},
  native_usenet_sources: [],
  resolutions: [],
  result_formats: [],
  usenet_provider_kinds: [],
  usenet_source_kinds: [],
} as ConfiguratorBootstrapData;

const values: ConfigureFormValues = {
  allowEnglishInLanguages: false,
  allowedLanguages: [],
  bittorrentEnabled: true,
  cachedOnly: false,
  debridServices: [],
  excludedLanguages: [],
  maxResultsPerResolution: 0,
  maxSizeGb: 0,
  nativeAccessToken: "",
  preferredLanguages: [],
  proxyPassword: "",
  removeTrash: true,
  removeUnknownLanguages: false,
  requiredLanguages: [],
  resolutions: [],
  resultFormat: [],
  schemaVersion: 1,
  scrapeDebridAccountTorrents: false,
  usenetEnabled: false,
  usenetProviders: [],
  usenetSources: [],
};

describe("PreferencesStep", () => {
  it("hides cached-only results until a Debrid account is configured", () => {
    render(
      <PreferencesStep
        bootstrap={bootstrap}
        onChange={vi.fn()}
        showDebridOptions={false}
        values={values}
      />,
    );

    expect(
      screen.queryByRole("switch", { name: "configure.results.cachedOnly" }),
    ).not.toBeInTheDocument();
  });
});
