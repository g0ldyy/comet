from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from comet.api.endpoints.admin import require_admin_auth
from comet.cometnet import CometNetBackend, get_active_backend

router = APIRouter()

# --- Models ---


class CreatePoolRequest(BaseModel):
    pool_id: str
    display_name: str
    description: str = ""
    join_mode: str = "invite"


class CreateInviteRequest(BaseModel):
    expires_in: Optional[int] = None
    max_uses: Optional[int] = None


class JoinPoolRequest(BaseModel):
    invite_code: Optional[str] = None
    node_url: Optional[str] = (
        None  # URL of the node that created the invite (for remote joining)
    )


class AddMemberRequest(BaseModel):
    member_key: str
    role: str = "member"


class UpdateMemberRoleRequest(BaseModel):
    role: str  # "admin" or "member"


class ReportRequest(BaseModel):
    info_hash: str
    reason: str
    description: str = ""
    pool_id: Optional[str] = None


class BlacklistRequest(BaseModel):
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)

    return await backend.get_stats()


@router.get(
    "/admin/api/cometnet/peers",
    tags=["Admin", "CometNet"],
    summary="Get Connected Peers",
)
async def get_peers(
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
    return await backend.get_peers()


@router.get(
    "/admin/api/cometnet/pools",
    tags=["Admin", "CometNet"],
    summary="Get Pools",
)
async def get_pools(
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
    return await backend.get_pools()


@router.post(
    "/admin/api/cometnet/pools",
    tags=["Admin", "CometNet"],
    summary="Create Pool",
)
async def create_pool(
    request: CreatePoolRequest,
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
    try:
        return await backend.create_pool(
            pool_id=request.pool_id,
            display_name=request.display_name,
            description=request.description,
            join_mode=request.join_mode,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/admin/api/cometnet/pools/{pool_id}",
    tags=["Admin", "CometNet"],
    summary="Delete Pool",
)
async def delete_pool(
    pool_id: str,
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
    try:
        if request.invite_code:
            success = await backend.join_pool_with_invite(
                pool_id, request.invite_code, request.node_url
            )
        else:
            raise HTTPException(status_code=400, detail="Invite code required")

        if not success:
            raise HTTPException(status_code=403, detail="Failed to join pool")

        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/invite",
    tags=["Admin", "CometNet"],
    summary="Create Pool Invite",
)
async def create_pool_invite(
    pool_id: str,
    request: CreateInviteRequest,
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
    return await backend.get_pool_invites(pool_id)


@router.delete(
    "/admin/api/cometnet/pools/{pool_id}/invites/{invite_code}",
    tags=["Admin", "CometNet"],
    summary="Delete Pool Invite",
)
async def delete_pool_invite(
    pool_id: str,
    invite_code: str,
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    """Get detailed information about a pool including all members."""
    await require_admin_auth(admin_session)
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
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    """Change a member's role (promote to admin or demote to member)."""
    await require_admin_auth(admin_session)
    try:
        if await backend.update_member_role(pool_id, member_key, request.role):
            return {"status": "success"}
        raise HTTPException(status_code=400, detail="Failed to update role")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post(
    "/admin/api/cometnet/pools/{pool_id}/leave",
    tags=["Admin", "CometNet"],
    summary="Leave Pool",
)
async def leave_pool(
    pool_id: str,
    admin_session: str = Cookie(None),
    backend=Depends(get_cometnet_backend),
):
    """Leave a pool (self-removal). Any member except creator can leave."""
    await require_admin_auth(admin_session)
    try:
        if await backend.leave_pool(pool_id):
            return {"status": "success"}
        raise HTTPException(
            status_code=400, detail="Failed to leave pool (not a member?)"
        )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
