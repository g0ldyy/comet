import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConfiguratorBootstrapData } from "../../api/generated/contracts";
import type { BindingDraft } from "./model";
import { UsenetStep } from "./UsenetStep";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(cleanup);

const bootstrap = {
  capabilities: {
    native_usenet: true,
    proxy_debrid_stream: true,
    stremio_api_prefix: "",
    torrent_streams: true,
    usenet: true,
  },
  debrid_services: [],
  default_configuration: {},
  languages: {},
  native_usenet_sources: ["personal_servers"],
  resolutions: [],
  result_formats: [],
  usenet_provider_kinds: ["torbox_usenet", "comet_native_usenet", "easynews"],
  usenet_source_kinds: [],
} as ConfiguratorBootstrapData;

describe("UsenetStep", () => {
  it("shows the precise capability result on its binding card", () => {
    const configurationId = "62b71d41-3bb6-4007-afd7-b53b0f71d2b1";
    render(
      <UsenetStep
        accounts={{}}
        bootstrap={bootstrap}
        capabilityResults={{
          [configurationId]: {
            configuration_id: configurationId,
            degraded: false,
            display_name: "Comet Usenet",
            eligible: false,
            error_code: "native_access_token_required",
            retry_after: null,
            state: "auth_failed",
          },
        }}
        nativeAccessToken=""
        onNativeAccessTokenChange={vi.fn()}
        onProvidersChange={vi.fn()}
        onSourcesChange={vi.fn()}
        onTestBinding={vi.fn()}
        providers={[
          {
            configurationId,
            displayName: "Comet Usenet",
            enabled: true,
            kind: "comet_native_usenet",
            options: { servers: [], source: "personal_servers" },
          },
        ]}
        sources={[]}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "configure.capabilities.nativeAccessTokenRequired",
    );
  });

  it("configures native access inside the first Comet Usenet provider", () => {
    const onProvidersChange = vi.fn();
    function TestStep() {
      const [providers, setProviders] = useState<BindingDraft[]>([]);
      const [token, setToken] = useState("");
      return (
        <UsenetStep
          accounts={{}}
          bootstrap={bootstrap}
          nativeAccessToken={token}
          onNativeAccessTokenChange={setToken}
          onProvidersChange={(nextProviders) => {
            onProvidersChange(nextProviders);
            setProviders(nextProviders);
          }}
          onSourcesChange={vi.fn()}
          onTestBinding={vi.fn()}
          providers={providers}
          sources={[]}
        />
      );
    }
    render(<TestStep />);

    expect(screen.queryByLabelText("configure.usenet.accessToken")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "configure.binding.addProvider" }));
    expect(onProvidersChange).toHaveBeenCalledWith([
      expect.objectContaining({
        kind: "comet_native_usenet",
        options: { source: "personal_servers" },
      }),
    ]);
    expect(screen.getByLabelText("configure.usenet.accessToken")).toBeVisible();
    expect(screen.getByText("configure.fields.source")).toBeVisible();
    expect(screen.getByText("configure.options.personal_servers")).toBeVisible();
    expect(screen.queryByText("configure.options.instance_pool")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("configure.usenet.accessToken"), {
      target: { value: "native-token" },
    });
    expect(screen.getByLabelText("configure.usenet.accessToken")).toHaveValue("native-token");

    fireEvent.click(screen.getByRole("button", { name: "configure.binding.addCustomServer" }));
    const host = screen.getByLabelText("configure.fields.host");
    host.focus();
    fireEvent.change(host, { target: { value: "news.example.com" } });
    expect(host).toHaveFocus();
    expect(host).toHaveValue("news.example.com");
  });

  it("defaults indexers to Stealth and can select or customize the browser User-Agent", () => {
    const onSourcesChange = vi.fn();
    function TestStep() {
      const [sources, setSources] = useState<BindingDraft[]>([]);
      return (
        <UsenetStep
          accounts={{}}
          bootstrap={{ ...bootstrap, usenet_source_kinds: ["newznab"] }}
          nativeAccessToken=""
          onNativeAccessTokenChange={vi.fn()}
          onProvidersChange={vi.fn()}
          onSourcesChange={(nextSources) => {
            onSourcesChange(nextSources);
            setSources(nextSources);
          }}
          onTestBinding={vi.fn()}
          providers={[]}
          sources={sources}
        />
      );
    }
    render(<TestStep />);

    fireEvent.click(screen.getByRole("button", { name: "configure.binding.addSource" }));
    expect(onSourcesChange).toHaveBeenLastCalledWith([
      expect.objectContaining({
        kind: "newznab",
        options: expect.objectContaining({ userAgentMode: "stealth" }),
      }),
    ]);
    expect(screen.getByRole("button", { name: /configure.binding.stealth/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: /configure.binding.browserUserAgent/ }));
    expect(onSourcesChange).toHaveBeenLastCalledWith([
      expect.objectContaining({
        options: expect.objectContaining({
          grabUserAgent: navigator.userAgent,
          queryUserAgent: navigator.userAgent,
          userAgentMode: "browser",
        }),
      }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: /configure.binding.customUserAgent/ }));
    fireEvent.change(screen.getByLabelText("configure.fields.queryUserAgent"), {
      target: { value: "Custom query UA" },
    });
    expect(onSourcesChange).toHaveBeenLastCalledWith([
      expect.objectContaining({
        options: expect.objectContaining({
          queryUserAgent: "Custom query UA",
          userAgentMode: "custom",
        }),
      }),
    ]);
  });
});
