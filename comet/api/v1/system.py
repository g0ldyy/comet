from __future__ import annotations

import asyncio
import platform
import shutil
import time
from pathlib import Path
from typing import Annotated

import orjson
from fastapi import APIRouter, Depends, Request
from fastapi import Path as ApiPath

from comet.api.endpoints import base
from comet.api.v1.contracts import (
    ApiSuccess,
    BuildInfoData,
    DatabaseStateData,
    FeatureStateData,
    MaintenanceResultData,
    MaintenanceStateData,
    ReadinessView,
    RuntimeInstanceView,
    RuntimeProcessView,
    RuntimeRestartData,
    StorageVolumeData,
    SystemDetailsData,
    SystemSnapshotData,
    UpdateCheckData,
)
from comet.api.v1.responses import ApiProblem, success_response
from comet.api.v1.security import require_admin_session, require_csrf
from comet.core.database import run_retention_cleanup
from comet.core.models import (
    IS_POSTGRES,
    database,
    native_usenet_offered,
    settings,
)
from comet.core.operator_store import OperatorSettingsStore
from comet.core.runtime_registry import RuntimeIdentity
from comet.core.schema_migrations import MIGRATIONS
from comet.observability import log
from comet.services.operator_commands import dispatch_runtime_restart
from comet.utils.update import UpdateManager

router = APIRouter(prefix="/admin/system", tags=["API v1 System"])
_store = OperatorSettingsStore(database)


async def _snapshot(request: Request) -> SystemSnapshotData:
    readiness = await base.current_readiness(
        worker_ready=getattr(request.app.state, "worker_initialized", False)
    )
    instance_rows, process_rows = await asyncio.gather(
        database.fetch_all(
            """
            SELECT *
            FROM runtime_instances
            ORDER BY last_heartbeat DESC, instance_id
            """,
            force_primary=True,
        ),
        database.fetch_all(
            """
            SELECT *
            FROM runtime_processes
            ORDER BY instance_id, role, process_id
            """,
            force_primary=True,
        ),
    )
    processes: dict[str, list[RuntimeProcessView]] = {}
    for row in process_rows:
        processes.setdefault(row["instance_id"], []).append(
            RuntimeProcessView(
                process_id=row["process_id"],
                role=row["role"],
                started_at=row["started_at"],
                last_heartbeat=row["last_heartbeat"],
            )
        )
    runtimes = [
        RuntimeInstanceView(
            instance_id=row["instance_id"],
            alias=row["alias"],
            hostname=row["hostname"],
            started_at=row["started_at"],
            last_heartbeat=row["last_heartbeat"],
            commit_hash=row["commit_hash"],
            branch=row["branch"],
            build_date=row["build_date"],
            applied_revision=row["applied_revision"],
            pending_restart_keys=orjson.loads(row["pending_restart_keys_json"]),
            readiness=ReadinessView.model_validate(orjson.loads(row["readiness_json"])),
            restart_capable=bool(row["restart_capable"]),
            processes=processes.get(row["instance_id"], []),
        )
        for row in instance_rows
    ]
    return SystemSnapshotData(
        stored_revision=await _store.current_revision(),
        applied_revision=settings.APPLIED_SETTINGS_REVISION,
        current_instance_id=RuntimeIdentity.current().instance_id,
        readiness=ReadinessView(
            state=readiness.state,
            components=readiness.components,
        ),
        runtimes=runtimes,
    )


def _disk_volume(
    name: str,
    path: Path,
    configured_limit: int | None = None,
) -> StorageVolumeData | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return StorageVolumeData(
        name=name,
        capacity_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        configured_limit_bytes=configured_limit,
    )


async def _storage() -> list[StorageVolumeData]:
    requests: list[tuple[str, Path, int | None]] = []
    if not IS_POSTGRES:
        requests.append(("database", Path(settings.DATABASE_PATH).parent, None))
    if settings.USENET_ENABLED:
        requests.extend(
            (
                (
                    "usenet_artifacts",
                    Path(settings.USENET_ARTIFACT_DIR),
                    settings.USENET_SPOOL_MAX_BYTES,
                ),
                (
                    "usenet_local",
                    Path(settings.USENET_LOCAL_DATA_DIR),
                    settings.USENET_DISK_CACHE_BYTES,
                ),
            )
        )
    volumes = await asyncio.gather(
        *(
            asyncio.to_thread(_disk_volume, name, path, limit)
            for name, path, limit in requests
        )
    )
    return [volume for volume in volumes if volume is not None]


async def _details(request: Request) -> SystemDetailsData:
    readiness, maintenance_row, storage = await asyncio.gather(
        base.current_readiness(
            worker_ready=getattr(request.app.state, "worker_initialized", False)
        ),
        database.fetch_one(
            """
            SELECT last_startup_cleanup_at
            FROM db_maintenance
            WHERE id = 1
            """,
            force_primary=True,
        ),
        _storage(),
    )
    version = UpdateManager.get_version_info()
    return SystemDetailsData(
        build=BuildInfoData(
            commit_hash=version.commit_hash,
            branch=version.branch,
            build_date=version.build_date,
            container_image=version.is_docker,
            python_version=platform.python_version(),
            python_implementation=platform.python_implementation(),
            native_engine_enabled=settings.USENET_ENGINE_ENABLED,
            native_engine_api_version=1 if settings.USENET_ENGINE_ENABLED else None,
        ),
        database=DatabaseStateData(
            backend="postgresql" if IS_POSTGRES else "sqlite",
            schema_version=MIGRATIONS[-1][0],
            schema_current=readiness.components["schema"] == "current",
            primary_connected=database.is_connected,
            replicas_configured=database.configured_replica_count,
            replicas_active=database.active_replica_count,
            replicas_unavailable=database.unavailable_replica_count,
        ),
        storage=storage,
        features=FeatureStateData(
            torrent_streams=not settings.DISABLE_TORRENT_STREAMS,
            debrid_stream_proxy=bool(settings.PROXY_DEBRID_STREAM),
            background_scraper=settings.BACKGROUND_SCRAPER_ENABLED,
            usenet=settings.USENET_ENABLED,
            native_usenet=native_usenet_offered(settings),
            cometnet=bool(settings.COMETNET_ENABLED),
            prometheus=settings.PROMETHEUS_ENABLED,
            read_replicas=bool(settings.DATABASE_READ_REPLICA_URLS),
        ),
        maintenance=MaintenanceStateData(
            last_retention_at=(
                maintenance_row["last_startup_cleanup_at"]
                if maintenance_row is not None
                else None
            ),
            retention_enabled=settings.DATABASE_STARTUP_CLEANUP_INTERVAL >= 0,
        ),
    )


@router.get("/snapshot", response_model=ApiSuccess[SystemSnapshotData])
async def system_snapshot(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
):
    return success_response(request, await _snapshot(request))


@router.get("/details", response_model=ApiSuccess[SystemDetailsData])
async def system_details(
    request: Request,
    _session_token: Annotated[str, Depends(require_admin_session)],
):
    return success_response(request, await _details(request))


@router.post("/update-check", response_model=ApiSuccess[UpdateCheckData])
async def check_for_updates(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
):
    status = await UpdateManager.check_for_updates()
    return success_response(
        request,
        UpdateCheckData(
            has_update=status.has_update,
            latest_commit_hash=status.latest_commit_hash,
            latest_url=status.latest_url,
            checked_at=status.checked_at.isoformat(),
            error=status.error,
            install_method=(
                "redeploy_container"
                if UpdateManager.get_version_info().is_docker
                else "update_checkout"
            ),
        ),
    )


@router.post("/maintenance/retention", response_model=ApiSuccess[MaintenanceResultData])
async def run_retention(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
):
    if not await run_retention_cleanup():
        raise ApiProblem(
            status_code=409,
            code="maintenance_in_progress",
            message="Retention maintenance is already running.",
        )
    completed_at = time.time()
    log.info(
        "system.retention.completed",
        "Eligible retained data was pruned",
        operation="retention",
    )
    return success_response(
        request,
        MaintenanceResultData(completed_at=completed_at),
    )


@router.post(
    "/runtimes/{instance_id}/restart",
    response_model=ApiSuccess[RuntimeRestartData],
)
async def restart_runtime(
    request: Request,
    _session_token: Annotated[str, Depends(require_csrf)],
    instance_id: Annotated[
        str,
        ApiPath(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$"),
    ],
):
    runtime = await database.fetch_one(
        """
        SELECT restart_capable
        FROM runtime_instances
        WHERE instance_id = :instance_id
        """,
        {"instance_id": instance_id},
        force_primary=True,
    )
    if runtime is None:
        raise ApiProblem(
            status_code=404,
            code="runtime_not_found",
            message="The runtime is not registered.",
        )
    if not runtime["restart_capable"]:
        raise ApiProblem(
            status_code=409,
            code="restart_unavailable",
            message="This runtime is not supervised for remote restart.",
        )
    result = await dispatch_runtime_restart(instance_id)
    if result is None:
        raise ApiProblem(
            status_code=503,
            code="command_timeout",
            message="The runtime did not acknowledge the restart.",
        )
    if result["outcome"] != "succeeded":
        raise ApiProblem(
            status_code=409,
            code=result["error_code"] or "restart_rejected",
            message="The runtime rejected the restart.",
        )
    log.warning(
        "system.runtime.restart_requested",
        "Supervised runtime restart requested",
        operation="restart",
        instance_id=instance_id,
    )
    return success_response(
        request,
        RuntimeRestartData(instance_id=instance_id),
    )
