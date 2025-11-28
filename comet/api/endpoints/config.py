from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from comet.core.models import settings, web_config

router = APIRouter()
templates = Jinja2Templates("comet/templates")


@router.get("/configure")
@router.get("/{b64config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML
            if settings.CUSTOM_HEADER_HTML
            else "",
            "webConfig": web_config,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
        },
    )
