import type { PropsWithChildren } from "react";

interface AlertProps extends PropsWithChildren {
  title?: string;
  tone?: "danger" | "info" | "success" | "warning";
}

export function Alert({ children, title, tone = "info" }: AlertProps) {
  return (
    <div className={`alert alert--${tone}`} role={tone === "danger" ? "alert" : "status"}>
      {title ? <strong className="alert__title">{title}</strong> : null}
      <div>{children}</div>
    </div>
  );
}
