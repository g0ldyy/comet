import aiohttp

from .realdebrid import RealDebrid
from .alldebrid import AllDebrid
from .premiumize import Premiumize
from .torbox import TorBox
from .debridlink import DebridLink
from .torrent import Torrent

debrid_services = {
    "realdebrid": {"extension": "RD", "class": RealDebrid},
    "alldebrid": {"extension": "AD", "class": AllDebrid},
    "premiumize": {"extension": "PM", "class": Premiumize},
    "torbox": {"extension": "TB", "class": TorBox},
    "debridlink": {"extension": "DL", "class": DebridLink},
    "torrent": {"extension": "TORRENT", "class": Torrent},
}


def get_debrid_extension(debrid_service: str):
    return debrid_services[debrid_service]["extension"]


def get_debrid(session: aiohttp.ClientSession, config: dict, ip: str):
    debrid_service = config["debridService"]
    debrid_api_key = config["debridApiKey"]

    return debrid_services[debrid_service]["class"](session, debrid_api_key, ip)
