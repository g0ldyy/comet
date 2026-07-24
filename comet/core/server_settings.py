from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="allow"
    )

    FASTAPI_HOST: str | None = "0.0.0.0"
    FASTAPI_PORT: int | None = 8000
    FASTAPI_WORKERS: int | None = 1
    USE_GUNICORN: bool | None = True
    GUNICORN_PRELOAD_APP: bool | None = True
