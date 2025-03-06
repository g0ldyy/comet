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
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    ADDON_ID: Optional[str] = "stremio.comet.fast"
    ADDON_NAME: Optional[str] = "Comet"
    FASTAPI_HOST: Optional[str] = "0.0.0.0"
    FASTAPI_PORT: Optional[int] = 8000
    FASTAPI_WORKERS: Optional[int] = 1
    USE_GUNICORN: Optional[bool] = True
    DASHBOARD_ADMIN_PASSWORD: Optional[str] = "".join(
        random.choices(string.ascii_letters + string.digits, k=16)
    )
    DATABASE_TYPE: Optional[str] = "sqlite"
    DATABASE_URL: Optional[str] = "username:password@hostname:port"
    DATABASE_PATH: Optional[str] = "data/comet.db"
    METADATA_CACHE_TTL: Optional[int] = 2592000  # 30 days
    TORRENT_CACHE_TTL: Optional[int] = 1296000  # 15 days
    DEBRID_CACHE_TTL: Optional[int] = 86400  # 1 day
    DEBRID_PROXY_URL: Optional[str] = None
    INDEXER_MANAGER_TYPE: Optional[str] = None
    INDEXER_MANAGER_URL: Optional[str] = "http://127.0.0.1:9117"
    INDEXER_MANAGER_API_KEY: Optional[str] = None
    INDEXER_MANAGER_TIMEOUT: Optional[int] = 30
    INDEXER_MANAGER_INDEXERS: List[str] = []
    GET_TORRENT_TIMEOUT: Optional[int] = 5
    DOWNLOAD_TORRENT_FILES: Optional[bool] = False
    SCRAPE_COMET: Optional[bool] = False
    COMET_URL: Optional[str] = "https://comet.elfhosted.com"
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
    STREMTHRU_URL: Optional[str] = "https://stremthru.13377001.xyz"
    REMOVE_ADULT_CONTENT: Optional[bool] = False

    @field_validator(
        "INDEXER_MANAGER_URL",
        "ZILEAN_URL",
        "TORRENTIO_URL",
        "MEDIAFUSION_URL",
        "COMET_URL",
        "STREMTHRU_URL",
    )
    def remove_trailing_slash(cls, v):
        if v and v.endswith("/"):
            return v[:-1]
        return v

    @field_validator("INDEXER_MANAGER_TYPE")
    def set_indexer_manager_type(cls, v, values):
        if v is not None and v.lower() == "none":
            return None
        return v

    @field_validator("INDEXER_MANAGER_INDEXERS")
    def indexer_manager_indexers_normalization(cls, v, values):
        v = [indexer.replace(" ", "").lower() for indexer in v]
        return v


settings = AppSettings()


class CometSettingsModel(SettingsModel):
    model_config = SettingsConfigDict()

    resolutions: ResolutionConfig = ResolutionConfig(
        r2160p=True, r480p=True, r360p=True
    )

    options: OptionsConfig = OptionsConfig(remove_ranks_under=-10000000000)

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
rtn_settings_default_dumped = rtn_settings_default.model_dump()
# {
#     "profile":"default",
#     "require":[

#     ],
#     "exclude":[

#     ],
#     "preferred":[

#     ],
#     "resolutions":{
#         "r2160p":true,
#         "r1080p":true,
#         "r720p":true,
#         "r480p":true,
#         "r360p":true,
#         "unknown":true
#     },
#     "options":{
#         "title_similarity":0.85,
#         "remove_all_trash":true,
#         "remove_ranks_under":-10000000000,
#         "remove_unknown_languages":false,
#         "allow_english_in_languages":false,
#         "enable_fetch_speed_mode":true,
#         "remove_adult_content":true
#     },
#     "languages":{
#         "required":[

#         ],
#         "exclude":[

#         ],
#         "preferred":[

#         ]
#     },
#     "custom_ranks":{
#         "quality":{
#             "av1":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "avc":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "bluray":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dvd":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdtv":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hevc":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "mpeg":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "remux":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "vhs":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "web":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webdl":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webmux":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "xvid":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "rips":{
#             "bdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "brrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dvdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "ppvrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "satrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "tvrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "uhdrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "vhsrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webdlrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "webrip":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "hdr":{
#             "bit10":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dolby_vision":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdr":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hdr10plus":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "sdr":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "audio":{
#             "aac":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "ac3":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "atmos":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dolby_digital":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dolby_digital_plus":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dts_lossy":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dts_lossless":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "eac3":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "flac":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "mono":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "mp3":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "stereo":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "surround":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "truehd":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "extras":{
#             "three_d":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "converted":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "documentary":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "dubbed":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "edition":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "hardcoded":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "network":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "proper":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "repack":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "retail":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "site":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "subbed":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "upscaled":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "scene":{
#                 "fetch":true,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         },
#         "trash":{
#             "cam":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "clean_audio":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "pdtv":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "r5":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "screener":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "size":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "telecine":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             },
#             "telesync":{
#                 "fetch":false,
#                 "use_custom_rank":false,
#                 "rank":0
#             }
#         }
#     }
# }
rtn_ranking_default = BestRanking()


class ConfigModel(BaseModel):
    cachedOnly: Optional[bool] = False
    removeTrash: Optional[bool] = True
    resultFormat: Optional[List[str]] = ["all"]
    maxResultsPerResolution: Optional[int] = 0
    maxSize: Optional[float] = 0
    debridService: Optional[str] = "torrent"
    debridApiKey: Optional[str] = ""
    debridStreamProxyPassword: Optional[str] = ""
    languages: Optional[dict] = rtn_settings_default_dumped["languages"]
    resolutions: Optional[dict] = rtn_settings_default_dumped["resolutions"]
    options: Optional[dict] = rtn_settings_default_dumped["options"]
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
default_config["rtnSettings"] = rtn_settings_default
default_config["rtnRanking"] = rtn_ranking_default


web_config = {
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
