import PTT
import RTN

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from comet.utils.models import settings
from comet.utils.general import config_check, get_debrid_extension

templates = Jinja2Templates("comet/templates")
main = APIRouter()


@main.get("/", status_code=200)
async def root():
    return RedirectResponse("/configure")


@main.get("/health", status_code=200)
async def health():
    return {"status": "ok"}


indexers = settings.INDEXER_MANAGER_INDEXERS
languages = [language for language in PTT.parse.LANGUAGES_TRANSLATION_TABLE.values()]
languages.insert(0, "Unknown")
languages.insert(1, "Multi")
web_config = {
    "indexers": [indexer.replace(" ", "_").lower() for indexer in indexers],
    "languages": languages,
    "resolutions": [resolution.value for resolution in RTN.models.Resolution],
    "resultFormat": ["Title", "Metadata", "Size", "Tracker", "Languages"],
}


@main.get("/configure")
@main.get("/{b64config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML
            if settings.CUSTOM_HEADER_HTML
            else "",
            "webConfig": web_config,
            "indexerManager": settings.INDEXER_MANAGER_TYPE,
            "proxyDebridStream": settings.PROXY_DEBRID_STREAM,
        },
    )


@main.get("/manifest.json")
@main.get("/{b64config}/manifest.json")
async def manifest(b64config: str = None):
    config = config_check(b64config)
    if not config:
        config = {"debridService": None}

    debrid_extension = get_debrid_extension(config["debridService"])

    return {
        "id": settings.ADDON_ID,
        "name": f"{settings.ADDON_NAME}{(' | ' + debrid_extension) if debrid_extension is not None else ''}",
        "description": "Stremio's fastest torrent/debrid search add-on.",
        "version": "1.0.0",
        "catalogs": [],
        "resources": [
            {
                "name": "stream",
                "types": ["movie", "series"],
                "idPrefixes": ["tt", "kitsu"],
            }
        ],
        "types": ["movie", "series", "anime", "other"],
        "logo": "https://i.imgur.com/jmVoVMu.jpeg",
        "background": "https://i.imgur.com/WwnXB3k.jpeg",
        "behaviorHints": {"configurable": True, "configurationRequired": False},
    }
