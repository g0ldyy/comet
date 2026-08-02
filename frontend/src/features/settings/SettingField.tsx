import { RotateCcw } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { SettingView } from "../../api/generated/contracts";
import { Checkbox } from "../../components/ui/Checkbox";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Switch } from "../../components/ui/Switch";
import { CollectionField } from "./CollectionField";
import { JsonField } from "./JsonField";
import type { SettingDraft } from "./model";
import { SecretInput } from "./SecretInput";

export function SettingField({
  draft,
  error,
  onChange,
  setting,
}: {
  draft: SettingDraft;
  error: string | null;
  onChange: (draft: SettingDraft) => void;
  setting: SettingView;
}) {
  const { t } = useTranslation();
  const { catalog } = setting;
  const hint = catalog.unit ? t(`settings.units.${catalog.unit}`) : undefined;
  const disabled = catalog.deployment_owned || draft.reset;
  const common = {
    disabled,
    label: catalog.key,
    ...(hint ? { hint } : {}),
  };
  const booleanOptions: Array<[string, string]> = [
    ["", t("settings.nullValue")],
    ["true", t("settings.trueValue")],
    ["false", t("settings.falseValue")],
  ];
  let editor: ReactNode;

  const collectionEditor =
    catalog.value_kind === "string_or_list" ||
    (catalog.value_kind === "list" && catalog.item_kind === "string") ? (
      <CollectionField disabled={disabled} draft={draft} onChange={onChange} setting={setting} />
    ) : null;

  if (collectionEditor) {
    editor = collectionEditor;
  } else if (catalog.value_kind === "boolean" && !catalog.nullable) {
    const checked = draft.text === "true";
    editor = (
      <div className="setting-boolean">
        <span>{t(checked ? "settings.trueValue" : "settings.falseValue")}</span>
        <Switch
          checked={checked}
          compact
          disabled={disabled}
          label={catalog.key}
          onCheckedChange={(value) => onChange({ ...draft, text: String(value) })}
        />
      </div>
    );
  } else if (catalog.value_kind === "boolean") {
    editor = (
      <fieldset className="setting-mode setting-mode--three">
        <legend className="visually-hidden">{catalog.key}</legend>
        {booleanOptions.map(([value, label]) => (
          <button
            aria-pressed={draft.text === value}
            disabled={disabled}
            key={value}
            onClick={() => onChange({ ...draft, text: value })}
            type="button"
          >
            {label}
          </button>
        ))}
      </fieldset>
    );
  } else if (catalog.value_kind === "enum") {
    editor = (
      <Select {...common} onValueChange={(text) => onChange({ ...draft, text })} value={draft.text}>
        {catalog.nullable ? <option value="">{t("settings.nullValue")}</option> : null}
        {catalog.choices?.map((choice) => (
          <option key={String(choice)} value={String(choice)}>
            {choice === true
              ? t("settings.trueValue")
              : choice === false
                ? t("settings.falseValue")
                : t(`settings.modes.${String(choice)}`, { defaultValue: String(choice) })}
          </option>
        ))}
      </Select>
    );
  } else if (["json", "list", "map"].includes(catalog.value_kind)) {
    editor = (
      <JsonField
        disabled={disabled}
        draft={draft}
        error={error}
        onChange={onChange}
        setting={setting}
      />
    );
  } else if (catalog.sensitive) {
    editor = (
      <SecretInput
        disabled={disabled}
        {...(hint ? { hint } : {})}
        label={catalog.key}
        onChange={(event) => onChange({ ...draft, text: event.target.value })}
        value={draft.text}
      />
    );
  } else if (catalog.input_kind === "multiline") {
    editor = (
      <label className="field">
        <span className="field__label">{catalog.key}</span>
        <textarea
          disabled={disabled}
          onChange={(event) => onChange({ ...draft, text: event.target.value })}
          value={draft.text}
        />
      </label>
    );
  } else {
    editor = (
      <Input
        {...common}
        inputMode={
          catalog.value_kind === "integer" || catalog.value_kind === "number"
            ? "decimal"
            : undefined
        }
        onChange={(event) => onChange({ ...draft, text: event.target.value })}
        step={
          catalog.value_kind === "integer"
            ? "1"
            : catalog.value_kind === "number"
              ? "any"
              : undefined
        }
        type={
          catalog.value_kind === "integer" || catalog.value_kind === "number"
            ? "number"
            : catalog.input_kind === "url"
              ? "url"
              : "text"
        }
        value={draft.text}
      />
    );
  }

  return (
    <article
      className={`setting-card ${draft.reset ? "setting-card--reset" : ""}`}
      data-search-target={catalog.key}
      tabIndex={-1}
    >
      <div className="setting-card__summary">
        <div className="setting-card__badges">
          <span>{t(`settings.sources.${setting.source}`)}</span>
          {catalog.sensitive ? <span>{t("settings.secret")}</span> : null}
          {catalog.restart_required ? <span>{t("settings.restart")}</span> : null}
          {catalog.deployment_owned ? <span>{t("settings.deploymentOwned")}</span> : null}
        </div>
        <h3>
          <code>{catalog.key}</code>
        </h3>
      </div>
      <div className="setting-card__editor">
        {editor}
        {error ? <span className="field__error">{error}</span> : null}
        <div className="setting-card__actions">
          {!catalog.deployment_owned && setting.source === "dashboard" ? (
            <Checkbox
              checked={draft.reset}
              label={t("settings.resetInherited")}
              onChange={(event) => onChange({ ...draft, reset: event.target.checked })}
            />
          ) : null}
          {draft.reset ? <RotateCcw aria-hidden="true" size={15} /> : null}
        </div>
      </div>
    </article>
  );
}
