import { Braces } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SettingView } from "../../api/generated/contracts";
import { Button } from "../../components/ui/Button";
import type { SettingDraft } from "./model";
import { SecretToggle } from "./SecretToggle";

export function JsonField({
  disabled,
  draft,
  error,
  onChange,
  setting,
}: {
  disabled: boolean;
  draft: SettingDraft;
  error: string | null;
  onChange: (draft: SettingDraft) => void;
  setting: SettingView;
}) {
  const { t } = useTranslation();
  const [revealed, setRevealed] = useState(false);
  const format = () =>
    onChange({ ...draft, text: JSON.stringify(JSON.parse(draft.text), null, 2) });

  return (
    <label className="field json-field">
      <span className="field__label">{setting.catalog.key}</span>
      <span className="json-field__toolbar">
        <span>
          <Braces aria-hidden="true" size={14} />
          {t(`settings.dataTypes.${setting.catalog.value_kind}`)}
        </span>
        <span className="json-field__actions">
          {setting.catalog.sensitive ? (
            <SecretToggle onToggle={() => setRevealed((current) => !current)} revealed={revealed} />
          ) : null}
          <Button disabled={disabled || error !== null} onClick={format} variant="ghost">
            {t("settings.formatJson")}
          </Button>
        </span>
      </span>
      <textarea
        aria-invalid={error ? true : undefined}
        disabled={disabled}
        onChange={(event) => onChange({ ...draft, text: event.target.value })}
        className={setting.catalog.sensitive && !revealed ? "secret-textarea" : undefined}
        spellCheck={false}
        value={draft.text}
      />
    </label>
  );
}
