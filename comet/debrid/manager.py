import aiohttp

from .realdebrid import RealDebrid
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .torbox import TorBox
from .debridlink import DebridLink
from .torrent import Torrent

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
    "torrent": {
        "extension": "TORRENT",
        "cache_availability_endpoint": False,
        "class": Torrent,
    },
}


def get_debrid_extension(debrid_service: str):
    return debrid_services[debrid_service]["extension"]


def get_debrid(
    session: aiohttp.ClientSession, debrid_service: str, debrid_api_key: str, ip: str
):
    return debrid_services[debrid_service]["class"](session, debrid_api_key, ip)


async def retrieve_debrid_availability(
    session: aiohttp.ClientSession,
    debrid_service: str,
    debrid_api_key: str,
    ip: str,
    info_hashes: list,
):
    if debrid_service == "torrent":
        return []

    if debrid_services[debrid_service]["cache_availability_endpoint"]:
        return await get_debrid(
            session, debrid_service, debrid_api_key, ip
        ).get_availability(info_hashes)

    return []
