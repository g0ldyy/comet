import { describe, expect, it } from "vitest";
import type { SettingView } from "../../api/generated/contracts";
import {
  draftError,
  exportSettings,
  initialDrafts,
  mutationDocument,
  parseSettingsImport,
  settingDraft,
} from "./model";

function setting(
  key: string,
  valueKind: string,
  value: unknown,
  options: Partial<SettingView["catalog"]> = {},
): SettingView {
  return {
    catalog: {
      category: "advanced_tuning",
      key,
      nullable: false,
      value_kind: valueKind,
      ...options,
    },
    active_value: value,
    source: "default",
    value,
  };
}

describe("settings model", () => {
  const settings = [
    setting("LIMIT", "integer", 10),
    setting("NAMES", "list", ["one"], { item_kind: "string" }),
    setting("TOKEN", "string", null, { sensitive: true }),
    setting("PRIVATE_NODES", "list", ["wss://private.example"], {
      item_kind: "string",
      sensitive: true,
    }),
  ];

  it("builds one atomic mutation from changed typed drafts", () => {
    const drafts = initialDrafts(settings);
    expect(drafts.PRIVATE_NODES?.text).toBe('[\n  "wss://private.example"\n]');
    drafts.LIMIT = { reset: false, text: "12" };
    drafts.NAMES = { reset: false, text: '["one", "two"]' };
    expect(mutationDocument(settings, drafts)).toEqual({
      reset: [],
      updates: {
        LIMIT: 12,
        NAMES: ["one", "two"],
      },
    });
  });

  it("preserves mixed enum values and string-or-list values", () => {
    const typed = [
      setting("SCRAPE_COMET", "enum", false, {
        choices: [false, true, "live", "background"],
      }),
      setting("COMET_URL", "string_or_list", "https://one.example", {
        item_kind: "string",
      }),
    ];
    const drafts = initialDrafts(typed);
    drafts.SCRAPE_COMET = { reset: false, text: "true" };
    drafts.COMET_URL = {
      reset: false,
      text: '["https://one.example","https://two.example"]',
    };

    expect(mutationDocument(typed, drafts).updates).toEqual({
      SCRAPE_COMET: true,
      COMET_URL: ["https://one.example", "https://two.example"],
    });
  });

  it("validates exact numeric, boolean, enum and JSON input", () => {
    expect(
      draftError(setting("LIMIT", "integer", 1), {
        reset: false,
        text: "1x",
      }),
    ).toBe("number");
    expect(
      draftError(setting("FLAG", "boolean", true), {
        reset: false,
        text: "yes",
      }),
    ).toBe("boolean");
    expect(
      draftError(setting("MODE", "enum", "one", { choices: ["one", "two"] }), {
        reset: false,
        text: "three",
      }),
    ).toBe("choice");
    expect(
      draftError(setting("NAMES", "list", []), {
        reset: false,
        text: "{}",
      }),
    ).toBe("list");
  });

  it("imports only current catalog keys and never exports secrets", () => {
    const imported = parseSettingsImport('{"settings":{"LIMIT":20,"NAMES":["two"]}}', settings);
    expect(settingDraft(imported, "LIMIT").text).toBe("20");
    expect(() => parseSettingsImport('{"settings":{"REMOVED_KEY":true}}', settings)).toThrow();
    expect(() => parseSettingsImport('{"settings":{"NAMES":["two",2]}}', settings)).toThrow();
    expect(exportSettings(settings)).not.toContain("TOKEN");
  });
});
