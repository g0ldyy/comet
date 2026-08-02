import { useTranslation } from "react-i18next";
import type { ConfiguratorBootstrapData } from "../../api/generated/contracts";
import { Input } from "../../components/ui/Input";
import { MultiSelect, type MultiSelectOption } from "../../components/ui/MultiSelect";
import { Switch } from "../../components/ui/Switch";
import type { ConfigureFormValues } from "./model";

type ChangeConfiguration = <Key extends keyof ConfigureFormValues>(
  key: Key,
  value: ConfigureFormValues[Key],
) => void;

export function PreferencesStep({
  bootstrap,
  onChange,
  showDebridOptions,
  values,
}: {
  bootstrap: ConfiguratorBootstrapData;
  onChange: ChangeConfiguration;
  showDebridOptions: boolean;
  values: ConfigureFormValues;
}) {
  const { t } = useTranslation();
  const resolutionOptions: MultiSelectOption[] = bootstrap.resolutions.map((resolution) => ({
    label: resolution.replace(/^r/, ""),
    value: resolution,
  }));
  const resultFieldOptions: MultiSelectOption[] = bootstrap.result_formats.map((field) => ({
    label: t(`configure.resultFields.${field}`),
    value: field,
  }));
  return (
    <section className="configuration-fields">
      <MultiSelect
        className="multi-select-field--wide"
        emptyLabel={t("configure.results.noneSelected")}
        label={t("configure.results.resolutions")}
        onChange={(resolutions) => onChange("resolutions", resolutions)}
        options={resolutionOptions}
        removeLabel={(label) => t("actions.removeSelection", { label })}
        searchLabel={t("configure.results.searchResolutions")}
        searchable={false}
        selected={values.resolutions}
      />
      <div className="field-grid">
        <Input
          label={t("configure.results.maxPerResolution")}
          min={0}
          onChange={(event) => onChange("maxResultsPerResolution", event.target.valueAsNumber)}
          type="number"
          value={values.maxResultsPerResolution}
        />
        <Input
          label={t("configure.results.maxSize")}
          min={0}
          onChange={(event) => onChange("maxSizeGb", event.target.valueAsNumber)}
          step="0.1"
          type="number"
          value={values.maxSizeGb}
        />
      </div>
      <MultiSelect
        className="multi-select-field--wide"
        emptyLabel={t("configure.results.noneSelected")}
        label={t("configure.results.fields")}
        onChange={(resultFormat) => onChange("resultFormat", resultFormat)}
        options={resultFieldOptions}
        removeLabel={(label) => t("actions.removeSelection", { label })}
        searchLabel={t("configure.results.searchFields")}
        searchable={false}
        selected={values.resultFormat}
      />
      <ResultFormatPreview
        emptyLabel={t("configure.results.emptyPreview")}
        selected={values.resultFormat}
      />
      <div className="switch-list">
        {showDebridOptions ? (
          <Switch
            checked={values.cachedOnly}
            label={t("configure.results.cachedOnly")}
            onCheckedChange={(checked) => onChange("cachedOnly", checked)}
          />
        ) : null}
        <Switch
          checked={values.removeTrash}
          label={t("configure.results.filters")}
          onCheckedChange={(checked) => onChange("removeTrash", checked)}
        />
        <Switch
          checked={values.allowEnglishInLanguages}
          label={t("configure.results.allowEnglish")}
          onCheckedChange={(checked) => onChange("allowEnglishInLanguages", checked)}
        />
        <Switch
          checked={values.removeUnknownLanguages}
          label={t("configure.results.removeUnknown")}
          onCheckedChange={(checked) => onChange("removeUnknownLanguages", checked)}
        />
      </div>
    </section>
  );
}

function ResultFormatPreview({ emptyLabel, selected }: { emptyLabel: string; selected: string[] }) {
  const { t } = useTranslation();
  const previewFields: Readonly<Record<string, string>> = {
    audio_info: t("configure.preview.audio"),
    languages: t("configure.preview.languages"),
    quality_info: t("configure.preview.quality"),
    release_group: t("configure.preview.releaseGroup"),
    seeders: t("configure.preview.seeders"),
    size: t("configure.preview.size"),
    title: t("configure.preview.title"),
    tracker: t("configure.preview.tracker"),
    video_info: t("configure.preview.video"),
  };
  const field = (name: string) => (selected.includes(name) ? previewFields[name] : undefined);
  const lines = [
    field("title"),
    [field("video_info"), field("audio_info")].filter(Boolean).join(" | "),
    [field("quality_info"), field("release_group")].filter(Boolean).join(" | "),
    [field("seeders"), field("size"), field("tracker")].filter(Boolean).join(" · "),
    field("languages"),
  ].filter(Boolean);
  return (
    <figure className="result-preview">
      <pre>{lines.join("\n") || emptyLabel}</pre>
    </figure>
  );
}

export function LanguageStep({
  bootstrap,
  onChange,
  values,
}: {
  bootstrap: ConfiguratorBootstrapData;
  onChange: ChangeConfiguration;
  values: ConfigureFormValues;
}) {
  const { i18n, t } = useTranslation();
  const languageNames = new Intl.DisplayNames([i18n.resolvedLanguage ?? "en"], {
    type: "language",
  });
  const options: MultiSelectOption[] = Object.entries(bootstrap.languages).map(([code, emoji]) => ({
    label: `${emoji} ${code === "multi" ? t("configure.languages.multi") : (languageNames.of(code) ?? code)}`,
    value: code,
  }));
  const fields = [
    ["requiredLanguages", "required"],
    ["allowedLanguages", "allowed"],
    ["excludedLanguages", "excluded"],
    ["preferredLanguages", "preferred"],
  ] as const;

  return (
    <section className="language-groups">
      {fields.map(([key, label]) => (
        <MultiSelect
          emptyLabel={t("configure.languages.none")}
          key={key}
          label={t(`configure.languages.${label}`)}
          onChange={(selected) => onChange(key, selected)}
          options={options}
          removeLabel={(optionLabel) => t("actions.removeSelection", { label: optionLabel })}
          searchLabel={t("configure.languages.search")}
          selected={values[key]}
        />
      ))}
    </section>
  );
}
