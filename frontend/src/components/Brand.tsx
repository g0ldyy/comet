import { useTranslation } from "react-i18next";

interface BrandProps {
  compact?: boolean;
}

export function Brand({ compact = false }: BrandProps) {
  const { t } = useTranslation();
  return (
    <span className="brand">
      <img alt="" className="brand__mark" height="32" src="/brand/comet-mark.svg" width="32" />
      {compact ? null : (
        <span>
          <strong className="brand__name">{t("app.name")}</strong>
        </span>
      )}
    </span>
  );
}
