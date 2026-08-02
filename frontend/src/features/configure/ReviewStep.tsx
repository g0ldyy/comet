import * as Dialog from "@radix-ui/react-dialog";
import { Clipboard, ExternalLink, MonitorPlay, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import type { ConfigureFormValues } from "./model";

export function ReviewStep({
  busy,
  onCopy,
  onInstall,
  onKodi,
  onTest,
  values,
}: {
  busy: boolean;
  onCopy: () => Promise<boolean>;
  onInstall: () => Promise<boolean>;
  onKodi: (code: string) => Promise<boolean>;
  onTest: () => Promise<boolean>;
  values: ConfigureFormValues;
}) {
  const { t } = useTranslation();
  const [kodiOpen, setKodiOpen] = useState(false);
  const [kodiCode, setKodiCode] = useState(
    () => new URLSearchParams(window.location.search).get("kodi_code") ?? "",
  );
  const [kodiError, setKodiError] = useState<string | null>(null);
  return (
    <section className="configuration-actions">
      <div className="review-actions">
        {values.usenetEnabled ? (
          <Button disabled={busy} onClick={() => void onTest()} variant="secondary">
            {t("configure.actions.testUsenet")}
          </Button>
        ) : null}
        <Button disabled={busy} onClick={() => void onCopy()} variant="secondary">
          <Clipboard aria-hidden="true" size={17} />
          {t("configure.actions.copyLink")}
        </Button>
        <Button disabled={busy} onClick={() => void onInstall()}>
          <ExternalLink aria-hidden="true" size={17} />
          {t("configure.actions.install")}
        </Button>
        <Dialog.Root onOpenChange={setKodiOpen} open={kodiOpen}>
          <Dialog.Trigger asChild>
            <Button disabled={busy} variant="secondary">
              <MonitorPlay aria-hidden="true" size={17} />
              {t("configure.actions.pairKodi")}
            </Button>
          </Dialog.Trigger>
          <Dialog.Portal>
            <Dialog.Overlay className="dialog__overlay" />
            <Dialog.Content className="dialog__content">
              <div className="dialog__header">
                <Dialog.Title>{t("configure.actions.pairKodi")}</Dialog.Title>
                <Dialog.Close asChild>
                  <Button aria-label={t("actions.close")} variant="ghost">
                    <X aria-hidden="true" size={18} />
                  </Button>
                </Dialog.Close>
              </div>
              <Dialog.Description>{t("configure.actions.kodiDescription")}</Dialog.Description>
              {kodiError ? <Alert tone="danger">{kodiError}</Alert> : null}
              <Input
                autoCapitalize="none"
                label={t("configure.actions.kodiCode")}
                maxLength={8}
                onChange={(event) => {
                  setKodiCode(event.target.value.toLowerCase());
                  setKodiError(null);
                }}
                placeholder="1a2b3c4d"
                value={kodiCode}
              />
              <div className="dialog__actions">
                <Button
                  disabled={busy}
                  onClick={async () => {
                    if (!/^[0-9a-f]{8}$/.test(kodiCode)) {
                      setKodiError(t("configure.actions.kodiInvalid"));
                      return;
                    }
                    if (await onKodi(kodiCode)) setKodiOpen(false);
                  }}
                >
                  {t("configure.actions.pair")}
                </Button>
              </div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      </div>
    </section>
  );
}
