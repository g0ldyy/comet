from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_FASTAPI_WORKERS = 64


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    FASTAPI_HOST: str = "0.0.0.0"
    FASTAPI_PORT: int = 8000
    FASTAPI_WORKERS: int = 1
    USE_GUNICORN: bool = True
    GUNICORN_PRELOAD_APP: bool = True
