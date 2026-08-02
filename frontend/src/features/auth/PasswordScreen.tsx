import { zodResolver } from "@hookform/resolvers/zod";
import { LockKeyhole } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { z } from "zod";
import { ApiClientError } from "../../api/client";
import { apiErrorSummary } from "../../api/errors";
import { Brand } from "../../components/Brand";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { LanguageSelector } from "../../i18n/LanguageSelector";

const credentialsSchema = z.object({
  password: z.string().min(1).max(512),
});

type Credentials = z.infer<typeof credentialsSchema>;

interface PasswordScreenProps {
  eyebrow: string;
  onSubmit: (password: string) => Promise<void>;
  title: string;
}

export function PasswordScreen({ eyebrow, onSubmit, title }: PasswordScreenProps) {
  const { t } = useTranslation();
  const [requestError, setRequestError] = useState<string | null>(null);
  const {
    formState: { errors, isSubmitting },
    handleSubmit,
    register,
    reset,
  } = useForm<Credentials>({
    defaultValues: { password: "" },
    resolver: zodResolver(credentialsSchema),
  });

  const submit = handleSubmit(async ({ password }) => {
    setRequestError(null);
    try {
      await onSubmit(password);
      reset();
    } catch (error) {
      if (error instanceof ApiClientError && error.status === 401) {
        setRequestError(
          error.requestId
            ? `${t("auth.wrongPassword")} ${t("errors.requestId", { id: error.requestId })}`
            : t("auth.wrongPassword"),
        );
      } else {
        setRequestError(
          apiErrorSummary(error, t("errors.generic"), (id) => t("errors.requestId", { id })),
        );
      }
    }
  });

  return (
    <main className="auth-layout">
      <div className="auth-layout__utility">
        <LanguageSelector />
      </div>
      <section aria-labelledby="auth-title" className="auth-card">
        <Brand />
        <p className="auth-card__tagline">
          Stremio&apos;s fastest torrent/debrid/usenet search add-on.
        </p>
        <div className="auth-card__heading">
          <span className="eyebrow">
            <LockKeyhole aria-hidden="true" size={15} />
            {eyebrow}
          </span>
          <h1 id="auth-title">{title}</h1>
        </div>
        {requestError ? <Alert tone="danger">{requestError}</Alert> : null}
        <form className="auth-form" onSubmit={submit}>
          <Input
            autoComplete="current-password"
            error={errors.password?.message}
            label={t("auth.password")}
            type="password"
            {...register("password")}
          />
          <Button className="auth-form__submit" disabled={isSubmitting} type="submit">
            {isSubmitting ? t("actions.signingIn") : t("actions.signIn")}
          </Button>
        </form>
      </section>
    </main>
  );
}
