import mediaflow_proxy.handlers
import mediaflow_proxy.utils.http_utils
from mediaflow_proxy.configs import settings as mediaflow_settings
from mediaflow_proxy.utils.http_client import (
    get_routing_config,
    initialize_routing_from_config,
)
from mediaflow_proxy.utils.http_utils import EnhancedStreamingResponse

from comet.core.models import settings
from comet.services.bandwidth import bandwidth_monitor
from comet.utils.http_client import ProxyPolicy


def configure_stream_proxy():
    proxy_url = ProxyPolicy(settings.GLOBAL_PROXY_URL).initial_url
    config = mediaflow_settings.transport_config
    if proxy_url is None:
        initialize_routing_from_config(config)
        return

    initialize_routing_from_config(
        config.model_copy(update={"proxy_url": proxy_url, "all_proxy": True})
    )
    routing = get_routing_config()
    for pattern, (verify_ssl, _) in routing.routes.items():
        routing.add_route(pattern, verify_ssl=verify_ssl, proxy_url=proxy_url)


async def _monitor_content(content, connection_id: str, charset: str):
    async for chunk in content:
        if chunk:
            chunk_size = (
                len(chunk.encode(charset)) if isinstance(chunk, str) else len(chunk)
            )
            bandwidth_monitor.update_connection(connection_id, chunk_size)
        yield chunk


async def monitored_handle_stream_request(
    method: str,
    video_url: str,
    proxy_headers: mediaflow_proxy.utils.http_utils.ProxyRequestHeaders,
    connection_id: str | None = None,
):
    response = await mediaflow_proxy.handlers.handle_stream_request(
        method, video_url, proxy_headers
    )

    if isinstance(response, EnhancedStreamingResponse) and connection_id:
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

    return response
