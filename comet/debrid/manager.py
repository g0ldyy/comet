import aiohttp

from .realdebrid import RealDebrid


def getDebrid(session: aiohttp.ClientSession, config: dict):
    if config["debridService"] == "realdebrid":
        return RealDebrid(session, config["debridApiKey"])
