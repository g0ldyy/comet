import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConfiguratorBootstrapData } from "../../api/generated/contracts";
import { PlaybackStep } from "./PlaybackStep";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(cleanup);

const services = [
  "realdebrid",
  "torbox",
  "alldebrid",
  "debridlink",
  "premiumize",
  "debrider",
  "easydebrid",
  "offcloud",
  "pikpak",
];

const bootstrap = {
  capabilities: {
    native_usenet: false,
    proxy_debrid_stream: true,
    stremio_api_prefix: "",
    torrent_streams: true,
    usenet: false,
  },
  debrid_services: services,
  default_configuration: {},
  languages: {},
  native_usenet_sources: [],
  resolutions: [],
  result_formats: [],
  usenet_provider_kinds: [],
  usenet_source_kinds: [],
} as ConfiguratorBootstrapData;

describe("PlaybackStep", () => {
  it("offers the account and credential pages for every debrid service", () => {
    const { container } = render(
      <PlaybackStep
        bootstrap={bootstrap}
        debridServices={services.map((service) => ({
          accountId: `account-${service}`,
          apiKey: "",
          configurationId: `configuration-${service}`,
          service,
        }))}
        onDebridServicesChange={vi.fn()}
        onProxyPasswordChange={vi.fn()}
        onScrapeChange={vi.fn()}
        proxyPassword=""
        scrape={false}
        showDebridOptions={true}
      />,
    );

    const links = [
      ...container.querySelectorAll<HTMLAnchorElement>(".debrid-credentials__links a"),
    ];
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "https://real-debrid.com/apitoken",
      "https://real-debrid.com/?id=16161532",
      "https://torbox.app/settings",
      "https://torbox.app/subscription?referral=1ffb2238-1c5f-402e-a2ce-3d7a86c52d02",
      "https://alldebrid.com/apikeys",
      "https://debrid-link.com/webapp/apikey",
      "https://debrid-link.fr/id/G7mli",
      "https://premiumize.me/account",
      "https://debrider.app/dashboard/account",
      "https://paradise-cloud.com/products/easydebrid",
      "https://offcloud.com/#/account",
      "https://mypikpak.com",
    ]);
    expect(links.every((link) => link.target === "_blank" && link.rel === "noreferrer")).toBe(true);
    expect(
      container.querySelector<HTMLInputElement>('[data-debrid-id="configuration-pikpak"] input'),
    ).toHaveAttribute("type", "text");
    expect(
      container.querySelector<HTMLInputElement>(
        '[data-debrid-id="configuration-realdebrid"] input',
      ),
    ).toHaveAttribute("type", "password");
  });

  it("adds the next available service and hides the action when all are configured", () => {
    const onChange = vi.fn();
    const props = {
      bootstrap,
      onDebridServicesChange: onChange,
      onProxyPasswordChange: vi.fn(),
      onScrapeChange: vi.fn(),
      proxyPassword: "",
      scrape: false,
      showDebridOptions: true,
    };
    const { rerender } = render(
      <PlaybackStep
        {...props}
        debridServices={[
          {
            accountId: "account-realdebrid",
            apiKey: "secret",
            configurationId: "configuration-realdebrid",
            service: "realdebrid",
          },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "configure.playback.addService" }));
    expect(onChange.mock.calls[0]?.[0]?.at(-1)?.service).toBe("torbox");

    rerender(
      <PlaybackStep
        {...props}
        debridServices={[
          ...services.map((service) => ({
            accountId: `account-${service}`,
            apiKey: "secret",
            configurationId: `configuration-${service}`,
            service,
          })),
          {
            accountId: "",
            apiKey: "",
            configurationId: "configuration-direct",
            service: "direct_torrent",
          },
        ]}
      />,
    );
    expect(
      screen.queryByRole("button", { name: "configure.playback.addService" }),
    ).not.toBeInTheDocument();
  });

  it("hides Debrid library search until an account is configured", () => {
    render(
      <PlaybackStep
        bootstrap={bootstrap}
        debridServices={[]}
        onDebridServicesChange={vi.fn()}
        onProxyPasswordChange={vi.fn()}
        onScrapeChange={vi.fn()}
        proxyPassword=""
        scrape={false}
        showDebridOptions={false}
      />,
    );

    expect(
      screen.queryByRole("switch", { name: "configure.playback.scrapeLibraries" }),
    ).not.toBeInTheDocument();
  });
});
