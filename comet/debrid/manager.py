import aiohttp

from .realdebrid import RealDebrid
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .torbox import TorBox
from .debridlink import DebridLink
from .torrent import Torrent

debrid_services = {
    "realdebrid": {"debrid_extension": "RD", "class": RealDebrid},
    "alldebrid": {"debrid_extension": "AD", "class": AllDebrid},
    "premiumize": {"debrid_extension": "PM", "class": Premiumize},
    "torbox": {"debrid_extension": "TB", "class": TorBox},
    "debridlink": {"debrid_extension": "DL", "class": DebridLink},
    "torrent": {"debrid_extension": "TORRENT", "class": Torrent},
}


def get_debrid_extension(debrid_service: str):
    return debrid_services[debrid_service]["debrid_extension"]


def get_debrid(session: aiohttp.ClientSession, config: dict, ip: str):
    debrid_service = config["debridService"]
    debrid_api_key = config["debridApiKey"]

    return debrid_services[debrid_service]["class"](session, debrid_api_key, ip)
