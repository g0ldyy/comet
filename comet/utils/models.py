import os

from typing import List, Optional
from databases import Database
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from RTN import RTN, BaseRankingModel, SettingsModel


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    ADDON_ID: str = "stremio.comet.fast"
    ADDON_NAME: str = "Comet"
    FASTAPI_HOST: str = "0.0.0.0"
    FASTAPI_PORT: int = 8000
    FASTAPI_WORKERS: int = 2 * (os.cpu_count() or 1)
    DATABASE_PATH: str = "data/comet.db"
    CACHE_TTL: int = 86400
    DEBRID_PROXY_URL: Optional[str] = None
    INDEXER_MANAGER_TYPE: str = "jackett"
    INDEXER_MANAGER_URL: str = "http://127.0.0.1:9117"
    INDEXER_MANAGER_API_KEY: str = "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    INDEXER_MANAGER_TIMEOUT: int = 30
    INDEXER_MANAGER_INDEXERS: List[str] = ["EXAMPLE1_CHANGETHIS", "EXAMPLE2_CHANGETHIS"]
    GET_TORRENT_TIMEOUT: int = 5
    ZILEAN_URL: Optional[str] = None
    CUSTOM_HEADER_HTML: Optional[str] = None


settings = AppSettings()


class ConfigModel(BaseModel):
    indexers: List[str]
    languages: Optional[List[str]] = ["All"]
    resolutions: Optional[List[str]] = ["All"]
    maxResults: Optional[int] = 0
    filterTitles: Optional[bool] = True
    debridService: str
    debridApiKey: str

    @field_validator("indexers")
    def check_indexers(cls, v, values):
        if not any(indexer in settings.INDEXER_MANAGER_INDEXERS for indexer in v):
            raise ValueError(
                f"At least one indexer must be from {settings.INDEXER_MANAGER_INDEXERS}"
            )
        return v

    @field_validator("maxResults")
    def check_max_results(cls, v):
        if v < 0:
            raise ValueError("maxResults cannot be less than 0")
        return v

    @field_validator("debridService")
    def check_debrid_service(cls, v):
        if v not in ["realdebrid", "realdebrid"]:
            raise ValueError("Invalid debridService")
        return v


class BestOverallRanking(BaseRankingModel):
    uhd: int = 100
    fhd: int = 90
    hd: int = 80
    sd: int = 70
    dolby_video: int = 100
    hdr: int = 80
    hdr10: int = 90
    dts_x: int = 100
    dts_hd: int = 80
    dts_hd_ma: int = 90
    atmos: int = 90
    truehd: int = 60
    ddplus: int = 40
    aac: int = 30
    ac3: int = 20
    remux: int = 150
    bluray: int = 120
    webdl: int = 90


rtn_settings = SettingsModel()
rtn_ranking = BestOverallRanking()

# For use anywhere
rtn = RTN(settings=rtn_settings, ranking_model=rtn_ranking)
database = Database(f"sqlite:///{settings.DATABASE_PATH}")
