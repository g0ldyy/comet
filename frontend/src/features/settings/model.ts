import { z } from "zod";
import type { SettingsMutationRequest, SettingView } from "../../api/generated/contracts";

export interface SettingDraft {
  reset: boolean;
  text: string;
}

export function settingDraft(
  drafts: Readonly<Record<string, SettingDraft>>,
  key: string,
): SettingDraft {
  return drafts[key] as SettingDraft;
}

const importDocument = z.strictObject({
  settings: z.record(z.string(), z.unknown()),
});

export function editableText(setting: SettingView): string {
  if (setting.value === null || setting.value === undefined) return "";
  if (
    setting.catalog.value_kind === "string" ||
    setting.catalog.value_kind === "enum" ||
    setting.catalog.value_kind === "string_or_list"
  ) {
    if (Array.isArray(setting.value)) return JSON.stringify(setting.value);
    return String(setting.value);
  }
  if (setting.catalog.value_kind === "boolean") {
    return setting.value ? "true" : "false";
  }
  if (setting.catalog.value_kind === "integer" || setting.catalog.value_kind === "number") {
    return String(setting.value);
  }
  return JSON.stringify(setting.value, null, 2);
}

export function initialDrafts(settings: ReadonlyArray<SettingView>): Record<string, SettingDraft> {
  return Object.fromEntries(
    settings.map((setting) => [setting.catalog.key, { reset: false, text: editableText(setting) }]),
  );
}

function parseDraft(setting: SettingView, draft: SettingDraft): unknown {
  const value = draft.text.trim();
  if (value === "" && setting.catalog.nullable) return null;
  switch (setting.catalog.value_kind) {
    case "boolean":
      return value === "true";
    case "integer":
      return Number(value);
    case "number":
      return Number(value);
    case "list":
    case "map":
    case "json":
      return JSON.parse(value);
    case "string_or_list":
      return value.startsWith("[") ? JSON.parse(value) : draft.text;
    case "enum": {
      const choice = setting.catalog.choices?.find((item) => String(item) === value);
      return choice ?? draft.text;
    }
    default:
      return draft.text;
  }
}

function matchesItemKind(item: unknown, kind: string | null | undefined): boolean {
  if (kind === "string") return typeof item === "string";
  if (kind === "integer") return Number.isSafeInteger(item);
  if (kind === "number") return typeof item === "number" && Number.isFinite(item);
  if (kind === "boolean") return typeof item === "boolean";
  return true;
}

export function draftError(setting: SettingView, draft: SettingDraft): string | null {
  if (draft.reset) return null;
  const text = draft.text.trim();
  if (
    (setting.catalog.value_kind === "integer" || setting.catalog.value_kind === "number") &&
    text === "" &&
    !setting.catalog.nullable
  ) {
    return "number";
  }
  if (
    setting.catalog.value_kind === "boolean" &&
    text !== "true" &&
    text !== "false" &&
    !(setting.catalog.nullable && text === "")
  ) {
    return "boolean";
  }
  if (
    setting.catalog.value_kind === "enum" &&
    !setting.catalog.choices?.some((choice) => String(choice) === text) &&
    !(setting.catalog.nullable && text === "")
  ) {
    return "choice";
  }
  try {
    const value = parseDraft(setting, draft);
    if (
      (setting.catalog.value_kind === "integer" &&
        (!Number.isInteger(value) || !Number.isSafeInteger(value))) ||
      (setting.catalog.value_kind === "number" &&
        (typeof value !== "number" || !Number.isFinite(value)))
    ) {
      return "number";
    }
    if (
      setting.catalog.value_kind === "list" &&
      (!Array.isArray(value) ||
        !value.every((item) => matchesItemKind(item, setting.catalog.item_kind)))
    ) {
      return "list";
    }
    if (
      setting.catalog.value_kind === "string_or_list" &&
      typeof value !== "string" &&
      (!Array.isArray(value) ||
        !value.every((item) => matchesItemKind(item, setting.catalog.item_kind)))
    ) {
      return "list";
    }
    if (
      setting.catalog.value_kind === "map" &&
      (typeof value !== "object" || value === null || Array.isArray(value))
    ) {
      return "object";
    }
  } catch {
    return "json";
  }
  return null;
}

export function changed(setting: SettingView, draft: SettingDraft): boolean {
  return draft.reset || draft.text !== editableText(setting);
}

export function mutationDocument(
  settings: ReadonlyArray<SettingView>,
  drafts: Readonly<Record<string, SettingDraft>>,
): SettingsMutationRequest {
  const updates: Record<string, unknown> = {};
  const reset: string[] = [];
  for (const setting of settings) {
    const draft = settingDraft(drafts, setting.catalog.key);
    if (draft.reset) {
      reset.push(setting.catalog.key);
    } else if (changed(setting, draft)) {
      updates[setting.catalog.key] = parseDraft(setting, draft);
    }
  }
  return { reset, updates };
}

export function exportSettings(settings: ReadonlyArray<SettingView>): string {
  return JSON.stringify(
    {
      settings: Object.fromEntries(
        settings
          .filter(
            (setting) =>
              setting.source === "dashboard" &&
              !setting.catalog.sensitive &&
              !setting.catalog.deployment_owned,
          )
          .map((setting) => [setting.catalog.key, setting.value]),
      ),
    },
    null,
    2,
  );
}

export function parseSettingsImport(
  text: string,
  settings: ReadonlyArray<SettingView>,
): Record<string, SettingDraft> {
  const document = importDocument.parse(JSON.parse(text));
  const imported = new Map(Object.entries(document.settings));
  const knownKeys = new Set(settings.map((setting) => setting.catalog.key));
  if ([...imported.keys()].some((key) => !knownKeys.has(key))) {
    throw new Error("Imported settings contain an unknown key");
  }
  const drafts = Object.fromEntries(
    settings.map((setting) => {
      const value = imported.get(setting.catalog.key);
      return [
        setting.catalog.key,
        value === undefined
          ? { reset: false, text: editableText(setting) }
          : {
              reset: false,
              text:
                typeof value === "string"
                  ? value
                  : typeof value === "boolean" || typeof value === "number"
                    ? String(value)
                    : JSON.stringify(value, null, 2),
            },
      ];
    }),
  );
  if (
    settings.some((setting) => {
      const draft = settingDraft(drafts, setting.catalog.key);
      return imported.has(setting.catalog.key) && draftError(setting, draft) !== null;
    })
  ) {
    throw new Error("Imported settings contain a value with the wrong type");
  }
  return drafts;
}
