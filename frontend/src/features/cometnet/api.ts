import { apiRequest } from "../../api/client";
import type {
  CometNetInviteData,
  CometNetMutationData,
  CometNetPoolDetailData,
  CometNetSnapshotData,
} from "../../api/generated/contracts";

export interface PoolCreateInput {
  pool_id: string;
  display_name: string;
  description: string;
  join_mode: "invite";
}

export interface PoolJoinInput {
  poolId: string;
  invite_code: string;
  node_url: string | null;
}

export function getCometNetSnapshot(): Promise<CometNetSnapshotData> {
  return apiRequest<CometNetSnapshotData>("/api/v1/admin/cometnet/snapshot", {
    scope: "admin",
  });
}

export function getCometNetPool(poolId: string): Promise<CometNetPoolDetailData> {
  return apiRequest<CometNetPoolDetailData>(`/api/v1/admin/cometnet/pools/${poolId}`, {
    scope: "admin",
  });
}

export function createCometNetPool(input: PoolCreateInput): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>("/api/v1/admin/cometnet/pools", {
    body: JSON.stringify(input),
    method: "POST",
    scope: "admin",
  });
}

export function deleteCometNetPool(poolId: string): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(`/api/v1/admin/cometnet/pools/${poolId}`, {
    method: "DELETE",
    scope: "admin",
  });
}

export function joinCometNetPool(input: PoolJoinInput): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(`/api/v1/admin/cometnet/pools/${input.poolId}/join`, {
    body: JSON.stringify({ invite_code: input.invite_code, node_url: input.node_url }),
    method: "POST",
    scope: "admin",
  });
}

export function createCometNetInvite(poolId: string): Promise<CometNetInviteData> {
  return apiRequest<CometNetInviteData>(`/api/v1/admin/cometnet/pools/${poolId}/invites`, {
    body: JSON.stringify({ expires_in: null, max_uses: null }),
    method: "POST",
    scope: "admin",
  });
}

export function revokeCometNetInvite(input: {
  poolId: string;
  inviteCode: string;
}): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(
    `/api/v1/admin/cometnet/pools/${input.poolId}/invites/${input.inviteCode}`,
    { method: "DELETE", scope: "admin" },
  );
}

export function setCometNetSubscription(input: {
  poolId: string;
  subscribed: boolean;
}): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(
    `/api/v1/admin/cometnet/pools/${input.poolId}/subscription`,
    { method: input.subscribed ? "POST" : "DELETE", scope: "admin" },
  );
}

export function addCometNetMember(input: {
  poolId: string;
  memberKey: string;
  role: "admin" | "member";
}): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(`/api/v1/admin/cometnet/pools/${input.poolId}/members`, {
    body: JSON.stringify({ member_key: input.memberKey, role: input.role }),
    method: "POST",
    scope: "admin",
  });
}

export function removeCometNetMember(input: {
  poolId: string;
  memberKey: string;
}): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(
    `/api/v1/admin/cometnet/pools/${input.poolId}/members/${input.memberKey}`,
    { method: "DELETE", scope: "admin" },
  );
}

export function updateCometNetMember(input: {
  poolId: string;
  memberKey: string;
  role: "admin" | "member";
}): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(
    `/api/v1/admin/cometnet/pools/${input.poolId}/members/${input.memberKey}`,
    {
      body: JSON.stringify({ role: input.role }),
      method: "PATCH",
      scope: "admin",
    },
  );
}

export function leaveCometNetPool(poolId: string): Promise<CometNetMutationData> {
  return apiRequest<CometNetMutationData>(`/api/v1/admin/cometnet/pools/${poolId}/leave`, {
    method: "POST",
    scope: "admin",
  });
}
