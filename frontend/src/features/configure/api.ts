import { z } from "zod";
import { apiRequest, rawJsonRequest } from "../../api/client";
import type { ConfigModel, ConfiguratorBootstrapData } from "../../api/generated/contracts";

export function getConfiguratorBootstrap(): Promise<ConfiguratorBootstrapData> {
  return apiRequest("/api/v1/configure/bootstrap", { scope: "configure" });
}

export function validateConfiguration(configuration: ConfigModel): Promise<ConfigModel> {
  return apiRequest("/api/v1/configure/validate", {
    body: JSON.stringify({ configuration }),
    method: "POST",
    scope: "configure",
  });
}

const capabilityBindingResult = z.object({
  configuration_id: z.string(),
  degraded: z.boolean(),
  display_name: z.string(),
  eligible: z.boolean(),
  error_code: z.string().nullable(),
  retry_after: z.number().nullable(),
  state: z.string(),
});

const capabilityResult = z.object({
  bindings: z.array(capabilityBindingResult).optional(),
  code: z.string().optional(),
  ok: z.boolean(),
  version: z.literal(1),
});

export type CapabilityBindingResult = z.infer<typeof capabilityBindingResult>;
export type CapabilityResult = z.infer<typeof capabilityResult>;

export async function testCapabilities(
  configuration: ConfigModel,
  apiPrefix: string,
  configurationId?: string,
): Promise<CapabilityResult> {
  const query = configurationId ? `?configuration_id=${encodeURIComponent(configurationId)}` : "";
  const response = await rawJsonRequest(
    `${apiPrefix}/configure/capabilities/test${query}` as `/${string}`,
    {
      body: JSON.stringify(configuration),
      method: "POST",
    },
  );
  return capabilityResult.parse(response.payload);
}

export async function associateKodi(code: string, manifestUrl: string): Promise<void> {
  const response = await rawJsonRequest("/kodi/associate_manifest", {
    body: JSON.stringify({ code, manifest_url: manifestUrl }),
    method: "POST",
  });
  if (!response.ok) {
    throw new Error("kodi_association_failed");
  }
}
