from typing import List

from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    JACKETT_URL: str
    JACKETT_KEY: str
    GET_TORRENT_TIMEOUT: int

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"
        validate_assignment = True

class ConfigModel(BaseModel):
    debridService: str
    debridApiKey: str
    indexers: List[str]
    maxResults: int

    class Config:
        json_schema_extra = {
            "example": {
                "debridService": "RealDebrid",
                "debridApiKey": "your_api_key_here",
                "indexers": ["indexer1", "indexer2"],
                "maxResults": 10
            }
        }

video_extensions: tuple = (
    ".mkv", ".mp4", ".avi", ".mov", ".flv", ".wmv", ".webm", ".mpg", ".mpeg", 
    ".m4v", ".3gp", ".3g2", ".ogv", ".ogg", ".drc", ".gif", ".gifv", ".mng", 
    ".avi", ".mov", ".qt", ".wmv", ".yuv", ".rm", ".rmvb", ".asf", ".amv", 
    ".m4p", ".mp2", ".mpe", ".mpv", ".m2v", ".svi", ".mxf", ".roq", ".nsv", 
    ".f4v", ".f4p", ".f4a", ".f4b"
)

settings = Settings()
