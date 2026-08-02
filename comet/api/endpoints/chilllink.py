from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Request

from comet.api.endpoints.stream import stream as get_streams
from comet.core.config_validation import config_check
from comet.core.manifest_branding import eligible_usenet_provider_badges
from comet.core.models import database, settings
from comet.debrid.manager import build_addon_name
from comet.utils.parsing import parse_media_id

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
async def chilllink_manifest(request: Request, b64config: str | None = None):
    config = config_check(b64config)
    usenet_badges = (
        await eligible_usenet_provider_badges(
            config,
            database,
            usenet_offered=settings.USENET_ENABLED,
            capability_secret=settings.COMET_CAPABILITY_SECRET,
            native_access_token=settings.USENET_NATIVE_ACCESS_TOKEN,
            native_servers=settings.USENET_NATIVE_SERVERS,
        )
        if config
        else ()
    )

    manifest = {
        "id": settings.ADDON_ID,
        "version": "2.0.0",
        "description": "Chillio's fastest debrid/usenet search add-on.",
        "supported_endpoints": {"feeds": None, "streams": "/streams"},
        "name": build_addon_name(settings.ADDON_NAME, config, usenet_badges)
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
    imdbID: Annotated[str, Query(min_length=1, max_length=64)],
    type: Annotated[str, Query(min_length=1, max_length=16)],
    season: Annotated[int | None, Query(ge=0, le=65_535)] = None,
    episode: Annotated[int | None, Query(ge=0, le=65_535)] = None,
    b64config: str | None = None,
):
    if (type == "movie" and (season is not None or episode is not None)) or (
        episode is not None and season is None
    ):
        return {"sources": []}

    media_id = imdbID
    if type == "series" and season is not None:
        media_id += f":{season}"
        if episode is not None:
            media_id += f":{episode}"
    try:
        parse_media_id(type, media_id)
    except ValueError:
        return {"sources": []}

    config = config_check(b64config)
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

    usenet_enabled = (
        config.get("schemaVersion") == 2 and "usenet" in config["enabledTransports"]
    )
    if not debrid_entries and not usenet_enabled:
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

    stremio_response = await get_streams(
        request=request,
        media_type=type,
        media_id=media_id,
        background_tasks=background_tasks,
        b64config=b64config,
        chilllink=True,
    )

    sources = []
    for stream in stremio_response["streams"]:
        if "url" not in stream or "_chilllink" not in stream:
            # Client-delegated infoHash/nzbUrl streams are not valid ChillLink
            # server URLs and remain available on their native Stremio path.
            continue
        behavior_hints = stream["behaviorHints"]
        sources.append(
            {
                "id": behavior_hints["bingeGroup"],
                "title": behavior_hints["filename"],
                "url": stream["url"],
                "metadata": stream["_chilllink"],
            }
        )

    return {"sources": sources}
