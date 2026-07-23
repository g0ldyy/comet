from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from comet.api.endpoints.admin import require_admin_auth
from comet.cometnet import CometNetBackend, get_active_backend

router = APIRouter(dependencies=[Depends(require_admin_auth)])

# --- Models ---


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreatePoolRequest(StrictRequest):
    pool_id: str
    display_name: str
    description: str = ""
    join_mode: str = "invite"


class CreateInviteRequest(StrictRequest):
    expires_in: Optional[int] = None
    max_uses: Optional[int] = None


class JoinPoolRequest(StrictRequest):
    invite_code: str
    node_url: Optional[str] = (
        None  # URL of the node that created the invite (for remote joining)
    )


class AddMemberRequest(StrictRequest):
    member_key: str
    role: str = "member"


class UpdateMemberRoleRequest(StrictRequest):
    role: str  # "admin" or "member"


class ReportRequest(StrictRequest):
    info_hash: str
    reason: str
    description: str = ""
    pool_id: Optional[str] = None


class BlacklistRequest(StrictRequest):
    info_hash: str
    reason: str
    pool_id: Optional[str] = None


# --- Endpoints ---


def get_cometnet_backend() -> CometNetBackend:
    """
    Get the active CometNet backend (either local service or relay).
    Raises HTTPException if neither is available.
    """
    backend = get_active_backend()
    if backend:
        return backend

    raise HTTPException(
        status_code=503,
        detail="CometNet is not enabled (neither local service nor relay configured)",
    )


@router.get(
    "/admin/api/cometnet/stats",
    tags=["Admin", "CometNet"],
    summary="Get CometNet Stats",
)
async def get_stats(
    backend=Depends(get_cometnet_backend),
):
    return await backend.get_stats()


@router.get(
    "/admin/api/cometnet/peers",
    tags=["Admin", "CometNet"],
    summary="Get Connected Peers",
)
async def get_peers(
    backend=Depends(get_cometnet_backend),
):
    return await backend.get_peers()


@router.get(
    "/admin/api/cometnet/pools",
    tags=["Admin", "CometNet"],
    summary="Get Pools",
)
async def get_pools(
    backend=Depends(get_cometnet_backend),
):
    return await backend.get_pools()


@router.post(
    "/admin/api/cometnet/pools",
    tags=["Admin", "CometNet"],
    summary="Create Pool",
)
async def create_pool(
    request: CreatePoolRequest,
    backend=Depends(get_cometnet_backend),
):
    try:
        return await backend.create_pool(
            pool_id=request.pool_id,
            display_name=request.display_name,
            description=request.description,
            join_mode=request.join_mode,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid pool request") from error
    except PermissionError as error:
        raise HTTPException(
            status_code=403, detail="Pool operation is forbidden"
        ) from error


@router.delete(
    "/admin/api/cometnet/pools/{pool_id}",
    tags=["Admin", "CometNet"],
    summary="Delete Pool",
)
async def delete_pool(
    pool_id: str,
    backend=Depends(get_cometnet_backend),
):
    if await backend.delete_pool(pool_id):
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Pool not found or failed to delete")


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/join",
    tags=["Admin", "CometNet"],
    summary="Join Pool",
)
async def join_pool(
    pool_id: str,
    request: JoinPoolRequest,
    backend=Depends(get_cometnet_backend),
):
    success = await backend.join_pool_with_invite(
        pool_id, request.invite_code, request.node_url
    )
    if not success:
        raise HTTPException(status_code=403, detail="Failed to join pool")

    return {"status": "success"}


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/invite",
    tags=["Admin", "CometNet"],
    summary="Create Pool Invite",
)
async def create_pool_invite(
    pool_id: str,
    request: CreateInviteRequest,
    backend=Depends(get_cometnet_backend),
):
    invite_link = await backend.create_pool_invite(
        pool_id, request.expires_in, request.max_uses
    )
    if invite_link:
        return {"invite_link": invite_link}
    raise HTTPException(status_code=400, detail="Failed to create invite")


@router.get(
    "/admin/api/cometnet/pools/{pool_id}/invites",
    tags=["Admin", "CometNet"],
    summary="Get Pool Invites",
)
async def get_pool_invites(
    pool_id: str,
    backend=Depends(get_cometnet_backend),
):
    return await backend.get_pool_invites(pool_id)


@router.delete(
    "/admin/api/cometnet/pools/{pool_id}/invites/{invite_code}",
    tags=["Admin", "CometNet"],
    summary="Delete Pool Invite",
)
async def delete_pool_invite(
    pool_id: str,
    invite_code: str,
    backend=Depends(get_cometnet_backend),
):
    success = await backend.delete_pool_invite(pool_id, invite_code)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to delete invite")
    return {"status": "success"}


@router.delete(
    "/admin/api/cometnet/pools/{pool_id}/subscribe",
    tags=["Admin", "CometNet"],
    summary="Unsubscribe from Pool",
)
async def unsubscribe_pool(
    pool_id: str,
    backend=Depends(get_cometnet_backend),
):
    if await backend.unsubscribe_from_pool(pool_id):
        return {"status": "success"}
    return {"status": "failed"}


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/subscribe",
    tags=["Admin", "CometNet"],
    summary="Subscribe to Pool",
)
async def subscribe_pool(
    pool_id: str,
    backend=Depends(get_cometnet_backend),
):
    if await backend.subscribe_to_pool(pool_id):
        return {"status": "success"}
    return {"status": "failed"}


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/members",
    tags=["Admin", "CometNet"],
    summary="Add Pool Member",
)
async def add_pool_member(
    pool_id: str,
    request: AddMemberRequest,
    backend=Depends(get_cometnet_backend),
):
    if await backend.add_pool_member(pool_id, request.member_key, request.role):
        return {"status": "success"}
    raise HTTPException(status_code=400, detail="Failed to add member")


@router.delete(
    "/admin/api/cometnet/pools/{pool_id}/members/{member_key}",
    tags=["Admin", "CometNet"],
    summary="Remove Pool Member",
)
async def remove_pool_member(
    pool_id: str,
    member_key: str,
    backend=Depends(get_cometnet_backend),
):
    if await backend.remove_pool_member(pool_id, member_key):
        return {"status": "success"}
    raise HTTPException(status_code=400, detail="Failed to remove member")


@router.get(
    "/admin/api/cometnet/pools/{pool_id}",
    tags=["Admin", "CometNet"],
    summary="Get Pool Details",
)
async def get_pool_details(
    pool_id: str,
    backend=Depends(get_cometnet_backend),
):
    """Get detailed information about a pool including all members."""
    pool = await backend.get_pool_details(pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")
    return pool


@router.patch(
    "/admin/api/cometnet/pools/{pool_id}/members/{member_key}/role",
    tags=["Admin", "CometNet"],
    summary="Update Member Role",
)
async def update_member_role(
    pool_id: str,
    member_key: str,
    request: UpdateMemberRoleRequest,
    backend=Depends(get_cometnet_backend),
):
    """Change a member's role (promote to admin or demote to member)."""
    try:
        if await backend.update_member_role(pool_id, member_key, request.role):
            return {"status": "success"}
        raise HTTPException(status_code=400, detail="Failed to update role")
    except PermissionError as error:
        raise HTTPException(
            status_code=403, detail="Pool operation is forbidden"
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid pool request") from error


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/leave",
    tags=["Admin", "CometNet"],
    summary="Leave Pool",
)
async def leave_pool(
    pool_id: str,
    backend=Depends(get_cometnet_backend),
):
    """Leave a pool (self-removal). Any member except creator can leave."""
    try:
        if await backend.leave_pool(pool_id):
            return {"status": "success"}
        raise HTTPException(
            status_code=400, detail="Failed to leave pool (not a member?)"
        )
    except PermissionError as error:
        raise HTTPException(
            status_code=403, detail="Pool operation is forbidden"
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Invalid pool request") from error
