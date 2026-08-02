import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";
import type { CapabilityBindingResult, CapabilityResult } from "./api";
import { capabilityFailureMessage, capabilityReason } from "./capabilities";

const translations: Readonly<Record<string, string>> = {
  "configure.capabilities.bindingFailure": "{{name}}: {{reason}}",
  "configure.capabilities.credentialsRejected": "Credentials rejected",
  "configure.capabilities.nativeAccessTokenRequired": "Access token required",
  "configure.capabilities.noCompatiblePlaybackProvider": "No playback provider",
  "configure.capabilities.temporarilyUnavailable": "Temporarily unavailable",
  "configure.messages.connectionAvailable": "Connection available",
};

const t = ((key: string, values?: Record<string, unknown>) =>
  Object.entries(values ?? {}).reduce(
    (message, [name, value]) => message.replace(`{{${name}}}`, String(value)),
    translations[key] ?? key,
  )) as TFunction;

const result = (
  configurationId: string,
  overrides: Partial<CapabilityBindingResult> = {},
): CapabilityBindingResult => ({
  configuration_id: configurationId,
  degraded: false,
  display_name: configurationId,
  eligible: false,
  error_code: "native_access_token_required",
  retry_after: null,
  state: "auth_failed",
  ...overrides,
});

describe("capability messages", () => {
  it("translates precise provider failures and degraded states", () => {
    expect(capabilityReason(t, result("native"))).toBe("Access token required");
    expect(
      capabilityReason(
        t,
        result("native", {
          degraded: true,
          eligible: true,
          error_code: "provider_unavailable",
          state: "transiently_unreachable",
        }),
      ),
    ).toBe("configure.capabilities.connectionDegraded");
  });

  it("associates every failure with its configured display name", () => {
    const response: CapabilityResult = {
      bindings: [
        result("native", { display_name: "Comet Usenet" }),
        result("indexer", {
          display_name: "NZBgeek",
          error_code: "no_compatible_playback_provider",
          state: "plan_incompatible",
        }),
      ],
      ok: false,
      version: 1,
    };

    expect(capabilityFailureMessage(t, response)).toBe(
      "Comet Usenet: Access token required · NZBgeek: No playback provider",
    );
  });

  it("resolves debrid capability identifiers to their visible service names", () => {
    const response: CapabilityResult = {
      bindings: [
        result("eb1cf994-4023-4c8a-89e8-e36007e5acd4", {
          display_name: "TorBox",
          error_code: "credentials_rejected",
        }),
      ],
      ok: false,
      version: 1,
    };

    expect(capabilityFailureMessage(t, response)).toBe("TorBox: Credentials rejected");
  });
});
