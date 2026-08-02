"""Authenticated CometNet operations with stable, secret-free responses."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import ConfigDict

from comet.api.v1.contracts import (
    ApiSuccess,
    CometNetInviteData,
    CometNetInviteView,
    CometNetMutationData,
    CometNetNodeView,
    CometNetPeerView,
    CometNetPoolDetailData,
    CometNetPoolMemberView,
    CometNetPoolView,
    CometNetSnapshotData,
    OperationalEventData,
)
from comet.api.v1.responses import ApiProblem, success_response
from comet.api.v1.security import require_admin_session, require_csrf
from comet.cometnet import CometNetBackend, get_active_backend
from comet.cometnet.admin_contracts import (
    AddMemberRequest as SharedAddMemberRequest,
)
from comet.cometnet.admin_contracts import (
    CreateInviteRequest as SharedCreateInviteRequest,
)
from comet.cometnet.admin_contracts import (
    CreatePoolRequest as SharedCreatePoolRequest,
)
from comet.cometnet.admin_contracts import (
    InviteCodePath,
    MemberKeyPath,
    PoolIdPath,
)
from comet.cometnet.admin_contracts import (
    JoinPoolRequest as SharedJoinPoolRequest,
)
from comet.cometnet.admin_contracts import (
    UpdateMemberRoleRequest as SharedUpdateMemberRoleRequest,
)
from comet.core.event_store import EventFilters, EventStore
from comet.core.models import database
from comet.observability import log

router = APIRouter(prefix="/admin/cometnet", tags=["API v1 CometNet"])
_events = EventStore(database)


class _V1Request:
    model_config = ConfigDict(extra="forbid", strict=True)


class CreatePoolRequest(SharedCreatePoolRequest, _V1Request):
    pass


class CreateInviteRequest(SharedCreateInviteRequest, _V1Request):
    pass


class JoinPoolRequest(SharedJoinPoolRequest, _V1Request):
    pass


class AddMemberRequest(SharedAddMemberRequest, _V1Request):
    pass


class UpdateMemberRoleRequest(SharedUpdateMemberRoleRequest, _V1Request):
    pass


def _backend() -> CometNetBackend:
    backend = get_active_backend()
    if backend is None:
        raise ApiProblem(
            status_code=503,
            code="cometnet_disabled",
            message="CometNet is not enabled.",
        )
    return backend


def _failed(action: str, *, status_code: int = 409) -> ApiProblem:
    return ApiProblem(
        status_code=status_code,
        code="cometnet_operation_failed",
        message=f"The CometNet {action} operation failed.",
    )


def _mutation(resource_id: str, action: str) -> CometNetMutationData:
    log.info(
        "cometnet.pool.changed",
        "CometNet pool changed",
        pool_id=resource_id,
        operation=action,
    )
    return CometNetMutationData(resource_id=resource_id, action=action)


def _node(stats: dict, enabled: bool) -> CometNetNodeView:
    connection = stats.get("connection_stats") or {}
    gossip = stats.get("gossip_stats") or {}
    relay = stats.get("relay")
    mode = "relay" if relay is not None else "local"
    healthy = bool(stats.get("enabled")) and (
        relay is None or bool(relay.get("running"))
    )
    return CometNetNodeView(
        enabled=enabled,
        healthy=healthy,
        node_id=stats.get("node_id"),
        mode=mode,
        uptime_seconds=float(stats.get("uptime_seconds") or 0),
        contribution_mode=stats.get("contribution_mode"),
        connected_peers=int(connection.get("connected_peers") or 0),
        inbound_peers=int(connection.get("inbound") or 0),
        outbound_peers=int(connection.get("outbound") or 0),
        average_latency_ms=float(connection.get("avg_latency_ms") or 0),
        bytes_sent=int(connection.get("bytes_sent") or 0),
        bytes_received=int(connection.get("bytes_received") or 0),
        messages_sent=int(connection.get("messages_sent") or 0),
        messages_received=int(connection.get("messages_received") or 0),
        torrents_sent=int(gossip.get("torrents_propagated") or 0),
        torrents_received=int(gossip.get("torrents_received") or 0),
        invalid_messages=int(gossip.get("invalid_messages") or 0),
    )


def _peer(peer: dict) -> CometNetPeerView:
    return CometNetPeerView(
        node_id=peer["node_id"],
        alias=peer.get("alias"),
        connected_at=float(peer["connected_at"]),
        last_activity=float(peer["last_activity"]),
        outbound=bool(peer["is_outbound"]),
        latency_ms=float(peer.get("latency_ms") or 0),
        reputation=(float(peer["reputation"]) if "reputation" in peer else None),
        trust_level=peer.get("trust_level"),
        torrents_received=int(peer.get("torrents_received") or 0),
        invalid_contributions=int(peer.get("invalid_contributions") or 0),
        bytes_sent=int(peer.get("bytes_sent") or 0),
        bytes_received=int(peer.get("bytes_received") or 0),
    )


def _pool(
    pool_id: str,
    manifest: dict,
    memberships: set[str],
    subscriptions: set[str],
) -> CometNetPoolView:
    return CometNetPoolView(
        pool_id=pool_id,
        display_name=manifest["display_name"],
        description=manifest.get("description", ""),
        member_count=len(manifest["members"]),
        version=int(manifest["version"]),
        updated_at=float(manifest["updated_at"]),
        membership=pool_id in memberships,
        subscribed=pool_id in subscriptions,
    )


@router.get("/snapshot", response_model=ApiSuccess[CometNetSnapshotData])
async def snapshot(
    request: Request,
    _session: Annotated[str, Depends(require_admin_session)],
):
    backend = get_active_backend()
    event_page = await _events.page(
        EventFilters(category="COMETNET"),
        limit=40,
        include_dropped=False,
    )
    if backend is None:
        return success_response(
            request,
            CometNetSnapshotData(
                collected_at=time.time(),
                node=CometNetNodeView(
                    enabled=False,
                    healthy=False,
                    node_id=None,
                    mode="disabled",
                    uptime_seconds=0,
                    contribution_mode=None,
                    connected_peers=0,
                    inbound_peers=0,
                    outbound_peers=0,
                    average_latency_ms=0,
                    bytes_sent=0,
                    bytes_received=0,
                    messages_sent=0,
                    messages_received=0,
                    torrents_sent=0,
                    torrents_received=0,
                    invalid_messages=0,
                ),
                peers=[],
                pools=[],
                events=[OperationalEventData(**item) for item in event_page.items],
            ),
        )
    stats, peers_payload, pools_payload = await asyncio.gather(
        backend.get_stats(),
        backend.get_peers(),
        backend.get_pools(),
    )
    memberships = set(pools_payload.get("memberships") or [])
    subscriptions = set(pools_payload.get("subscriptions") or [])
    manifests = pools_payload.get("pools") or {}
    return success_response(
        request,
        CometNetSnapshotData(
            collected_at=time.time(),
            node=_node(stats, True),
            peers=[_peer(peer) for peer in peers_payload.get("peers") or []],
            pools=[
                _pool(pool_id, manifest, memberships, subscriptions)
                for pool_id, manifest in sorted(manifests.items())
            ],
            events=[OperationalEventData(**item) for item in event_page.items],
        ),
    )


@router.get(
    "/pools/{pool_id}",
    response_model=ApiSuccess[CometNetPoolDetailData],
)
async def pool_detail(
    request: Request,
    pool_id: PoolIdPath,
    _session: Annotated[str, Depends(require_admin_session)],
):
    backend = _backend()
    detail, pools, invite_payload = await asyncio.gather(
        backend.get_pool_details(pool_id),
        backend.get_pools(),
        backend.get_pool_invites(pool_id),
    )
    if detail is None:
        raise ApiProblem(
            status_code=404,
            code="cometnet_pool_not_found",
            message="The requested CometNet pool was not found.",
        )
    invites = invite_payload.values() if invite_payload else ()
    subscriptions = set(pools.get("subscriptions") or [])
    return success_response(
        request,
        CometNetPoolDetailData(
            pool_id=detail["pool_id"],
            display_name=detail["display_name"],
            description=detail["description"],
            creator_key=detail["creator_key"],
            join_mode=detail["join_mode"],
            version=detail["version"],
            created_at=detail["created_at"],
            updated_at=detail["updated_at"],
            is_admin=detail["is_admin"],
            is_member=detail["is_member"],
            subscribed=pool_id in subscriptions,
            members=[
                CometNetPoolMemberView(
                    public_key=member["public_key"],
                    node_id=member["node_id"],
                    role=member["role"],
                    added_at=member["added_at"],
                    contribution_count=member["contribution_count"],
                    last_seen=member["last_seen"],
                    is_self=member["is_self"],
                )
                for member in detail["members"]
            ],
            invites=[
                CometNetInviteView(
                    invite_code=invite["invite_code"],
                    created_at=invite["created_at"],
                    expires_at=invite["expires_at"],
                    max_uses=invite["max_uses"],
                    uses=invite["uses"],
                    node_url=invite["node_url"],
                )
                for invite in invites
            ],
        ),
    )


@router.post("/pools", response_model=ApiSuccess[CometNetMutationData])
async def create_pool(
    request: Request,
    body: CreatePoolRequest,
    _session: Annotated[str, Depends(require_csrf)],
):
    try:
        await _backend().create_pool(
            body.pool_id,
            body.display_name,
            body.description,
            body.join_mode,
        )
    except (PermissionError, ValueError):
        raise _failed("create") from None
    return success_response(request, _mutation(body.pool_id, "create"))


@router.delete(
    "/pools/{pool_id}",
    response_model=ApiSuccess[CometNetMutationData],
)
async def delete_pool(
    request: Request,
    pool_id: PoolIdPath,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().delete_pool(pool_id):
        raise _failed("delete")
    return success_response(request, _mutation(pool_id, "delete"))


@router.post(
    "/pools/{pool_id}/join",
    response_model=ApiSuccess[CometNetMutationData],
)
async def join_pool(
    request: Request,
    pool_id: PoolIdPath,
    body: JoinPoolRequest,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().join_pool_with_invite(
        pool_id,
        body.invite_code,
        body.node_url,
    ):
        raise _failed("join", status_code=403)
    return success_response(request, _mutation(pool_id, "join"))


@router.post(
    "/pools/{pool_id}/invites",
    response_model=ApiSuccess[CometNetInviteData],
)
async def create_invite(
    request: Request,
    pool_id: PoolIdPath,
    body: CreateInviteRequest,
    _session: Annotated[str, Depends(require_csrf)],
):
    invite_link = await _backend().create_pool_invite(
        pool_id,
        body.expires_in,
        body.max_uses,
    )
    if invite_link is None:
        raise _failed("invite")
    _mutation(pool_id, "create_invite")
    return success_response(
        request,
        CometNetInviteData(pool_id=pool_id, invite_link=invite_link),
    )


@router.delete(
    "/pools/{pool_id}/invites/{invite_code}",
    response_model=ApiSuccess[CometNetMutationData],
)
async def delete_invite(
    request: Request,
    pool_id: PoolIdPath,
    invite_code: InviteCodePath,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().delete_pool_invite(pool_id, invite_code):
        raise _failed("revoke invite")
    return success_response(request, _mutation(pool_id, "revoke_invite"))


@router.post(
    "/pools/{pool_id}/subscription",
    response_model=ApiSuccess[CometNetMutationData],
)
async def subscribe(
    request: Request,
    pool_id: PoolIdPath,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().subscribe_to_pool(pool_id):
        raise _failed("subscribe")
    return success_response(request, _mutation(pool_id, "subscribe"))


@router.delete(
    "/pools/{pool_id}/subscription",
    response_model=ApiSuccess[CometNetMutationData],
)
async def unsubscribe(
    request: Request,
    pool_id: PoolIdPath,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().unsubscribe_from_pool(pool_id):
        raise _failed("unsubscribe")
    return success_response(request, _mutation(pool_id, "unsubscribe"))


@router.post(
    "/pools/{pool_id}/members",
    response_model=ApiSuccess[CometNetMutationData],
)
async def add_member(
    request: Request,
    pool_id: PoolIdPath,
    body: AddMemberRequest,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().add_pool_member(pool_id, body.member_key, body.role):
        raise _failed("add member")
    return success_response(request, _mutation(pool_id, "add_member"))


@router.delete(
    "/pools/{pool_id}/members/{member_key}",
    response_model=ApiSuccess[CometNetMutationData],
)
async def remove_member(
    request: Request,
    pool_id: PoolIdPath,
    member_key: MemberKeyPath,
    _session: Annotated[str, Depends(require_csrf)],
):
    if not await _backend().remove_pool_member(pool_id, member_key):
        raise _failed("remove member")
    return success_response(request, _mutation(pool_id, "remove_member"))


@router.patch(
    "/pools/{pool_id}/members/{member_key}",
    response_model=ApiSuccess[CometNetMutationData],
)
async def update_member(
    request: Request,
    pool_id: PoolIdPath,
    member_key: MemberKeyPath,
    body: UpdateMemberRoleRequest,
    _session: Annotated[str, Depends(require_csrf)],
):
    try:
        changed = await _backend().update_member_role(
            pool_id,
            member_key,
            body.role,
        )
    except (PermissionError, ValueError):
        raise _failed("update member") from None
    if not changed:
        raise _failed("update member")
    return success_response(request, _mutation(pool_id, "update_member"))


@router.post(
    "/pools/{pool_id}/leave",
    response_model=ApiSuccess[CometNetMutationData],
)
async def leave_pool(
    request: Request,
    pool_id: PoolIdPath,
    _session: Annotated[str, Depends(require_csrf)],
):
    try:
        left = await _backend().leave_pool(pool_id)
    except (PermissionError, ValueError):
        raise _failed("leave") from None
    if not left:
        raise _failed("leave")
    return success_response(request, _mutation(pool_id, "leave"))
