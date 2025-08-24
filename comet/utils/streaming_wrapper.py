from typing import AsyncGenerator
from starlette.responses import Response
from starlette.background import BackgroundTask

import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils
from mediaflow_proxy.utils.http_utils import EnhancedStreamingResponse

from comet.utils.bandwidth_monitor import bandwidth_monitor


class BandwidthMonitoringStreamingResponse(EnhancedStreamingResponse):
    def __init__(
        self,
        content: AsyncGenerator[bytes, None],
        status_code: int = 200,
        headers: dict = None,
        media_type: str = None,
        background: BackgroundTask = None,
        connection_id: str = None,
    ):
        super().__init__(content, status_code, headers, media_type, background)
        self.connection_id = connection_id

    async def stream_response(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )

        async for chunk in self.body_iterator:
            # Monitor bandwidth for this chunk
            if self.connection_id and chunk:
                bandwidth_monitor.update_connection(self.connection_id, len(chunk))

            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                }
            )

        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )

        if self.background is not None:
            await self.background()


async def monitored_handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: mediaflow_proxy.utils.http_utils.ProxyRequestHeaders,
    connection_id: str = None,
) -> Response:
    """
    Wrapper around mediaflow-proxy's handle_stream_request with bandwidth monitoring.

    Args:
        method: HTTP method (GET/HEAD)
        video_url: URL to stream
        proxy_headers: Proxy headers
        connection_id: Connection ID for monitoring

    Returns:
        Response with bandwidth monitoring
    """
    # Call original mediaflow-proxy handler
    response = await mediaflow_proxy.handlers.handle_stream_request(
        method, video_url, proxy_headers
    )

    # If it's a streaming response and we have a connection_id, wrap it for monitoring
    if isinstance(response, EnhancedStreamingResponse) and connection_id:
        return BandwidthMonitoringStreamingResponse(
            content=response.body_iterator,
            status_code=response.status_code,
            headers=dict(response.headers),
            background=response.background,
            connection_id=connection_id,
        )

    return response


async def create_monitoring_wrapper():
    if not bandwidth_monitor._initialized:
        await bandwidth_monitor.initialize()
