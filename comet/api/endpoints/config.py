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
    html_keys = [
        "CUSTOM_LOGO_URL",
        "CUSTOM_ADDON_NAME",
        "CUSTOM_DISCORD_URL",
        "CUSTOM_HEADER_HTML",
    ]

    def normalize(v):
        if v is None:
            return ""
        if isinstance(v, str) and v.strip().lower() in ("none", "null"):
            return ""
        return v

    html_context = {k: normalize(getattr(settings, k)) for k in html_keys}

    response = templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            **html_context,
            "webConfig": web_config,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
            "disableTorrentStreams": settings.DISABLE_TORRENT_STREAMS,
        },
    )

    if settings.HTTP_CACHE_ENABLED:
        response.headers["Cache-Control"] = CachePolicies.configure_page().build()
        response.headers["Vary"] = "Accept, Accept-Encoding"

    return response
