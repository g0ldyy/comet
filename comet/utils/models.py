import os
import random
import string

from typing import List, Optional
from databases import Database
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from RTN import RTN, BestRanking, SettingsModel


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    ADDON_ID: Optional[str] = "stremio.comet.fast"
    ADDON_NAME: Optional[str] = "Comet"
    FASTAPI_HOST: Optional[str] = "0.0.0.0"
    FASTAPI_PORT: Optional[int] = 8000
    FASTAPI_WORKERS: Optional[int] = 2 * (os.cpu_count() or 1)
    DASHBOARD_ADMIN_PASSWORD: Optional[str] = None
    DATABASE_TYPE: Optional[str] = "sqlite"
    DATABASE_URL: Optional[str] = "username:password@hostname:port"
    DATABASE_PATH: Optional[str] = "data/comet.db"
    CACHE_TTL: Optional[int] = 86400
    DEBRID_PROXY_URL: Optional[str] = None
    INDEXER_MANAGER_TYPE: Optional[str] = None
    INDEXER_MANAGER_URL: Optional[str] = "http://127.0.0.1:9117"
    INDEXER_MANAGER_API_KEY: Optional[str] = None
    INDEXER_MANAGER_TIMEOUT: Optional[int] = 30
    INDEXER_MANAGER_INDEXERS: List[str] = ["EXAMPLE1_CHANGETHIS", "EXAMPLE2_CHANGETHIS"]
    GET_TORRENT_TIMEOUT: Optional[int] = 5
    ZILEAN_URL: Optional[str] = None
    ZILEAN_TAKE_FIRST: Optional[int] = 500
    SCRAPE_TORRENTIO: Optional[bool] = False
    SCRAPE_MEDIAFUSION: Optional[bool] = False
    MEDIAFUSION_URL: Optional[str] = "https://mediafusion.elfhosted.com"
    CUSTOM_HEADER_HTML: Optional[str] = None
    PROXY_DEBRID_STREAM: Optional[bool] = False
    PROXY_DEBRID_STREAM_PASSWORD: Optional[str] = None
    PROXY_DEBRID_STREAM_MAX_CONNECTIONS: Optional[int] = -1
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE: Optional[str] = "realdebrid"
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY: Optional[str] = None
    TITLE_MATCH_CHECK: Optional[bool] = True

    @field_validator("DASHBOARD_ADMIN_PASSWORD")
    def set_dashboard_admin_password(cls, v, values):
        if v is None:
            return "".join(random.choices(string.ascii_letters + string.digits, k=16))
        return v

    @field_validator("INDEXER_MANAGER_TYPE")
    def set_indexer_manager_type(cls, v, values):
        if v == "None":
            return None
        return v

    @field_validator("PROXY_DEBRID_STREAM_PASSWORD")
    def set_debrid_stream_proxy_password(cls, v, values):
        if v is None:
            return "".join(random.choices(string.ascii_letters + string.digits, k=16))
        return v


settings = AppSettings()


class ConfigModel(BaseModel):
    indexers: List[str]
    languages: Optional[List[str]] = ["All"]
    resolutions: Optional[List[str]] = ["All"]
    resultFormat: Optional[List[str]] = ["All"]
    maxResults: Optional[int] = 0
    maxResultsPerResolution: Optional[int] = 0
    maxSize: Optional[float] = 0
    debridService: str
    debridApiKey: str
    debridStreamProxyPassword: Optional[str] = ""

    @field_validator("indexers")
    def check_indexers(cls, v, values):
        settings.INDEXER_MANAGER_INDEXERS = [
            indexer.replace(" ", "_").lower()
            for indexer in settings.INDEXER_MANAGER_INDEXERS
        ]  # to equal webui
        valid_indexers = [
            indexer for indexer in v if indexer in settings.INDEXER_MANAGER_INDEXERS
        ]
        # if not valid_indexers: # For only Zilean mode
        #     raise ValueError(
        #         f"At least one indexer must be from {settings.INDEXER_MANAGER_INDEXERS}"
        #     )
        return valid_indexers

    @field_validator("maxResults")
    def check_max_results(cls, v):
        if v < 0:
            v = 0
        return v
    
    @field_validator("maxResultsPerResolution")
    def check_max_results_per_resolution(cls, v):
        if v < 0:
            v = 0
        return v

    @field_validator("maxSize")
    def check_max_size(cls, v):
        if v < 0:
            v = 0
        return v

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in ["realdebrid", "alldebrid", "premiumize", "torbox", "debridlink"]:
            raise ValueError("Invalid debridService")
        return v


rtn_settings = SettingsModel()
rtn_ranking = BestRanking()

# For use anywhere
rtn = RTN(settings=rtn_settings, ranking_model=rtn_ranking)

database_url = (
    settings.DATABASE_PATH
    if settings.DATABASE_TYPE == "sqlite"
    else settings.DATABASE_URL
)
database = Database(
    f"{'sqlite' if settings.DATABASE_TYPE == 'sqlite' else 'postgresql+asyncpg'}://{'/' if settings.DATABASE_TYPE == 'sqlite' else ''}{database_url}"
)

trackers = [
    "tracker:https://tracker.gbitt.info:443/announce",
    "tracker:udp://discord.heihachi.pw:6969/announce",
    "tracker:http://tracker.corpscorp.online:80/announce",
    "tracker:udp://tracker.leechers-paradise.org:6969/announce",
    "tracker:https://tracker.renfei.net:443/announce",
    "tracker:udp://exodus.desync.com:6969/announce",
    "tracker:http://tracker.xiaoduola.xyz:6969/announce",
    "tracker:udp://ipv4.tracker.harry.lu:80/announce",
    "tracker:udp://tracker.torrent.eu.org:451/announce",
    "tracker:udp://tracker.coppersurfer.tk:6969/announce",
    "tracker:http://tracker.dmcomic.org:2710/announce",
    "tracker:http://www.genesis-sp.org:2710/announce",
    "tracker:http://t.jaekr.sh:6969/announce",
    "tracker:http://tracker.bt-hash.com:80/announce",
    "tracker:https://tracker.tamersunion.org:443/announce",
    "tracker:udp://open.stealth.si:80/announce",
    "tracker:udp://tracker.opentrackr.org:1337/announce",
    "tracker:udp://leet-tracker.moe:1337/announce",
    "tracker:udp://oh.fuuuuuck.com:6969/announce",
    "tracker:udp://tracker.bittor.pw:1337/announce",
    "tracker:udp://explodie.org:6969/announce",
    "tracker:http://finbytes.org:80/announce.php",
    "tracker:udp://tracker.dump.cl:6969/announce",
    "tracker:udp://open.free-tracker.ga:6969/announce",
    "tracker:http://tracker.gbitt.info:80/announce",
    "tracker:udp://isk.richardsw.club:6969/announce",
    "tracker:http://bt1.xxxxbt.cc:6969/announce",
    "tracker:udp://tracker.qu.ax:6969/announce",
    "tracker:udp://opentracker.io:6969/announce",
    "tracker:udp://tracker.internetwarriors.net:1337/announce",
    "tracker:udp://tracker.0x7c0.com:6969/announce",
    "tracker:udp://9.rarbg.me:2710/announce",
    "tracker:udp://tracker.pomf.se:80/announce",
    "tracker:udp://tracker.openbittorrent.com:80/announce",
    "tracker:udp://open.tracker.cl:1337/announce",
    "tracker:http://www.torrentsnipe.info:2701/announce",
    "tracker:udp://retracker01-msk-virt.corbina.net:80/announce",
    "tracker:udp://open.demonii.com:1337/announce",
    "tracker:udp://tracker-udp.gbitt.info:80/announce",
    "tracker:udp://tracker.tiny-vps.com:6969/announce",
]
