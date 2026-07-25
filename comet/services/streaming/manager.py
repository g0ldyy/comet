import asyncio
import time
import uuid

import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils
from starlette.background import BackgroundTask

from comet.core.logger import logger
from comet.core.models import database, settings
from comet.services.bandwidth import bandwidth_monitor
from comet.services.lock import DistributedLock
from comet.services.status_video import build_status_video_response
from comet.services.streaming.wrapper import monitored_handle_stream_request


async def on_stream_end(connection_id: str, ip: str):
    cancellation = None
    try:
        await bandwidth_monitor.end_connection(connection_id)
    except asyncio.CancelledError as exc:
        cancellation = exc
    except Exception as e:
        logger.warning(
            f"Error ending bandwidth tracking for connection {connection_id}: {e}"
        )

    try:
        await database.execute(
            "DELETE FROM active_connections WHERE id = :connection_id AND ip = :ip",
            {"connection_id": connection_id, "ip": ip},
        )
        logger.log(
            "STREAM", f"Stream ended - Connection: {connection_id} from IP: {ip}"
        )
    except Exception as e:
        logger.warning(
            f"Error deleting stream connection {connection_id} from IP {ip}: {e}"
        )

    if cancellation is not None:
        raise cancellation


async def check_ip_connections(ip: str):
    if settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS <= -1:
        return True

    try:
        count = await database.fetch_val(
            "SELECT COUNT(*) FROM active_connections WHERE ip = :ip",
            {"ip": ip},
        )
        if count >= settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS:
            logger.log(
                "STREAM",
                f"Connection limit reached for IP: {ip} ({count} active connections)",
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"Error checking IP connections for {ip}: {e}")
        return False


async def add_active_connection(media_id: str, ip: str):
    connection_id = str(uuid.uuid4())

    await database.execute(
        "INSERT INTO active_connections (id, ip, content, started_at) VALUES (:connection_id, :ip, :content, :started_at)",
        {
            "connection_id": connection_id,
            "ip": ip,
            "content": media_id,
            "started_at": time.time(),
        },
    )

    try:
        await bandwidth_monitor.start_connection(connection_id, ip, media_id)
    except BaseException:
        try:
            await database.execute(
                "DELETE FROM active_connections WHERE id = :connection_id AND ip = :ip",
                {"connection_id": connection_id, "ip": ip},
            )
        except Exception as cleanup_error:
            logger.warning(
                f"Error rolling back stream connection {connection_id}: {cleanup_error}"
            )
        raise

    logger.log(
        "STREAM",
        f"New stream connection - ID: {connection_id}, IP: {ip}, Content: {media_id}",
    )
    return connection_id


async def admit_active_connection(media_id: str, ip: str) -> str | None:
    if settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS <= -1:
        return await add_active_connection(media_id, ip)

    lock = DistributedLock(
        f"stream-admission:{ip}",
        timeout=10,
        retry_interval=0.05,
    )
    if not await lock.acquire(wait_timeout=5):
        logger.warning(f"Could not serialize stream admission for IP: {ip}")
        return None

    try:
        if not await check_ip_connections(ip):
            return None
        return await add_active_connection(media_id, ip)
    finally:
        await lock.release()


async def combined_background_tasks(
    connection_id: str,
    ip: str,
    streamer_close_task: BackgroundTask | None,
):
    try:
        if streamer_close_task is not None:
            await streamer_close_task()
    finally:
        await on_stream_end(connection_id, ip)


async def custom_handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: mediaflow_proxy.utils.http_utils.ProxyRequestHeaders,
    media_id: str,
    ip: str,
):
    connection_id = await admit_active_connection(media_id, ip)
    if connection_id is None:
        return build_status_video_response(
            ["PROXY_LIMIT_REACHED"],
            default_key="PROXY_LIMIT_REACHED",
        )

    try:
        response = await monitored_handle_stream_request(
            method, video_url, proxy_headers, connection_id
        )
    except BaseException:
        await on_stream_end(connection_id, ip)
        raise

    original_background_task = response.background
    response.background = BackgroundTask(
        combined_background_tasks,
        connection_id=connection_id,
        ip=ip,
        streamer_close_task=original_background_task,
    )
    return response
