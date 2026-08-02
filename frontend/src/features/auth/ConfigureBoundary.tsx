import { useQuery } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { useTranslation } from "react-i18next";
import { ApiClientError } from "../../api/client";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { getConfigureSession, loginConfigure } from "./api";
import { PasswordScreen } from "./PasswordScreen";

const configureSessionKey = ["auth", "configure-session"] as const;

export function ConfigureBoundary({ children }: PropsWithChildren) {
  const { t } = useTranslation();
  const session = useQuery({
    queryFn: getConfigureSession,
    queryKey: configureSessionKey,
  });

  if (session.isPending) {
    return (
      <main className="centered-state">
        <Skeleton label={t("app.loading")} lines={3} />
      </main>
    );
  }

  if (session.error instanceof ApiClientError && session.error.status === 401) {
    return (
      <PasswordScreen
        eyebrow={t("auth.configureEyebrow")}
        onSubmit={async (password) => {
          const authenticated = await loginConfigure(password);
          queryClient.setQueryData(configureSessionKey, authenticated);
        }}
        title={t("auth.configureTitle")}
      />
    );
  }

  if (session.isError) {
    return (
      <main className="centered-state">
        <Alert title={t("auth.sessionError")} tone="danger">
          <ApiErrorDetails error={session.error} fallback={t("errors.generic")} />
        </Alert>
        <Button onClick={() => void session.refetch()}>{t("actions.retry")}</Button>
      </main>
    );
  }

  return children;
}
