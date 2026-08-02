import { describe, expect, it } from "vitest";
import english from "./locales/en.json";

const translations = import.meta.glob("./locales/*.json", {
  eager: true,
  import: "default",
}) as Record<string, Record<string, unknown>>;

function keys(value: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof child === "object" && child !== null
      ? keys(child as Record<string, unknown>, path)
      : [path];
  });
}

function messages(value: Record<string, unknown>, prefix = ""): Map<string, string> {
  return new Map(
    Object.entries(value).flatMap(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      return typeof child === "object" && child !== null
        ? [...messages(child as Record<string, unknown>, path)]
        : [[path, String(child)] as const];
    }),
  );
}

function variables(message: string): string[] {
  return [...message.matchAll(/\{\{\s*([^},\s]+)[^}]*}}/g)]
    .map((match) => match[1] as string)
    .sort();
}

describe("translations", () => {
  it("keeps every locale in parity with English", () => {
    const englishKeys = keys(english).sort();

    for (const [path, messages] of Object.entries(translations)) {
      expect(keys(messages).sort(), path).toEqual(englishKeys);
    }
  });

  it("keeps interpolation variables intact in every locale", () => {
    const englishMessages = messages(english);

    for (const [path, translation] of Object.entries(translations)) {
      const localizedMessages = messages(translation);
      for (const [key, englishMessage] of englishMessages) {
        expect(variables(localizedMessages.get(key) ?? ""), `${path}: ${key}`).toEqual(
          variables(englishMessage),
        );
      }
    }
  });
});
