import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useTranslation } from "react-i18next";

type ToastTone = "danger" | "info" | "success" | "warning";

const icons = {
  danger: CircleAlert,
  info: Info,
  success: CircleCheck,
  warning: CircleAlert,
} as const;

export function Toast({
  children,
  closing,
  onClose,
  onExited,
  tone,
}: {
  children: string;
  closing: boolean;
  onClose: () => void;
  onExited: () => void;
  tone: ToastTone;
}) {
  const { t } = useTranslation();
  const Icon = icons[tone];
  return (
    <div
      aria-live={tone === "danger" ? "assertive" : "polite"}
      className={`toast toast--${tone}${closing ? " toast--closing" : ""}`}
      onAnimationEnd={(event) => {
        if (closing && event.animationName === "toast-out") onExited();
      }}
      role={tone === "danger" ? "alert" : "status"}
    >
      <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
      <span>{children}</span>
      <button aria-label={t("actions.close")} onClick={onClose} type="button">
        <X aria-hidden="true" size={16} strokeWidth={1.8} />
      </button>
    </div>
  );
}
