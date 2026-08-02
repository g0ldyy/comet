import { Link } from "@tanstack/react-router";
import { Settings } from "lucide-react";
import { useTranslation } from "react-i18next";

export function SettingsShortcut() {
  const { t } = useTranslation();
  return (
    <Link
      aria-label={t("nav.settings")}
      className="settings-shortcut"
      title={t("nav.settings")}
      to="/admin/settings"
    >
      <Settings aria-hidden="true" size={19} strokeWidth={1.7} />
    </Link>
  );
}
