import asyncio
import os
import time
import uuid

import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils
from starlette.background import BackgroundTask

from comet.core.models import database, settings
from comet.core.runtime_registry import RuntimeIdentity
from comet.observability import log
from comet.services.bandwidth import bandwidth_monitor
from comet.services.lock import DistributedLock
from comet.services.status_video import build_status_video_response
from comet.services.streaming.wrapper import monitored_handle_stream_request


def _observe_cleanup_failure(operation: str, error: Exception) -> None:
    log.warning(
        "stream.cleanup.failed",
        "Stream cleanup failed",
        operation=operation,
        failure_reason=operation,
        details=type(error).__name__,
        error_code="dependency_warning",
    )


async def on_stream_end(
    connection_id: str,
    ip: str,
    *,
    outcome: str,
    error_code: str | None = None,
):
    cancellation = None
    try:
        await bandwidth_monitor.end_connection(
            connection_id,
            outcome=outcome,
            error_code=error_code,
        )
    except asyncio.CancelledError as exc:
        cancellation = exc
    except Exception as error:
        _observe_cleanup_failure("bandwidth_cleanup", error)

    try:
        await database.execute(
            "DELETE FROM active_connections WHERE id = :connection_id AND ip = :ip",
            {"connection_id": connection_id, "ip": ip},
        )
    except Exception as error:
        _observe_cleanup_failure("database_cleanup", error)

    if cancellation is not None:
        raise cancellation


async def check_ip_connections(ip: str):
    if settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS <= -1:
        return True

    count = await database.fetch_val(
        "SELECT COUNT(*) FROM active_connections WHERE ip = :ip",
        {"ip": ip},
    )
    return count < settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS


async def add_active_connection(media_id: str, ip: str, service: str):
    connection_id = str(uuid.uuid4())
    started_at = time.time()
    identity = RuntimeIdentity.current()

    await database.execute(
        """
        INSERT INTO active_connections (
            id, ip, content, service, instance_id, process_id,
            started_at, updated_at
        ) VALUES (
            :connection_id, :ip, :content, :service, :instance_id, :process_id,
            :started_at, :started_at
        )
        """,
        {
            "connection_id": connection_id,
            "ip": ip,
            "content": media_id,
            "service": service,
            "instance_id": identity.instance_id,
            "process_id": os.getpid(),
            "started_at": started_at,
        },
    )

    try:
        await bandwidth_monitor.start_connection(
            connection_id,
            ip,
            media_id,
            service,
            started_at=started_at,
        )
    except BaseException:
        try:
            await database.execute(
                "DELETE FROM active_connections WHERE id = :connection_id AND ip = :ip",
                {"connection_id": connection_id, "ip": ip},
            )
        except Exception as error:
            _observe_cleanup_failure("database_rollback", error)
        raise
    return connection_id


async def admit_active_connection(media_id: str, ip: str, service: str) -> str | None:
    if settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS <= -1:
        return await add_active_connection(media_id, ip, service)

    lock = DistributedLock(
        f"stream-admission:{ip}",
        timeout=10,
        retry_interval=0.05,
    )
    if not await lock.acquire(wait_timeout=5):
        return None

    try:
        if not await check_ip_connections(ip):
            return None
        return await add_active_connection(media_id, ip, service)
    finally:
        await lock.release()


async def combined_background_tasks(
    connection_id: str,
    ip: str,
    streamer_close_task: BackgroundTask | None,
):
    outcome = "completed"
    error_code = None
    try:
        if streamer_close_task is not None:
            await streamer_close_task()
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "failed"
        error_code = "upstream_close_failed"
        raise
    finally:
        await on_stream_end(
            connection_id,
            ip,
            outcome=outcome,
            error_code=error_code,
        )


async def custom_handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: mediaflow_proxy.utils.http_utils.ProxyRequestHeaders,
    media_id: str,
    ip: str,
    service: str,
    source_type: str = "unknown",
):
    connection_id = await admit_active_connection(media_id, ip, service)
    if connection_id is None:
        return build_status_video_response(
            ["PROXY_LIMIT_REACHED"],
            default_key="PROXY_LIMIT_REACHED",
        )

    try:
        response = await monitored_handle_stream_request(
            method,
            video_url,
            proxy_headers,
            connection_id,
        )
    except asyncio.CancelledError:
        await on_stream_end(connection_id, ip, outcome="cancelled")
        raise
    except BaseException:
        await on_stream_end(
            connection_id,
            ip,
            outcome="failed",
            error_code="upstream_request_failed",
        )
        raise

    original_background_task = response.background
    response.background = BackgroundTask(
        combined_background_tasks,
        connection_id=connection_id,
        ip=ip,
        streamer_close_task=original_background_task,
    )
    response.comet_playback_mode = "proxy"
    response.comet_source_type = source_type
    return response
