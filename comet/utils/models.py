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
    REMOVE_ADULT_CONTENT: Optional[bool] = False

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
        if not isinstance(v, int):
            v = 0

        if v < 0:
            v = 0
        return v

    @field_validator("maxResultsPerResolution")
    def check_max_results_per_resolution(cls, v):
        if not isinstance(v, int):
            v = 0

        if v < 0:
            v = 0
        return v

    @field_validator("maxSize")
    def check_max_size(cls, v):
        if not isinstance(v, int):
            v = 0

        if v < 0:
            v = 0
        return v

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in ["realdebrid", "alldebrid", "premiumize", "torbox", "debridlink"]:
            raise ValueError("Invalid debridService")
        return v


default_settings = {
    "profile": "default",
    "require": [],
    "exclude": [],
    "preferred": [],
    "resolutions": {
        "r2160p": True,
        "r1080p": True,
        "r720p": True,
        "r480p": True,
        "r360p": True,
        "unknown": True,
    },
    "options": {
        "title_similarity": 0.85,
        "remove_all_trash": True,
        "remove_ranks_under": -1000000000000000,
        "remove_unknown_languages": False,
        "allow_english_in_languages": True,
        "enable_fetch_speed_mode": True,
        "remove_adult_content": settings.REMOVE_ADULT_CONTENT,
    },
    "languages": {
        "required": [],
        "exclude": [
            # "ar",
            # "hi",
            # "fr",
            # "es",
            # "de",
            # "ru",
            # "pt",
            # "it"
        ],
        "preferred": [],
    },
    "custom_ranks": {
        "quality": {
            "av1": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "avc": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "bluray": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dvd": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "hdtv": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "hevc": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "mpeg": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "remux": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "vhs": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "web": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "webdl": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "webmux": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "xvid": {"fetch": True, "use_custom_rank": False, "rank": 0},
        },
        "rips": {
            "bdrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "brrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dvdrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "hdrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "ppvrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "satrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "tvrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "uhdrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "vhsrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "webdlrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "webrip": {"fetch": True, "use_custom_rank": False, "rank": 0},
        },
        "hdr": {
            "bit10": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dolby_vision": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "hdr": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "hdr10plus": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "sdr": {"fetch": True, "use_custom_rank": False, "rank": 0},
        },
        "audio": {
            "aac": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "ac3": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "atmos": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dolby_digital": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dolby_digital_plus": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dts_lossy": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dts_lossless": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "eac3": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "flac": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "mono": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "mp3": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "stereo": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "surround": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "Truehd": {"fetch": True, "use_custom_rank": False, "rank": 0},
        },
        "extras": {
            "three_d": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "converted": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "documentary": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "dubbed": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "edition": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "hardcoded": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "network": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "proper": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "repack": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "retail": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "site": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "subbed": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "upscaled": {"fetch": True, "use_custom_rank": False, "rank": 0},
            "scene": {"fetch": True, "use_custom_rank": False, "rank": 0},
        },
        "trash": {
            "cam": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "clean_audio": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "pdtv": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "r5": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "screener": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "size": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "telecine": {"fetch": False, "use_custom_rank": False, "rank": 0},
            "telesync": {"fetch": False, "use_custom_rank": False, "rank": 0},
        },
    },
}
rtn_settings = SettingsModel(**default_settings)
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
