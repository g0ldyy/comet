import { useQuery } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { useTranslation } from "react-i18next";
import { ApiClientError } from "../../api/client";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { getAdminSession, loginAdmin } from "./api";
import { PasswordScreen } from "./PasswordScreen";

const adminSessionKey = ["auth", "admin-session"] as const;

export function AdminBoundary({ children }: PropsWithChildren) {
  const { t } = useTranslation();
  const session = useQuery({
    queryFn: getAdminSession,
    queryKey: adminSessionKey,
  });

  if (session.isPending) {
    return (
      <main className="centered-state">
        <Skeleton label={t("app.loading")} lines={4} />
      </main>
    );
  }

  if (session.error instanceof ApiClientError && session.error.status === 401) {
    return (
      <PasswordScreen
        eyebrow={t("auth.adminEyebrow")}
        onSubmit={async (password) => {
          const authenticated = await loginAdmin(password);
          queryClient.setQueryData(adminSessionKey, authenticated);
        }}
        title={t("auth.adminTitle")}
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

export async function clearAdminSession() {
  await queryClient.cancelQueries({ queryKey: ["admin"] });
  queryClient.removeQueries({ queryKey: ["admin"] });
  await queryClient.resetQueries({ exact: true, queryKey: adminSessionKey });
}
