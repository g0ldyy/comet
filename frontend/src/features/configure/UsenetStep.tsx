import { useTranslation } from "react-i18next";
import type { ConfiguratorBootstrapData } from "../../api/generated/contracts";
import { Alert } from "../../components/ui/Alert";
import type { CapabilityBindingResult } from "./api";
import { BindingEditor } from "./BindingEditor";
import { type BindingDraft, NATIVE_USENET_PROVIDER } from "./model";

export function UsenetStep({
  accounts,
  bootstrap,
  capabilityResults = {},
  nativeAccessToken,
  onNativeAccessTokenChange,
  onProvidersChange,
  onSourcesChange,
  onTestBinding,
  providers,
  sources,
}: {
  accounts: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
  bootstrap: ConfiguratorBootstrapData;
  capabilityResults?: Readonly<Record<string, CapabilityBindingResult>>;
  nativeAccessToken: string;
  onNativeAccessTokenChange: (token: string) => void;
  onProvidersChange: (providers: BindingDraft[]) => void;
  onSourcesChange: (sources: BindingDraft[]) => void;
  onTestBinding: (configurationId: string) => Promise<boolean>;
  providers: BindingDraft[];
  sources: BindingDraft[];
}) {
  const { t } = useTranslation();
  const providerKinds = bootstrap.usenet_provider_kinds
    .filter((kind) => kind !== NATIVE_USENET_PROVIDER || bootstrap.capabilities.native_usenet)
    .toSorted(
      (left, right) =>
        Number(right === NATIVE_USENET_PROVIDER) - Number(left === NATIVE_USENET_PROVIDER),
    );

  if (!bootstrap.capabilities.usenet) {
    return (
      <section className="configuration-fields">
        <Alert tone="info">{t("configure.usenet.unavailable")}</Alert>
      </section>
    );
  }

  return (
    <section className="configuration-fields">
      <div className="binding-group">
        <h3>{t("configure.usenet.providers")}</h3>
        <BindingEditor
          accounts={accounts}
          bindings={providers}
          capabilityResults={capabilityResults}
          kinds={providerKinds}
          {...(bootstrap.capabilities.native_usenet
            ? {
                nativeAccess: {
                  onChange: onNativeAccessTokenChange,
                  sources: bootstrap.native_usenet_sources,
                  value: nativeAccessToken,
                },
              }
            : {})}
          onChange={onProvidersChange}
          onTest={onTestBinding}
        />
      </div>
      <div className="binding-group">
        <h3>{t("configure.usenet.sources")}</h3>
        <BindingEditor
          accounts={accounts}
          bindings={sources}
          capabilityResults={capabilityResults}
          kinds={bootstrap.usenet_source_kinds}
          onChange={onSourcesChange}
          onTest={onTestBinding}
          source
        />
      </div>
    </section>
  );
}
