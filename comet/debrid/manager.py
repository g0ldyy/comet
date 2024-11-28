import aiohttp

from .realdebrid import RealDebrid
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .torbox import TorBox
from .debridlink import DebridLink


def getDebrid(session: aiohttp.ClientSession, config: dict, ip: str):
    debrid_service = config["debridService"]
    debrid_api_key = config["debridApiKey"]
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