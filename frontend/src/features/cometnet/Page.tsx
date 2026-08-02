import { useMutation, useQuery } from "@tanstack/react-query";
import { Activity, RadioTower, ShieldCheck, Users } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { queryClient } from "../../api/query-client";
import { ApiErrorDetails } from "../../components/ApiErrorDetails";
import { Alert } from "../../components/ui/Alert";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { MetricCard } from "../metrics/MetricCard";
import { formatMetric } from "../metrics/model";
import {
  addCometNetMember,
  createCometNetInvite,
  createCometNetPool,
  deleteCometNetPool,
  getCometNetPool,
  getCometNetSnapshot,
  joinCometNetPool,
  leaveCometNetPool,
  removeCometNetMember,
  revokeCometNetInvite,
  setCometNetSubscription,
  updateCometNetMember,
} from "./api";

export function CometNetPage() {
  const { t } = useTranslation();
  const [selectedPool, setSelectedPool] = useState<string | null>(null);
  const [poolId, setPoolId] = useState("");
  const [poolName, setPoolName] = useState("");
  const [poolDescription, setPoolDescription] = useState("");
  const [joinPoolId, setJoinPoolId] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [nodeUrl, setNodeUrl] = useState("");
  const [memberKey, setMemberKey] = useState("");
  const [memberRole, setMemberRole] = useState<"admin" | "member">("member");
  const [createdInvite, setCreatedInvite] = useState<string | null>(null);
  const snapshot = useQuery({
    queryFn: getCometNetSnapshot,
    queryKey: ["admin", "cometnet", "snapshot"],
    refetchInterval: 3_000,
  });
  const pool = useQuery({
    enabled: selectedPool !== null,
    queryFn: () => getCometNetPool(selectedPool as string),
    queryKey: ["admin", "cometnet", "pool", selectedPool],
    refetchInterval: 5_000,
  });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["admin", "cometnet", "snapshot"] }),
      queryClient.invalidateQueries({ queryKey: ["admin", "cometnet", "pool"] }),
    ]);
  };
  const action = useMutation({
    mutationFn: async (run: () => Promise<unknown>) => run(),
    onSuccess: refresh,
  });

  const submitCreate = (event: FormEvent) => {
    event.preventDefault();
    action.mutate(() =>
      createCometNetPool({
        description: poolDescription,
        display_name: poolName,
        join_mode: "invite",
        pool_id: poolId,
      }),
    );
  };
  const submitJoin = (event: FormEvent) => {
    event.preventDefault();
    action.mutate(() =>
      joinCometNetPool({
        invite_code: inviteCode,
        node_url: nodeUrl || null,
        poolId: joinPoolId,
      }),
    );
  };
  const data = snapshot.data;
  const detail = pool.data;

  return (
    <section
      aria-labelledby="cometnet-title"
      className="section-page dashboard-page operations-page"
    >
      <header className="section-page__header">
        <div>
          <h1 id="cometnet-title">{t("nav.cometnet")}</h1>
        </div>
      </header>

      {snapshot.isError || pool.isError ? (
        <Alert title={t("cometnet.errorTitle")} tone="danger">
          <ApiErrorDetails
            error={snapshot.error ?? pool.error}
            fallback={t("cometnet.errorDescription")}
          />
        </Alert>
      ) : null}
      {action.isError ? (
        <Alert title={t("cometnet.actionError")} tone="danger">
          <ApiErrorDetails error={action.error} fallback={t("cometnet.actionErrorDescription")} />
        </Alert>
      ) : null}
      {snapshot.isPending ? (
        <Skeleton label={t("cometnet.loading")} lines={10} />
      ) : data ? (
        <>
          {!data.node.enabled ? (
            <Alert title={t("cometnet.disabledTitle")} tone="warning">
              {t("cometnet.disabledDescription")}
            </Alert>
          ) : null}
          <div className="metric-grid metric-grid--hero">
            <MetricCard
              detail={`${data.node.inbound_peers} ${t("cometnet.inbound")} · ${data.node.outbound_peers} ${t("cometnet.outbound")}`}
              label={t("cometnet.peers")}
              value={formatMetric(data.node.connected_peers, "number")}
            />
            <MetricCard
              detail={t("cometnet.sentReceived", {
                received: formatMetric(data.node.bytes_received, "bytes"),
                sent: formatMetric(data.node.bytes_sent, "bytes"),
              })}
              label={t("cometnet.traffic")}
              value={formatMetric(data.node.bytes_received + data.node.bytes_sent, "bytes")}
            />
            <MetricCard
              detail={t("cometnet.receivedSent", {
                received: data.node.torrents_received,
                sent: data.node.torrents_sent,
              })}
              label={t("cometnet.contributions")}
              value={formatMetric(data.node.torrents_received + data.node.torrents_sent, "number")}
            />
            <MetricCard
              detail={`${data.node.mode} · ${data.node.contribution_mode ?? "—"}`}
              label={t("cometnet.latency")}
              value={formatMetric(data.node.average_latency_ms / 1_000, "seconds")}
            />
          </div>

          <div className="dashboard-columns">
            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">{t("cometnet.nodeEyebrow")}</span>
                  <h2>{t("cometnet.node")}</h2>
                </div>
                <RadioTower aria-hidden="true" size={20} />
              </header>
              <dl className="health-list">
                <div>
                  <dt>{t("cometnet.identity")}</dt>
                  <dd className="code-value">{data.node.node_id?.slice(0, 16) ?? "—"}</dd>
                </div>
                <div>
                  <dt>{t("cometnet.state")}</dt>
                  <dd className={data.node.healthy ? "health-state" : "health-state--unavailable"}>
                    {t(data.node.healthy ? "cometnet.healthy" : "cometnet.unavailable")}
                  </dd>
                </div>
                <div>
                  <dt>{t("cometnet.messages")}</dt>
                  <dd>{data.node.messages_sent + data.node.messages_received}</dd>
                </div>
                <div>
                  <dt>{t("cometnet.invalid")}</dt>
                  <dd>{data.node.invalid_messages}</dd>
                </div>
              </dl>
            </article>

            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">{t("cometnet.manageEyebrow")}</span>
                  <h2>{t("cometnet.createJoin")}</h2>
                </div>
              </header>
              <div className="cometnet-form-stack">
                <form className="operation-form" onSubmit={submitCreate}>
                  <Input
                    label={t("cometnet.poolId")}
                    onChange={(event) => setPoolId(event.target.value)}
                    pattern="[a-z0-9][a-z0-9_-]{1,63}"
                    required
                    value={poolId}
                  />
                  <Input
                    label={t("cometnet.poolName")}
                    onChange={(event) => setPoolName(event.target.value)}
                    required
                    value={poolName}
                  />
                  <Input
                    label={t("cometnet.descriptionLabel")}
                    onChange={(event) => setPoolDescription(event.target.value)}
                    value={poolDescription}
                  />
                  <Button disabled={action.isPending} type="submit">
                    {t("cometnet.create")}
                  </Button>
                </form>
                <form className="operation-form" onSubmit={submitJoin}>
                  <Input
                    label={t("cometnet.poolId")}
                    onChange={(event) => setJoinPoolId(event.target.value)}
                    required
                    value={joinPoolId}
                  />
                  <Input
                    label={t("cometnet.inviteCode")}
                    onChange={(event) => setInviteCode(event.target.value)}
                    required
                    value={inviteCode}
                  />
                  <Input
                    label={t("cometnet.nodeUrl")}
                    onChange={(event) => setNodeUrl(event.target.value)}
                    value={nodeUrl}
                  />
                  <Button disabled={action.isPending} type="submit" variant="secondary">
                    {t("cometnet.join")}
                  </Button>
                </form>
              </div>
            </article>
          </div>

          <article className="dashboard-panel">
            <header>
              <div>
                <span className="eyebrow">{t("cometnet.peersEyebrow")}</span>
                <h2>{t("cometnet.connectedPeers")}</h2>
              </div>
              <span>{data.peers.length}</span>
            </header>
            <div className="operations-table-wrap">
              <table className="operations-table">
                <thead>
                  <tr>
                    <th>{t("cometnet.peer")}</th>
                    <th>{t("cometnet.direction")}</th>
                    <th>{t("cometnet.latency")}</th>
                    <th>{t("cometnet.reputation")}</th>
                    <th>{t("cometnet.traffic")}</th>
                    <th>{t("cometnet.contributions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.peers.map((peer) => (
                    <tr key={peer.node_id}>
                      <td data-label={t("cometnet.peer")}>
                        <strong>{peer.alias ?? peer.node_id.slice(0, 12)}</strong>
                        <small>{peer.node_id}</small>
                      </td>
                      <td data-label={t("cometnet.direction")}>
                        {t(peer.outbound ? "cometnet.outbound" : "cometnet.inbound")}
                      </td>
                      <td data-label={t("cometnet.latency")}>{Math.round(peer.latency_ms)} ms</td>
                      <td data-label={t("cometnet.reputation")}>
                        {peer.reputation ?? "—"} {peer.trust_level ?? ""}
                      </td>
                      <td data-label={t("cometnet.traffic")}>
                        {formatMetric(peer.bytes_sent + peer.bytes_received, "bytes")}
                      </td>
                      <td data-label={t("cometnet.contributions")}>{peer.torrents_received}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.peers.length === 0 ? (
                <p className="empty-state">{t("cometnet.noPeers")}</p>
              ) : null}
            </div>
          </article>

          <div className="cometnet-pool-layout">
            <article className="dashboard-panel">
              <header>
                <div>
                  <span className="eyebrow">{t("cometnet.poolsEyebrow")}</span>
                  <h2>{t("cometnet.pools")}</h2>
                </div>
                <span>{data.pools.length}</span>
              </header>
              <div className="pool-list">
                {data.pools.map((item) => (
                  <button
                    className={
                      selectedPool === item.pool_id ? "pool-card pool-card--active" : "pool-card"
                    }
                    key={item.pool_id}
                    onClick={() => {
                      setCreatedInvite(null);
                      setSelectedPool(item.pool_id);
                    }}
                    type="button"
                  >
                    <strong>{item.display_name}</strong>
                    <span>{item.description || item.pool_id}</span>
                    <small>
                      {item.member_count} {t("cometnet.members")} · v{item.version}
                    </small>
                  </button>
                ))}
                {data.pools.length === 0 ? (
                  <p className="empty-state">{t("cometnet.noPools")}</p>
                ) : null}
              </div>
            </article>

            <article className="dashboard-panel cometnet-pool-detail">
              <header>
                <div>
                  <span className="eyebrow">{t("cometnet.poolDetailEyebrow")}</span>
                  <h2>
                    {detail?.display_name ??
                      t(data.node.enabled ? "cometnet.selectPool" : "cometnet.disabledTitle")}
                  </h2>
                </div>
                <Users aria-hidden="true" size={20} />
              </header>
              {selectedPool !== null && pool.isPending ? (
                <Skeleton label={t("cometnet.loadingPool")} lines={6} />
              ) : null}
              {selectedPool === null ? (
                <p className="empty-state">
                  {t(
                    data.node.enabled ? "cometnet.selectPoolHint" : "cometnet.disabledDescription",
                  )}
                </p>
              ) : null}
              {detail ? (
                <>
                  <p>{detail.description || detail.pool_id}</p>
                  <div className="row-actions">
                    <Button
                      disabled={action.isPending}
                      onClick={() =>
                        action.mutate(() =>
                          setCometNetSubscription({
                            poolId: detail.pool_id,
                            subscribed: !detail.subscribed,
                          }),
                        )
                      }
                      variant="secondary"
                    >
                      {t(detail.subscribed ? "cometnet.unsubscribe" : "cometnet.subscribe")}
                    </Button>
                    {detail.is_admin ? (
                      <>
                        <Button
                          disabled={action.isPending}
                          onClick={() =>
                            action.mutate(
                              async () => {
                                const invite = await createCometNetInvite(detail.pool_id);
                                setCreatedInvite(invite.invite_link);
                              },
                              { onSuccess: refresh },
                            )
                          }
                          variant="secondary"
                        >
                          {t("cometnet.createInvite")}
                        </Button>
                        <Button
                          disabled={action.isPending}
                          onClick={() => action.mutate(() => deleteCometNetPool(detail.pool_id))}
                          variant="danger"
                        >
                          {t("cometnet.deletePool")}
                        </Button>
                      </>
                    ) : detail.is_member ? (
                      <Button
                        disabled={action.isPending}
                        onClick={() => action.mutate(() => leaveCometNetPool(detail.pool_id))}
                        variant="danger"
                      >
                        {t("cometnet.leave")}
                      </Button>
                    ) : null}
                  </div>
                  {createdInvite ? (
                    <div className="invite-output">
                      <strong>{t("cometnet.inviteReady")}</strong>
                      <code>{createdInvite}</code>
                    </div>
                  ) : null}
                  {detail.is_admin ? (
                    <form
                      className="member-form"
                      onSubmit={(event) => {
                        event.preventDefault();
                        action.mutate(() =>
                          addCometNetMember({
                            memberKey,
                            poolId: detail.pool_id,
                            role: memberRole,
                          }),
                        );
                      }}
                    >
                      <Input
                        label={t("cometnet.memberKey")}
                        onChange={(event) => setMemberKey(event.target.value)}
                        required
                        value={memberKey}
                      />
                      <Select
                        label={t("cometnet.role")}
                        onValueChange={(value) => setMemberRole(value as "admin" | "member")}
                        value={memberRole}
                      >
                        <option value="member">{t("cometnet.member")}</option>
                        <option value="admin">{t("cometnet.admin")}</option>
                      </Select>
                      <Button disabled={action.isPending} type="submit">
                        {t("cometnet.addMember")}
                      </Button>
                    </form>
                  ) : null}
                  <div className="member-list">
                    {detail.members.map((member) => (
                      <div key={member.public_key}>
                        <div>
                          <strong>
                            {member.node_id.slice(0, 12)}
                            {member.is_self ? ` · ${t("cometnet.you")}` : ""}
                          </strong>
                          <small>
                            {member.role} · {member.contribution_count}{" "}
                            {t("cometnet.contributions")}
                          </small>
                        </div>
                        {detail.is_admin && member.role !== "creator" && !member.is_self ? (
                          <div className="row-actions">
                            <Button
                              disabled={action.isPending}
                              onClick={() =>
                                action.mutate(() =>
                                  updateCometNetMember({
                                    memberKey: member.public_key,
                                    poolId: detail.pool_id,
                                    role: member.role === "admin" ? "member" : "admin",
                                  }),
                                )
                              }
                              variant="ghost"
                            >
                              {t(member.role === "admin" ? "cometnet.demote" : "cometnet.promote")}
                            </Button>
                            <Button
                              disabled={action.isPending}
                              onClick={() =>
                                action.mutate(() =>
                                  removeCometNetMember({
                                    memberKey: member.public_key,
                                    poolId: detail.pool_id,
                                  }),
                                )
                              }
                              variant="danger"
                            >
                              {t("cometnet.remove")}
                            </Button>
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                  {detail.invites.length > 0 ? (
                    <div className="invite-list">
                      <h3>{t("cometnet.invites")}</h3>
                      {detail.invites.map((invite) => (
                        <div key={invite.invite_code}>
                          <code>{invite.invite_code}</code>
                          <span>
                            {invite.uses}/{invite.max_uses ?? "∞"}
                          </span>
                          <Button
                            disabled={action.isPending}
                            onClick={() =>
                              action.mutate(() =>
                                revokeCometNetInvite({
                                  inviteCode: invite.invite_code,
                                  poolId: detail.pool_id,
                                }),
                              )
                            }
                            variant="danger"
                          >
                            {t("cometnet.revoke")}
                          </Button>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </>
              ) : selectedPool === null ? (
                <p className="empty-state">{t("cometnet.selectPoolHint")}</p>
              ) : null}
            </article>
          </div>

          <article className="dashboard-panel">
            <header>
              <div>
                <h2>{t("cometnet.timeline")}</h2>
              </div>
              <Activity aria-hidden="true" size={20} />
            </header>
            <div className="cometnet-timeline">
              {data.events.map((event) => (
                <div key={event.id}>
                  <ShieldCheck aria-hidden="true" size={16} />
                  <div>
                    <strong>{event.message}</strong>
                    <small>
                      {event.event} · {new Date(event.created_at * 1_000).toLocaleString()}
                    </small>
                  </div>
                </div>
              ))}
              {data.events.length === 0 ? (
                <p className="empty-state">{t("cometnet.noEvents")}</p>
              ) : null}
            </div>
          </article>
        </>
      ) : null}
    </section>
  );
}
