import RTN

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from comet.utils.models import settings

templates = Jinja2Templates("comet/templates")
main = APIRouter()


@main.get("/", status_code=200)
async def root():
    return RedirectResponse("/configure")


@main.get("/health", status_code=200)
async def health():
    return {"status": "ok"}


indexers = settings.INDEXER_MANAGER_INDEXERS

webConfig = {
    "indexers": [indexer.replace(" ", "_") for indexer in indexers],
    "languages": [language.replace(" ", "_") for language in RTN.patterns.language_code_mapping.keys()],
    "resolutions": ["480p", "720p", "1080p", "1440p", "2160p", "2880p", "4320p"]
}

@main.get("/configure")
@main.get("/{b64config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "CUSTOM_HEADER_HTML": settings.CUSTOM_HEADER_HTML if settings.CUSTOM_HEADER_HTML and settings.CUSTOM_HEADER_HTML != "None" else "", "webConfig": webConfig})


@main.get("/manifest.json")
@main.get("/{b64config}/manifest.json")
async def manifest():
    return {
        "id": "stremio.comet.fast",
        "version": "1.0.0",
        "name": "Comet",
        "description": "Stremio's fastest torrent/debrid search add-on.",
        "logo": "https://i.imgur.com/jmVoVMu.jpeg",
        "background": "https://i.imgur.com/WwnXB3k.jpeg",
        "resources": [
            "stream"
        ],
        "types": [
            "movie",
            "series"
        ],
        "idPrefixes": [
            "tt"
        ],
        "catalogs": [],
        "behaviorHints": {
            "configurable": True
        }
    }
