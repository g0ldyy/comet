from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Query, Request

from comet.api.endpoints.stream import stream as get_streams
from comet.core.config_validation import config_check
from comet.core.models import settings
from comet.debrid.manager import build_addon_name

router = APIRouter()


@router.get(
    "/manifest",
    tags=["ChillLink"],
    summary="Add-on Manifest",
    description="Returns the add-on manifest.",
)
@router.get(
    "/{b64config}/manifest",
    tags=["ChillLink"],
    summary="Add-on Manifest",
    description="Returns the add-on manifest with existing configuration.",
)
async def chilllink_manifest(request: Request, b64config: str = None):
    config = config_check(b64config, strict_b64config=True)

    manifest = {
        "id": settings.ADDON_ID,
        "version": "2.0.0",
        "description": "Chillio's fastest debrid search add-on.",
        "supported_endpoints": {"feeds": None, "streams": "/streams"},
        "name": build_addon_name(settings.ADDON_NAME, config)
        if config
        else "❌ | Comet",
    }

    if not config:
        manifest["description"] = (
            f"OBSOLETE CONFIGURATION, PLEASE RE-CONFIGURE ON {request.url.scheme}://{request.url.netloc}"
        )

    return manifest


@router.get(
    "/streams",
    tags=["ChillLink"],
    summary="Stream Provider",
    description="Returns a list of streams for the specified media.",
)
@router.get(
    "/{b64config}/streams",
    tags=["ChillLink"],
    summary="Stream Provider",
    description="Returns a list of streams for the specified media with existing configuration.",
)
async def chilllink_streams(
    request: Request,
    background_tasks: BackgroundTasks,
    imdbID: str = Query(...),
    type: str = Query(...),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    b64config: Optional[str] = None,
):
    config = config_check(b64config, strict_b64config=True)
    if not config:
        return {
            "sources": [
                {
                    "id": "comet.fast",
                    "title": "Configuration is invalid. Please reconfigure Comet.",
                    "url": "https://comet.feels.legal",
                    "metadata": [],
                }
            ]
        }

    debrid_entries = config["_debridEntries"]

    if not debrid_entries:
        return {
            "sources": [
                {
                    "id": "comet.fast",
                    "title": "You need to configure a debrid service to use Comet in Chillio.",
                    "url": "https://comet.feels.legal",
                    "metadata": [],
                }
            ]
        }

    if type == "movie":
        media_id = imdbID
    elif type == "series":
        media_id = f"{imdbID}:{season}:{episode}"
    else:
        return {"sources": []}

    stremio_response = await get_streams(
        request=request,
        media_type=type,
        media_id=media_id,
        background_tasks=background_tasks,
        b64config=b64config,
        chilllink=True,
    )

    stremio_streams = stremio_response["streams"]
    sources = [
        {
            "id": stream["behaviorHints"]["bingeGroup"],
            "title": stream["behaviorHints"]["filename"],
            "url": stream["url"],
            "metadata": stream["_chilllink"],
        }
        for stream in stremio_streams
    ]

    return {"sources": sources}
