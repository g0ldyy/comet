import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils
from mediaflow_proxy.utils.http_utils import EnhancedStreamingResponse

from comet.services.bandwidth import bandwidth_monitor


async def _monitor_content(content, connection_id: str, charset: str):
    bandwidth_monitor.bind_stream_task(connection_id)
    async for chunk in content:
        if chunk:
            size = len(chunk.encode(charset)) if isinstance(chunk, str) else len(chunk)
            bandwidth_monitor.update_connection(connection_id, size)
        yield chunk


async def monitored_handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: mediaflow_proxy.utils.http_utils.ProxyRequestHeaders,
    connection_id: str | None = None,
):
    response = await mediaflow_proxy.handlers.handle_stream_request(
        method,
        video_url,
        proxy_headers,
    )
    if not isinstance(response, EnhancedStreamingResponse) or connection_id is None:
        return response
    return EnhancedStreamingResponse(
        content=_monitor_content(
            response.body_iterator,
            connection_id,
            response.charset,
        ),
        status_code=response.status_code,
        headers=dict(response.headers),
        background=response.background,
    )
