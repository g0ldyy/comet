import { Plus, X } from "lucide-react";
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import type { SettingView } from "../../api/generated/contracts";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import type { SettingDraft } from "./model";
import { SecretInput } from "./SecretInput";

function stringItems(text: string): string[] {
  return JSON.parse(text) as string[];
}

export function CollectionField({
  disabled,
  draft,
  onChange,
  setting,
}: {
  disabled: boolean;
  draft: SettingDraft;
  onChange: (draft: SettingDraft) => void;
  setting: SettingView;
}) {
  const { t } = useTranslation();
  const { catalog } = setting;
  const multiple = catalog.value_kind === "list" || draft.text.trimStart().startsWith("[");
  const nextItemId = useRef(0);
  const itemIds = useRef<string[]>([]);

  const items = multiple ? stringItems(draft.text) : [];
  while (itemIds.current.length < items.length) {
    itemIds.current.push(`${catalog.key}-${nextItemId.current}`);
    nextItemId.current += 1;
  }
  itemIds.current.length = items.length;
  const updateItems = (next: string[]) => onChange({ ...draft, text: JSON.stringify(next) });

  return (
    <div className="collection-field">
      {catalog.value_kind === "string_or_list" ? (
        <fieldset className="setting-mode">
          <legend className="visually-hidden">{catalog.key}</legend>
          <button
            aria-pressed={!multiple}
            disabled={disabled}
            onClick={() => onChange({ ...draft, text: items[0] ?? "" })}
            type="button"
          >
            {t("settings.singleValue")}
          </button>
          <button
            aria-pressed={multiple}
            disabled={disabled}
            onClick={() => updateItems(multiple ? items : draft.text === "" ? [] : [draft.text])}
            type="button"
          >
            {t("settings.multipleValues")}
          </button>
        </fieldset>
      ) : null}
      {multiple ? (
        <div className="collection-field__items">
          {items.map((item, index) => (
            <div className="collection-field__row" key={itemIds.current[index]}>
              {catalog.sensitive ? (
                <SecretInput
                  disabled={disabled}
                  label={`${catalog.key} ${index + 1}`}
                  labelHidden
                  onChange={(event) => {
                    const next = [...items];
                    next[index] = event.target.value;
                    updateItems(next);
                  }}
                  value={item}
                />
              ) : (
                <Input
                  disabled={disabled}
                  label={`${catalog.key} ${index + 1}`}
                  labelHidden
                  onChange={(event) => {
                    const next = [...items];
                    next[index] = event.target.value;
                    updateItems(next);
                  }}
                  type={catalog.input_kind === "url" ? "url" : "text"}
                  value={item}
                />
              )}
              <Button
                aria-label={t("settings.removeValue", { index: index + 1 })}
                disabled={disabled}
                onClick={() => {
                  itemIds.current.splice(index, 1);
                  updateItems(items.filter((_, itemIndex) => itemIndex !== index));
                }}
                variant="ghost"
              >
                <X aria-hidden="true" size={15} />
              </Button>
            </div>
          ))}
          <Button
            disabled={disabled}
            onClick={() => updateItems([...items, ""])}
            variant="secondary"
          >
            <Plus aria-hidden="true" size={15} />
            {t("settings.addValue")}
          </Button>
        </div>
      ) : catalog.sensitive ? (
        <SecretInput
          disabled={disabled}
          label={catalog.key}
          onChange={(event) => onChange({ ...draft, text: event.target.value })}
          value={draft.text}
        />
      ) : (
        <Input
          disabled={disabled}
          label={catalog.key}
          onChange={(event) => onChange({ ...draft, text: event.target.value })}
          type={catalog.input_kind === "url" ? "url" : "text"}
          value={draft.text}
        />
      )}
    </div>
  );
}
