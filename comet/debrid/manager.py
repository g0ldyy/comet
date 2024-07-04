import aiohttp

from .realdebrid import RealDebrid


def getDebrid(session: aiohttp.ClientSession, config: dict):
    debrid_service = config["debridService"]
    debrid_api_key = config["debridApiKey"]
    if debrid_service == "realdebrid":
        return RealDebrid(session, debrid_api_key)