import type { TFunction } from "i18next";
import type { CapabilityBindingResult, CapabilityResult } from "./api";

const errorMessages: Readonly<Record<string, string>> = {
  account_suspended: "configure.capabilities.accountSuspended",
  addon_manifest_invalid: "configure.capabilities.configurationInvalid",
  addon_unavailable: "configure.capabilities.temporarilyUnavailable",
  api_key_invalid: "configure.capabilities.credentialsRejected",
  api_key_missing: "configure.capabilities.credentialsRequired",
  api_key_rejected: "configure.capabilities.credentialsRejected",
  api_key_required: "configure.capabilities.credentialsRequired",
  binding_not_testable: "configure.capabilities.bindingNotTestable",
  configuration_invalid: "configure.capabilities.configurationInvalid",
  configuration_too_large: "configure.capabilities.configurationTooLarge",
  configuration_required: "configure.capabilities.configurationRequired",
  configure_auth_required: "configure.capabilities.configureAuthRequired",
  credentials_rejected: "configure.capabilities.credentialsRejected",
  credentials_required: "configure.capabilities.credentialsRequired",
  discovery_configuration_invalid: "configure.capabilities.configurationInvalid",
  discovery_credentials_invalid: "configure.capabilities.credentialsRequired",
  engine_unavailable: "configure.capabilities.engineUnavailable",
  native_access_token_rejected: "configure.capabilities.nativeAccessTokenRejected",
  native_access_token_required: "configure.capabilities.nativeAccessTokenRequired",
  native_api_required: "configure.capabilities.nativeApiRequired",
  newz_unavailable: "configure.capabilities.usenetPlanRequired",
  nntp_servers_required: "configure.capabilities.serversRequired",
  no_compatible_playback_provider: "configure.capabilities.noCompatiblePlaybackProvider",
  nzb_export_base_url_required: "configure.capabilities.exportUrlRequired",
  personal_servers_disabled: "configure.capabilities.personalServersDisabled",
  personal_servers_required: "configure.capabilities.serversRequired",
  plan_incompatible: "configure.capabilities.planIncompatible",
  provider_caps_incompatible: "configure.capabilities.indexerIncompatible",
  provider_configuration_invalid: "configure.capabilities.configurationInvalid",
  provider_limit_exhausted: "configure.capabilities.rateLimited",
  provider_query_unsupported: "configure.capabilities.indexerIncompatible",
  provider_unavailable: "configure.capabilities.temporarilyUnavailable",
  servers_unavailable: "configure.capabilities.serversUnavailable",
  source_required: "configure.capabilities.sourceRequired",
  usenet_plan_required: "configure.capabilities.usenetPlanRequired",
  usenet_unavailable: "configure.capabilities.usenetPlanRequired",
  validation_failed: "configure.capabilities.validationFailed",
  validation_incomplete: "configure.capabilities.validationIncomplete",
  validation_timeout: "configure.capabilities.validationTimeout",
  validation_unavailable: "configure.capabilities.temporarilyUnavailable",
};

export function capabilityReason(t: TFunction, result: CapabilityBindingResult): string {
  if (result.eligible) {
    return t(
      result.degraded
        ? "configure.capabilities.connectionDegraded"
        : "configure.messages.connectionAvailable",
    );
  }
  const key = result.error_code ? errorMessages[result.error_code] : undefined;
  if (key) return t(key);
  if (result.state === "auth_failed") return t("configure.capabilities.credentialsRejected");
  if (result.state === "transiently_unreachable") {
    return t("configure.capabilities.temporarilyUnavailable");
  }
  return t("configure.capabilities.unknown", { code: result.error_code ?? result.state });
}

export function capabilityFailureMessage(t: TFunction, result: CapabilityResult): string {
  const failures = (result.bindings ?? []).filter((binding) => !binding.eligible);
  if (failures.length > 0) {
    return failures
      .map((binding) =>
        t("configure.capabilities.bindingFailure", {
          name: binding.display_name,
          reason: capabilityReason(t, binding),
        }),
      )
      .join(" · ");
  }
  const key = result.code ? errorMessages[result.code] : undefined;
  if (result.code === "usenet_unavailable") {
    return t("configure.capabilities.instanceUnavailable");
  }
  return key ? t(key) : t("configure.messages.connectionError");
}
