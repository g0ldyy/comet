import time
import uuid
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse

from comet.utils.models import settings, database
from comet.utils.logger import logger
import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils


async def on_stream_end(connection_id: str, ip: str):
    await database.execute(
        "DELETE FROM active_connections WHERE id = :connection_id AND ip = :ip",
        {"connection_id": connection_id, "ip": ip},
    )
    logger.log("STREAM", f"Stream ended - Connection: {connection_id} from IP: {ip}")


async def check_ip_connections(ip: str):
    if settings.PROXY_DEBRID_STREAM_MAX_CONNECTIONS <= -1:
        return True

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


async def add_active_connection(media_id: str, ip: str):
    connection_id = str(uuid.uuid4())
    await database.execute(
        "INSERT INTO active_connections (id, ip, content, timestamp) VALUES (:connection_id, :ip, :content, :timestamp)",
        {
            "connection_id": connection_id,
            "ip": ip,
            "content": media_id,
            "timestamp": time.time(),
        },
    )
    logger.log(
        "STREAM",
        f"New stream connection - ID: {connection_id}, IP: {ip}, Content: {media_id}",
    )
    return connection_id


async def combined_background_tasks(
    connection_id: str, ip: str, streamer_close_task: BackgroundTask
):
    await streamer_close_task()
    await on_stream_end(connection_id, ip)


async def custom_handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: mediaflow_proxy.utils.http_utils.ProxyRequestHeaders,
    media_id: str,
    ip: str,
):
    if not await check_ip_connections(ip):
        return FileResponse("comet/assets/proxylimit.mp4")

    connection_id = await add_active_connection(media_id, ip)

    response = await mediaflow_proxy.handlers.handle_stream_request(
        method, video_url, proxy_headers
    )
    original_background_task = response.background
    response.background = BackgroundTask(
        combined_background_tasks,
        connection_id=connection_id,
        ip=ip,
        streamer_close_task=original_background_task,
    )
    return response
