import ipaddress
import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_FASTAPI_WORKERS = 64
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


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

    @field_validator("FASTAPI_HOST")
    @classmethod
    def validate_fastapi_host(cls, value):
        if (
            type(value) is not str
            or not value
            or len(value) > 253
            or not value.isascii()
            or value != value.strip()
            or any(character.isspace() or ord(character) < 33 for character in value)
        ):
            raise ValueError("FASTAPI_HOST must be a bounded IP address or hostname")
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            pass
        hostname = value.removesuffix(".")
        if not hostname or any(
            _HOST_LABEL.fullmatch(label) is None for label in hostname.split(".")
        ):
            raise ValueError("FASTAPI_HOST must be a bounded IP address or hostname")
        return value

    @field_validator("FASTAPI_PORT", mode="before")
    @classmethod
    def reject_boolean_fastapi_port(cls, value):
        if isinstance(value, bool):
            raise ValueError("FASTAPI_PORT cannot be a boolean")
        return value

    @field_validator("FASTAPI_PORT")
    @classmethod
    def validate_fastapi_port(cls, value):
        if not 1 <= value <= 65_535:
            raise ValueError("FASTAPI_PORT must be between 1 and 65535")
        return value

    @field_validator("FASTAPI_WORKERS", mode="before")
    @classmethod
    def validate_fastapi_workers(cls, value):
        if isinstance(value, bool):
            raise ValueError("FASTAPI_WORKERS cannot be a boolean")
        return value

    @field_validator("FASTAPI_WORKERS")
    @classmethod
    def bound_fastapi_workers(cls, value):
        if not 0 <= value <= MAX_FASTAPI_WORKERS:
            raise ValueError(
                f"FASTAPI_WORKERS must be between 0 and {MAX_FASTAPI_WORKERS}"
            )
        return value
