from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from comet.core.models import settings, web_config
from comet.utils.cache import CachePolicies

router = APIRouter()
templates = Jinja2Templates("comet/templates")


@router.get(
    "/configure",
    tags=["Configuration"],
    summary="Configuration Page",
    description="Renders the configuration page.",
)
@router.get(
    "/{b64config}/configure",
    tags=["Configuration"],
    summary="Configuration Page",
    description="Renders the configuration page with existing configuration.",
)
async def configure(request: Request):
    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML
            if settings.CUSTOM_HEADER_HTML
            else "",
            "webConfig": web_config,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
            "disableTorrentStreams": settings.DISABLE_TORRENT_STREAMS,
        },
    )

    if settings.HTTP_CACHE_ENABLED:
        response.headers["Cache-Control"] = CachePolicies.configure_page().build()
        response.headers["Vary"] = "Accept, Accept-Encoding"

    return response
