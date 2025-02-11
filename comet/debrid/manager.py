import aiohttp

from comet.utils.models import settings

from .realdebrid import RealDebrid
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .torbox import TorBox
from .debridlink import DebridLink
from .torrent import Torrent
from .stremthru import StremThru
from .easydebrid import EasyDebrid
from .offcloud import Offcloud
from .pikpak import PikPak

debrid_services = {
    "realdebrid": {
        "extension": "RD",
        "cache_availability_endpoint": False,
        "class": RealDebrid,
    },
    "alldebrid": {
        "extension": "AD",
        "cache_availability_endpoint": False,
        "class": AllDebrid,
    },
    "premiumize": {
        "extension": "PM",
        "cache_availability_endpoint": True,
        "class": Premiumize,
    },
    "torbox": {"extension": "TB", "cache_availability_endpoint": True, "class": TorBox},
    "debridlink": {
        "extension": "DL",
        "cache_availability_endpoint": False,
        "class": DebridLink,
    },
    "stremthru": {
        "extension": "ST",
        "cache_availability_endpoint": True,
        "class": StremThru,
    },
    "easydebrid": {
        "extension": "ED",
        "cache_availability_endpoint": True,
        "class": EasyDebrid,
    },
    "offcloud": {
        "extension": "OC",
        "cache_availability_endpoint": False,
        "class": Offcloud,
    },
    "pikpak": {
        "extension": "PP",
        "cache_availability_endpoint": False,
        "class": PikPak,
    },
    "torrent": {
        "extension": "TORRENT",
        "cache_availability_endpoint": False,
        "class": Torrent,
    },
}


def get_actual_debrid_service(debrid_service: str, debrid_api_key: str):
    if debrid_service == "stremthru":
        return debrid_api_key.split(":")[0].lower()
    return debrid_service


def get_debrid_extension(debrid_service: str, debrid_api_key: str):
    original_extension = debrid_services[debrid_service]["extension"]
    if debrid_service == "stremthru":
        return f"{original_extension}-{debrid_services[get_actual_debrid_service(debrid_service, debrid_api_key)]['extension']}"

    return original_extension


def build_stremthru_token(debrid_service: str, debrid_api_key: str):
    return f"{debrid_service}:{debrid_api_key}"


def get_debrid(
    session: aiohttp.ClientSession,
    video_id: str,
    stremthru_url: str,
    debrid_service: str,
    debrid_api_key: str,
    ip: str,
):
    if settings.FORCE_STREMTHRU and debrid_service != "torrent":
        return debrid_services["stremthru"]["class"](
            session,
            video_id,
            stremthru_url,
            build_stremthru_token(debrid_service, debrid_api_key),
            ip,
        )

    if debrid_service == "stremthru":
        return debrid_services[debrid_service]["class"](
            session, video_id, stremthru_url, debrid_api_key, ip
        )

    return debrid_services[debrid_service]["class"](
        session, video_id, debrid_api_key, ip
    )


async def retrieve_debrid_availability(
    session: aiohttp.ClientSession,
    video_id: str,
    stremthru_url: str,
    debrid_service: str,
    debrid_api_key: str,
    ip: str,
    info_hashes: list,
):
    if debrid_service == "torrent":
        return []

    if (
        settings.FORCE_STREMTHRU
        or debrid_services[debrid_service]["cache_availability_endpoint"]
    ):
        return await get_debrid(
            session, video_id, stremthru_url, debrid_service, debrid_api_key, ip
        ).get_availability(info_hashes)

    return []
