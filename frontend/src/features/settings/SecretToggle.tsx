import { Eye, EyeOff } from "lucide-react";
import { useTranslation } from "react-i18next";

export function SecretToggle({ revealed, onToggle }: { revealed: boolean; onToggle: () => void }) {
  const { t } = useTranslation();
  const label = t(revealed ? "settings.hideSecret" : "settings.showSecret");

  return (
    <button
      aria-label={label}
      aria-pressed={revealed}
      className="secret-toggle"
      onClick={onToggle}
      title={label}
      type="button"
    >
      {revealed ? <EyeOff aria-hidden="true" size={16} /> : <Eye aria-hidden="true" size={16} />}
    </button>
  );
}
