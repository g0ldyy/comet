import * as Dialog from "@radix-ui/react-dialog";
import { Copy, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { OperationalEventData } from "../../api/generated/contracts";
import { Button } from "../../components/ui/Button";

export function EventDetail({
  event,
  onClose,
  related,
}: {
  event: OperationalEventData | null;
  onClose: () => void;
  related: OperationalEventData[];
}) {
  const { t } = useTranslation();
  return (
    <Dialog.Root onOpenChange={(open) => !open && onClose()} open={event !== null}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog__overlay" />
        <Dialog.Content className="event-detail">
          {event ? (
            <>
              <div className="dialog__header">
                <div>
                  <Dialog.Title>{event.event}</Dialog.Title>
                  <Dialog.Description>{event.message}</Dialog.Description>
                </div>
                <Dialog.Close asChild>
                  <Button aria-label={t("actions.close")} variant="ghost">
                    <X aria-hidden="true" size={18} />
                  </Button>
                </Dialog.Close>
              </div>
              <dl className="event-detail__metadata">
                <Metadata
                  label={t("events.detail.timestamp")}
                  value={new Date(event.created_at * 1_000).toLocaleString()}
                />
                <Metadata label={t("events.filters.level")} value={event.level} />
                <Metadata label={t("events.filters.category")} value={event.category} />
                <Metadata label={t("events.filters.replica")} value={event.instance_id} />
                <Metadata
                  label={t("events.detail.process")}
                  value={`${event.role} · ${event.process_id}`}
                />
                <Metadata label={t("events.filters.request")} value={event.request_id} />
                <Metadata label={t("events.filters.run")} value={event.run_id} />
                <Metadata label={t("events.filters.connection")} value={event.connection_id} />
                <Metadata label={t("events.filters.outcome")} value={event.outcome} />
                <Metadata label={t("events.detail.errorCode")} value={event.error_code} />
              </dl>
              <div className="event-detail__json">
                <div>
                  <strong>{t("events.detail.safeDetails")}</strong>
                  <Button
                    aria-label={t("events.detail.copy")}
                    onClick={() =>
                      void navigator.clipboard.writeText(JSON.stringify(event, null, 2))
                    }
                    variant="ghost"
                  >
                    <Copy aria-hidden="true" size={16} />
                  </Button>
                </div>
                <pre>{JSON.stringify(event.details, null, 2)}</pre>
              </div>
              <div className="event-detail__related">
                <strong>{t("events.detail.related")}</strong>
                {related.slice(0, 20).map((item) => (
                  <div key={item.id}>
                    <time>{new Date(item.created_at * 1000).toLocaleTimeString()}</time>
                    <span>{item.event}</span>
                    <span>{item.outcome ?? item.level}</span>
                  </div>
                ))}
                {related.length === 0 ? <p>{t("events.detail.noRelated")}</p> : null}
              </div>
            </>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Metadata({ label, value }: { label: string; value: string | null }) {
  if (value === null) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
