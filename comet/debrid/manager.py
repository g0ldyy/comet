import aiohttp

from comet.utils.config import should_use_stremthru

from .realdebrid import RealDebrid
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .torbox import TorBox
from .debridlink import DebridLink
from .stremthru import StremThru


def getDebrid(session: aiohttp.ClientSession, config: dict, ip: str):
    debrid_service = config["debridService"]
    debrid_api_key = config["debridApiKey"]

    if should_use_stremthru(config):
        return StremThru(
            session=session,
            url=config["stremthruUrl"],
            debrid_service=debrid_service,
            token=debrid_api_key,
        )

    if debrid_service == "realdebrid":
        return RealDebrid(session, debrid_api_key, ip)
    elif debrid_service == "alldebrid":
        return AllDebrid(session, debrid_api_key)
    elif debrid_service == "premiumize":
        return Premiumize(session, debrid_api_key)
    elif debrid_service == "torbox":
        return TorBox(session, debrid_api_key)
    elif debrid_service == "debridlink":
        return DebridLink(session, debrid_api_key)