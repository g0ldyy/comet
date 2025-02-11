import os
import random
import string
import RTN

from typing import List, Optional
from databases import Database
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from RTN import BestRanking, SettingsModel
from RTN.models import (
    ResolutionConfig,
    OptionsConfig,
    LanguagesConfig,
    CustomRanksConfig,
    CustomRank,
    QualityRankModel,
    RipsRankModel,
    HdrRankModel,
    AudioRankModel,
    ExtrasRankModel,
)


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    ADDON_ID: Optional[str] = "stremio.comet.fast"
    ADDON_NAME: Optional[str] = "Comet"
    FASTAPI_HOST: Optional[str] = "0.0.0.0"
    FASTAPI_PORT: Optional[int] = 8000
    FASTAPI_WORKERS: Optional[int] = 2 * (os.cpu_count() or 1)
    DASHBOARD_ADMIN_PASSWORD: Optional[str] = "".join(
        random.choices(string.ascii_letters + string.digits, k=16)
    )
    DATABASE_TYPE: Optional[str] = "sqlite"
    DATABASE_URL: Optional[str] = "username:password@hostname:port"
    DATABASE_PATH: Optional[str] = "data/comet.db"
    CACHE_TTL: Optional[int] = 86400
    DEBRID_PROXY_URL: Optional[str] = None
    INDEXER_MANAGER_TYPE: Optional[str] = None
    INDEXER_MANAGER_URL: Optional[str] = "http://127.0.0.1:9117"
    INDEXER_MANAGER_API_KEY: Optional[str] = None
    INDEXER_MANAGER_TIMEOUT: Optional[int] = 30
    INDEXER_MANAGER_INDEXERS: List[str] = []
    GET_TORRENT_TIMEOUT: Optional[int] = 5
    DOWNLOAD_TORRENTS: Optional[bool] = True
    SCRAPE_MEDIAFUSION: Optional[bool] = False
    SCRAPE_ZILEAN: Optional[bool] = False
    ZILEAN_URL: Optional[str] = "https://zilean.elfhosted.com"
    SCRAPE_TORRENTIO: Optional[bool] = False
    TORRENTIO_URL: Optional[str] = "https://torrentio.strem.fun"
    SCRAPE_MEDIAFUSION: Optional[bool] = False
    MEDIAFUSION_URL: Optional[str] = "https://mediafusion.elfhosted.com"
    CUSTOM_HEADER_HTML: Optional[str] = None
    PROXY_DEBRID_STREAM: Optional[bool] = False
    PROXY_DEBRID_STREAM_PASSWORD: Optional[str] = "".join(
        random.choices(string.ascii_letters + string.digits, k=16)
    )
    PROXY_DEBRID_STREAM_MAX_CONNECTIONS: Optional[int] = -1
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE: Optional[str] = "realdebrid"
    PROXY_DEBRID_STREAM_DEBRID_DEFAULT_APIKEY: Optional[str] = None
    STREMTHRU_URL: Optional[str] = (
        "https://stremthru.13377001.xyz"  # 403 issues with https://stremthru.elfhosted.com
    )
    REMOVE_ADULT_CONTENT: Optional[bool] = False

    @field_validator("INDEXER_MANAGER_TYPE")
    def set_indexer_manager_type(cls, v, values):
        if v == "None":
            return None
        return v

    @field_validator("INDEXER_MANAGER_INDEXERS")
    def indexer_manager_indexers_normalization(cls, v, values):
        v = [indexer.replace(" ", "_").lower() for indexer in v]  # to equal webui
        return v


settings = AppSettings()


class CometSettingsModel(SettingsModel):
    model_config = SettingsConfigDict()

    resolutions: ResolutionConfig = ResolutionConfig(
        r2160p=True, r480p=True, r360p=True
    )

    options: OptionsConfig = OptionsConfig(
        remove_ranks_under=-10000000000, allow_english_in_languages=True
    )

    languages: LanguagesConfig = LanguagesConfig(exclude=[])

    custom_ranks: CustomRanksConfig = CustomRanksConfig(
        quality=QualityRankModel(
            av1=CustomRank(fetch=True),
            dvd=CustomRank(fetch=True),
            mpeg=CustomRank(fetch=True),
            remux=CustomRank(fetch=True),
            vhs=CustomRank(fetch=True),
            webmux=CustomRank(fetch=True),
            xvid=CustomRank(fetch=True),
        ),
        rips=RipsRankModel(
            bdrip=CustomRank(fetch=True),
            dvdrip=CustomRank(fetch=True),
            ppvrip=CustomRank(fetch=True),
            satrip=CustomRank(fetch=True),
            tvrip=CustomRank(fetch=True),
            uhdrip=CustomRank(fetch=True),
            vhsrip=CustomRank(fetch=True),
            webdlrip=CustomRank(fetch=True),
        ),
        hdr=HdrRankModel(
            dolby_vision=CustomRank(fetch=True),
        ),
        audio=AudioRankModel(
            mono=CustomRank(fetch=True),
            mp3=CustomRank(fetch=True),
        ),
        extras=ExtrasRankModel(
            three_d=CustomRank(fetch=True),
            converted=CustomRank(fetch=True),
            documentary=CustomRank(fetch=True),
            site=CustomRank(fetch=True),
            upscaled=CustomRank(fetch=True),
        ),
    )


rtn_settings_default = CometSettingsModel()
rtn_ranking_default = BestRanking()


class ConfigModel(BaseModel):
    # languages: Optional[List[str]] = ["all"]
    # resolutions: Optional[List[str]] = ["all"]
    cachedOnly: Optional[bool] = False
    removeTrash: Optional[bool] = True
    resultFormat: Optional[List[str]] = ["all"]
    maxResultsPerResolution: Optional[int] = 0
    maxSize: Optional[float] = 0
    debridService: Optional[str] = "torrent"
    debridApiKey: Optional[str] = ""
    debridStreamProxyPassword: Optional[str] = ""
    rtnSettings: Optional[CometSettingsModel] = rtn_settings_default
    rtnRanking: Optional[BestRanking] = rtn_ranking_default

    @field_validator("maxResultsPerResolution")
    def check_max_results_per_resolution(cls, v):
        if not isinstance(v, int):
            v = 0

        if v < 0:
            v = 0
        return v

    @field_validator("maxSize")
    def check_max_size(cls, v):
        if not isinstance(v, float):
            v = 0

        if v < 0:
            v = 0
        return v

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in [
            "realdebrid",
            "alldebrid",
            "premiumize",
            "torbox",
            "easydebrid",
            "debridlink",
            "offcloud",
            "pikpak",
            "torrent",
        ]:
            raise ValueError("Invalid debridService")
        return v


default_config = ConfigModel().model_dump()
# default_config["rtnSettings"] = SettingsModel(**default_config["rtnSettings"])
# default_config["rtnRanking"] = BestRanking(**default_config["rtnRanking"])


# Web Config Initialization
# languages = [language for language in PTT.parse.LANGUAGES_TRANSLATION_TABLE.values()]
# languages.insert(0, "Unknown")
# languages.insert(1, "Multi")
web_config = {
    # "languages": languages,
    "resolutions": [resolution.value for resolution in RTN.models.Resolution],
    "resultFormat": ["title", "metadata", "seeders", "size", "tracker", "languages"],
}

database_url = (
    settings.DATABASE_PATH
    if settings.DATABASE_TYPE == "sqlite"
    else settings.DATABASE_URL
)
database = Database(
    f"{'sqlite' if settings.DATABASE_TYPE == 'sqlite' else 'postgresql+asyncpg'}://{'/' if settings.DATABASE_TYPE == 'sqlite' else ''}{database_url}"
)

trackers = [
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://tracker.0x7c0.com:6969/announce",
    "udp://opentracker.io:6969/announce",
    "udp://leet-tracker.moe:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://tracker.leechers-paradise.org:6969/announce",
    "udp://tracker.pomf.se:80/announce",
    "udp://9.rarbg.me:2710/announce",
    "http://tracker.gbitt.info:80/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://open.free-tracker.ga:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://isk.richardsw.club:6969/announce",
    "https://tracker.gbitt.info:443/announce",
    "udp://tracker.coppersurfer.tk:6969/announce",
    "udp://oh.fuuuuuck.com:6969/announce",
    "udp://ipv4.tracker.harry.lu:80/announce",
    "udp://open.demonii.com:1337/announce",
    "https://tracker.tamersunion.org:443/announce",
    "https://tracker.renfei.net:443/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.dump.cl:6969/announce",
]
