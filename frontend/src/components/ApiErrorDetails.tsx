import { useTranslation } from "react-i18next";
import { ApiClientError } from "../api/client";
import { apiErrorReason } from "../api/errors";

export function ApiErrorDetails({ error, fallback }: { error: unknown; fallback: string }) {
  const { t } = useTranslation();
  const reason = apiErrorReason(error);

  return (
    <>
      <p>{fallback}</p>
      {reason && reason !== fallback ? <p>{reason}</p> : null}
      {error instanceof ApiClientError ? (
        <small className="api-error-meta">
          <code>{error.code}</code>
          {error.requestId ? <span>{t("errors.requestId", { id: error.requestId })}</span> : null}
        </small>
      ) : null}
    </>
  );
}
