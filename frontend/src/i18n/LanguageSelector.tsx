import { Languages } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Select } from "../components/ui/Select";
import { locales } from "./index";

export function LanguageSelector() {
  const { i18n, t } = useTranslation();
  const languageNames = new Intl.DisplayNames([i18n.resolvedLanguage ?? "en"], {
    type: "language",
  });

  return (
    <div className="language-select">
      <Select
        className="language-select__field"
        label={t("language.label")}
        labelHidden
        leadingIcon={<Languages size={16} />}
        onValueChange={(language) => void i18n.changeLanguage(language)}
        value={i18n.resolvedLanguage ?? "en"}
      >
        {locales.map(({ code }) => (
          <option key={code} value={code}>
            {languageNames.of(code) ?? code}
          </option>
        ))}
      </Select>
    </div>
  );
}
