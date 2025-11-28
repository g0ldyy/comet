import aiohttp

from .stremthru import StremThru

debrid_services = {
    "realdebrid": {"extension": "RD"},
    "alldebrid": {"extension": "AD"},
    "premiumize": {"extension": "PM"},
    "torbox": {"extension": "TB"},
    "debridlink": {"extension": "DL"},
    "stremthru": {"extension": "ST"},
    "debrider": {"extension": "DB"},
    "easydebrid": {"extension": "ED"},
    "offcloud": {"extension": "OC"},
    "pikpak": {"extension": "PP"},
    "torrent": {"extension": "TORRENT"},
}


def get_debrid_extension(debrid_service: str):
    return debrid_services[debrid_service]["extension"]


def build_stremthru_token(debrid_service: str, debrid_api_key: str):
    return f"{debrid_service}:{debrid_api_key}"


def get_debrid(
    session: aiohttp.ClientSession,
    video_id: str,
    media_only_id: str,
    debrid_service: str,
    debrid_api_key: str,
    ip: str,
):
    if debrid_service != "torrent":
        return StremThru(
            session,
            video_id,
            media_only_id,
            build_stremthru_token(debrid_service, debrid_api_key),
            ip,
        )


async def retrieve_debrid_availability(
    session: aiohttp.ClientSession,
    video_id: str,
    media_only_id: str,
    debrid_service: str,
    debrid_api_key: str,
    ip: str,
    info_hashes: list,
    seeders_map: dict,
    tracker_map: dict,
    sources_map: dict,
):
    if debrid_service == "torrent":
        return []

    return await get_debrid(
        session, video_id, media_only_id, debrid_service, debrid_api_key, ip
    ).get_availability(info_hashes, seeders_map, tracker_map, sources_map)
