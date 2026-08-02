import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import resourcesToBackend from "i18next-resources-to-backend";
import { initReactI18next } from "react-i18next";

export const locales = [
  { code: "en" },
  { code: "fr" },
  { code: "es" },
  { code: "de" },
  { code: "it" },
  { code: "pt-BR" },
  { code: "nl" },
  { code: "pl" },
  { code: "tr" },
  { code: "ru" },
  { code: "uk" },
  { code: "ar" },
  { code: "zh-CN" },
  { code: "zh-TW" },
  { code: "ja" },
  { code: "ko" },
] as const;

type LocaleLoader = () => Promise<{ default: Record<string, unknown> }>;

const englishLoader: LocaleLoader = () => import("./locales/en.json");
const loaders: Record<string, LocaleLoader> = {
  ar: () => import("./locales/ar.json"),
  de: () => import("./locales/de.json"),
  en: englishLoader,
  es: () => import("./locales/es.json"),
  fr: () => import("./locales/fr.json"),
  it: () => import("./locales/it.json"),
  ja: () => import("./locales/ja.json"),
  ko: () => import("./locales/ko.json"),
  nl: () => import("./locales/nl.json"),
  pl: () => import("./locales/pl.json"),
  "pt-BR": () => import("./locales/pt-BR.json"),
  ru: () => import("./locales/ru.json"),
  tr: () => import("./locales/tr.json"),
  uk: () => import("./locales/uk.json"),
  "zh-CN": () => import("./locales/zh-CN.json"),
  "zh-TW": () => import("./locales/zh-TW.json"),
};

export async function initializeI18n() {
  await i18n
    .use(
      resourcesToBackend((language: string) => {
        const load = loaders[language] ?? englishLoader;
        return load();
      }),
    )
    .use(LanguageDetector)
    .use(initReactI18next)
    .init({
      detection: {
        caches: ["localStorage"],
        lookupLocalStorage: "comet-language",
        order: ["localStorage", "navigator"],
      },
      fallbackLng: "en",
      interpolation: {
        escapeValue: false,
      },
      load: "currentOnly",
      supportedLngs: locales.map(({ code }) => code),
    });

  const updateDirection = (language: string) => {
    document.documentElement.dir = language === "ar" ? "rtl" : "ltr";
    document.documentElement.lang = language;
  };
  updateDirection(i18n.resolvedLanguage ?? "en");
  i18n.on("languageChanged", updateDirection);
  return i18n;
}

export default i18n;
