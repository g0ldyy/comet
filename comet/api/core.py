import os

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates("comet/templates")
main = APIRouter()


@main.get("/", status_code=200)
async def root():
    return RedirectResponse("/configure")


@main.get("/health", status_code=200)
async def health():
    return {"status": "ok"}


@main.get("/configure")
@main.get("/{b64config}/configure")
async def configure(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "CUSTOM_HEADER_HTML": os.getenv("CUSTOM_HEADER_HTML", "")})


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
