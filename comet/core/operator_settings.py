"""Shared operator settings bootstrap and revision persistence."""

from __future__ import annotations

import asyncio
import contextvars
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
from databases import Database
from dotenv import dotenv_values
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import run_schema_migrations

BOOTSTRAP_SETTING_KEYS = frozenset(
    {
        "DATABASE_TYPE",
        "DATABASE_URL",
        "DATABASE_PATH",
        "DATABASE_FORCE_IPV4_RESOLUTION",
    }
)
LOGGING_SETTING_KEYS = frozenset({"LOG_PROFILE", "LOG_FORMAT", "NO_COLOR"})
_EFFECTIVE_SETTINGS_ENV = "COMET_EFFECTIVE_SETTINGS_JSON"
_RUNTIME_INSTANCE_ENV = "COMET_RUNTIME_INSTANCE_ID"
_RUNTIME_RESTART_MARKER_ENV = "COMET_RUNTIME_RESTART_MARKER"
_MAX_EFFECTIVE_SETTINGS_BYTES = 2 * 1024 * 1024
_POSTGRES_MIGRATION_LOCK_ID = 0x434F4D4554534554
_GENERATED_SECRET_KEYS = (
    "ADMIN_DASHBOARD_PASSWORD",
    "PROXY_DEBRID_STREAM_PASSWORD",
    "COMETNET_API_KEY",
)


class BootstrapSettings(BaseSettings):
    """The deployment-owned values required to reach the shared store."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    DATABASE_TYPE: str = "sqlite"
    DATABASE_URL: str | None = None
    DATABASE_PATH: str = "data/comet.db"
    DATABASE_FORCE_IPV4_RESOLUTION: bool = False

    @field_validator("DATABASE_TYPE", mode="before")
    @classmethod
    def normalize_database_type(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("DATABASE_TYPE must be sqlite or postgresql")
        normalized = value.strip().lower()
        if normalized == "sqlite":
            return normalized
        if normalized in {
            "postgres",
            "postgresql",
            "postgresql+asyncpg",
            "pgsql",
            "psql",
        }:
            return "postgresql"
        raise ValueError("DATABASE_TYPE must be sqlite or postgresql")


class _GeneratedSecretInputs(BaseSettings):
    """Deployment inputs needed before the complete settings model is imported."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    ADMIN_DASHBOARD_PASSWORD: str | None = None
    PROXY_DEBRID_STREAM_PASSWORD: str | None = None
    COMETNET_API_KEY: str | None = None
    USENET_ENABLED: bool = False
    USENET_NATIVE_ACCESS_TOKEN: str | None = None
    COMET_CAPABILITY_SECRET: str | None = None
    CONFIGURE_PAGE_PASSWORD: str | None = None
    PUBLIC_API_TOKEN: str | None = None


@dataclass(frozen=True, slots=True)
class EffectiveSettingsPayload:
    revision: int
    values: dict[str, Any]
    dashboard_keys: frozenset[str]
    generated_keys: frozenset[str]
    deployment_keys: frozenset[str]


def _read_deployment_keys() -> frozenset[str]:
    configured = {
        key.upper()
        for key, value in dotenv_values(".env").items()
        if value not in {None, ""} or key.upper() in LOGGING_SETTING_KEYS
    }
    configured.update(
        key.upper()
        for key, value in os.environ.items()
        if value != "" or key.upper() in LOGGING_SETTING_KEYS
    )
    return frozenset(configured)


_PROCESS_DEPLOYMENT_KEYS = _read_deployment_keys()


class LiveSettings:
    """Atomically published settings with request-local generation binding."""

    __slots__ = ("_active", "_bound", "_overrides", "_publish_lock")

    def __init__(self, initial: Any):
        object.__setattr__(self, "_active", initial)
        object.__setattr__(
            self,
            "_bound",
            contextvars.ContextVar("comet_settings_generation", default=None),
        )
        object.__setattr__(self, "_publish_lock", threading.Lock())
        object.__setattr__(
            self,
            "_overrides",
            contextvars.ContextVar("comet_settings_overrides", default=None),
        )

    def snapshot(self):
        bound = self._bound.get()
        return self._active if bound is None else bound

    def active_snapshot(self):
        return self._active

    def value(self, name: str):
        overrides = self._overrides.get()
        return (
            overrides[name]
            if overrides is not None and name in overrides
            else self.snapshot().__dict__[name]
        )

    @contextmanager
    def bind(self):
        token = self._bound.set(self._active)
        try:
            yield self._bound.get()
        finally:
            self._bound.reset(token)

    @contextmanager
    def bind_snapshot(self, snapshot):
        token = self._bound.set(snapshot)
        try:
            yield snapshot
        finally:
            self._bound.reset(token)

    def publish(self, candidate: Any) -> None:
        with self._publish_lock:
            object.__setattr__(self, "_active", candidate)

    def __getattribute__(self, name: str):
        if name in {
            "_active",
            "_bound",
            "_publish_lock",
            "_overrides",
            "bind",
            "bind_snapshot",
            "active_snapshot",
            "publish",
            "snapshot",
            "value",
        }:
            return object.__getattribute__(self, name)
        current = object.__getattribute__(self, "snapshot")()
        overrides = object.__getattribute__(self, "_overrides").get()
        if overrides is not None and name in overrides:
            return overrides[name]
        if name == "__class__":
            return current.__class__
        if name == "__dict__":
            return current.__dict__
        return getattr(current, name)

    def __setattr__(self, name: str, value: Any) -> None:
        current = self._overrides.get()
        overrides = {} if current is None else current.copy()
        overrides[name] = value
        self._overrides.set(overrides)


def _database_for_bootstrap(config: BootstrapSettings) -> ReplicaAwareDatabase:
    if config.DATABASE_TYPE == "sqlite":
        path = Path(config.DATABASE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        return ReplicaAwareDatabase(Database(f"sqlite:///{path}"))

    if config.DATABASE_URL is None:
        raise ValueError("DATABASE_URL is required for PostgreSQL")
    raw_url = config.DATABASE_URL
    if "://" not in raw_url:
        raw_url = f"postgresql://{raw_url}"
    parsed = make_url(raw_url)
    if parsed.get_backend_name() != "postgresql" or not parsed.host:
        raise ValueError("DATABASE_URL must select PostgreSQL and include a host")
    parsed = parsed.set(drivername="postgresql+asyncpg")
    return ReplicaAwareDatabase(
        Database(parsed.render_as_string(hide_password=False)),
        force_ipv4=config.DATABASE_FORCE_IPV4_RESOLUTION,
    )


async def _run_bootstrap_migrations(
    database: ReplicaAwareDatabase,
    config: BootstrapSettings,
) -> None:
    if config.DATABASE_TYPE == "sqlite":
        await run_schema_migrations(
            database,
            is_sqlite=True,
            is_postgres=False,
        )
        return

    async with database.connection() as connection:
        await connection.execute(
            "SELECT pg_advisory_lock(:lock_id)",
            {"lock_id": _POSTGRES_MIGRATION_LOCK_ID},
        )
        try:
            await run_schema_migrations(
                database,
                is_sqlite=False,
                is_postgres=True,
            )
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(:lock_id)",
                {"lock_id": _POSTGRES_MIGRATION_LOCK_ID},
            )


def _decode_json_document(document: object, *, context: str) -> Any:
    if not isinstance(document, (str, bytes)):
        raise ValueError(f"{context} must be JSON")
    if len(document.encode("utf-8") if isinstance(document, str) else document) > (
        _MAX_EFFECTIVE_SETTINGS_BYTES
    ):
        raise ValueError(f"{context} is too large")
    try:
        return orjson.loads(document)
    except orjson.JSONDecodeError:
        raise ValueError(f"{context} is invalid JSON") from None


async def _load_dashboard_overrides(
    database: ReplicaAwareDatabase,
) -> tuple[int, dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT state.current_revision, setting.key, setting.value_json
        FROM operator_settings_state AS state
        LEFT JOIN operator_settings AS setting ON 1 = 1
        WHERE state.id = 1
        ORDER BY setting.key
        """,
        force_primary=True,
    )
    revision = rows[0]["current_revision"]
    values: dict[str, Any] = {}
    for row in rows:
        key = row["key"]
        if key is None:
            continue
        values[key] = orjson.loads(row["value_json"])
    return revision, values


async def _shared_secret(
    database: ReplicaAwareDatabase,
    key: str,
    *,
    byte_count: int = 32,
) -> str:
    candidate = secrets.token_urlsafe(byte_count)
    await database.execute(
        """
        INSERT INTO operator_generated_secrets (key, value, created_at)
        VALUES (:key, :value, :created_at)
        ON CONFLICT (key) DO NOTHING
        """,
        {"key": key, "value": candidate, "created_at": time.time()},
    )
    value = await database.fetch_val(
        """
        SELECT value
        FROM operator_generated_secrets
        WHERE key = :key
        """,
        {"key": key},
        force_primary=True,
    )
    return value


def _raw_effective_value(
    name: str,
    dashboard_values: dict[str, Any],
    deployment_values: _GeneratedSecretInputs,
) -> Any:
    if name in dashboard_values:
        return dashboard_values[name]
    return getattr(deployment_values, name)


def _enabled(
    name: str,
    dashboard_values: dict[str, Any],
    deployment_values: _GeneratedSecretInputs,
) -> bool:
    value = _raw_effective_value(name, dashboard_values, deployment_values)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _has_explicit_value(
    name: str,
    dashboard_values: dict[str, Any],
    deployment_values: _GeneratedSecretInputs,
) -> bool:
    value = _raw_effective_value(name, dashboard_values, deployment_values)
    return value is not None and value != ""


async def _prepare_effective_payload(
    config: BootstrapSettings,
    deployment_values: _GeneratedSecretInputs | None = None,
) -> EffectiveSettingsPayload:
    database = _database_for_bootstrap(config)
    await database.connect()
    try:
        await _run_bootstrap_migrations(database, config)
        if config.DATABASE_TYPE == "sqlite":
            await database.execute("PRAGMA journal_mode=WAL")
        return await resolve_effective_settings(
            database,
            deployment_values=(
                deployment_values or _GeneratedSecretInputs(_env_file=None)
            ),
        )
    finally:
        await database.disconnect()


async def resolve_effective_settings(
    database,
    *,
    deployment_values: _GeneratedSecretInputs | None = None,
) -> EffectiveSettingsPayload:
    """Resolve the latest durable revision through the canonical precedence."""

    revision, dashboard_values = await _load_dashboard_overrides(database)
    deployment_values = deployment_values or _GeneratedSecretInputs()
    values = dict(dashboard_values)
    generated_keys: set[str] = set()

    for key in _GENERATED_SECRET_KEYS:
        if _has_explicit_value(key, dashboard_values, deployment_values):
            continue
        values[key] = await _shared_secret(database, key)
        generated_keys.add(key)

    if _enabled("USENET_ENABLED", dashboard_values, deployment_values):
        for key in ("USENET_NATIVE_ACCESS_TOKEN", "COMET_CAPABILITY_SECRET"):
            if _has_explicit_value(key, dashboard_values, deployment_values):
                continue
            values[key] = await _shared_secret(database, key)
            generated_keys.add(key)

    if _has_explicit_value(
        "CONFIGURE_PAGE_PASSWORD",
        dashboard_values,
        deployment_values,
    ) and not _has_explicit_value(
        "PUBLIC_API_TOKEN",
        dashboard_values,
        deployment_values,
    ):
        values["PUBLIC_API_TOKEN"] = await _shared_secret(
            database,
            "PUBLIC_API_TOKEN",
        )
        generated_keys.add("PUBLIC_API_TOKEN")

    return EffectiveSettingsPayload(
        revision=revision,
        values=values,
        dashboard_keys=frozenset(dashboard_values),
        generated_keys=frozenset(generated_keys),
        deployment_keys=_PROCESS_DEPLOYMENT_KEYS,
    )


def _encode_payload(payload: EffectiveSettingsPayload) -> str:
    return orjson.dumps(
        {
            "version": 1,
            "revision": payload.revision,
            "values": payload.values,
            "dashboard_keys": sorted(payload.dashboard_keys),
            "generated_keys": sorted(payload.generated_keys),
            "deployment_keys": sorted(payload.deployment_keys),
        }
    ).decode("utf-8")


def prepare_effective_settings_environment() -> EffectiveSettingsPayload:
    """Load one shared revision before application settings are imported."""

    existing = os.environ.get(_EFFECTIVE_SETTINGS_ENV)
    if existing is not None:
        return effective_settings_payload()

    payload = asyncio.run(
        _prepare_effective_payload(
            BootstrapSettings(),
            deployment_values=_GeneratedSecretInputs(),
        )
    )
    os.environ[_EFFECTIVE_SETTINGS_ENV] = _encode_payload(payload)
    os.environ.setdefault(_RUNTIME_INSTANCE_ENV, uuid.uuid4().hex)
    os.environ.setdefault("COMET_RUNTIME_STARTED_AT", str(time.time()))
    os.environ.setdefault(
        _RUNTIME_RESTART_MARKER_ENV,
        str(
            Path(BootstrapSettings().DATABASE_PATH).parent
            / f".comet-restart-{os.environ[_RUNTIME_INSTANCE_ENV]}"
        ),
    )
    return payload


def request_runtime_restart() -> None:
    marker = Path(os.environ[_RUNTIME_RESTART_MARKER_ENV])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch(exist_ok=True)


def consume_runtime_restart_request() -> bool:
    marker_value = os.environ.get(_RUNTIME_RESTART_MARKER_ENV)
    if marker_value is None:
        return False
    marker = Path(marker_value)
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def fresh_runtime_environment() -> dict[str, str]:
    payload = effective_settings_payload()
    environment = os.environ.copy()
    for key in (
        _EFFECTIVE_SETTINGS_ENV,
        _RUNTIME_INSTANCE_ENV,
        _RUNTIME_RESTART_MARKER_ENV,
        "COMET_RUNTIME_STARTED_AT",
        "COMET_RUNTIME_SUPERVISOR_PID",
        "COMET_WEB_MASTER_PID",
        "COMET_RESOLVED_FASTAPI_WORKERS",
    ):
        environment.pop(key, None)
    if "PROMETHEUS_MULTIPROC_DIR" not in payload.deployment_keys:
        environment.pop("PROMETHEUS_MULTIPROC_DIR", None)
    return environment


def effective_settings_payload() -> EffectiveSettingsPayload:
    document = os.environ.get(_EFFECTIVE_SETTINGS_ENV)
    if document is None:
        return EffectiveSettingsPayload(
            0,
            {},
            frozenset(),
            frozenset(),
            _PROCESS_DEPLOYMENT_KEYS,
        )
    decoded = _decode_json_document(document, context="effective settings payload")
    if not isinstance(decoded, dict) or decoded.get("version") != 1:
        raise ValueError("effective settings payload has an unsupported version")
    revision = decoded.get("revision")
    values = decoded.get("values")
    dashboard_keys = decoded.get("dashboard_keys")
    generated_keys = decoded.get("generated_keys")
    deployment_keys = decoded.get("deployment_keys")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(values, dict)
        or not isinstance(dashboard_keys, list)
        or not all(isinstance(key, str) for key in dashboard_keys)
        or not isinstance(generated_keys, list)
        or not all(isinstance(key, str) for key in generated_keys)
        or not isinstance(deployment_keys, list)
        or not all(isinstance(key, str) for key in deployment_keys)
    ):
        raise ValueError("effective settings payload is malformed")
    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("effective settings payload contains an invalid key")
    return EffectiveSettingsPayload(
        revision=revision,
        values=values,
        dashboard_keys=frozenset(dashboard_keys),
        generated_keys=frozenset(generated_keys),
        deployment_keys=frozenset(deployment_keys),
    )


def deployment_setting_keys() -> frozenset[str]:
    return effective_settings_payload().deployment_keys


def effective_model_inputs(model_fields: set[str] | frozenset[str]) -> dict[str, Any]:
    payload = effective_settings_payload()
    return {key: value for key, value in payload.values.items() if key in model_fields}


def validate_effective_setting_keys(
    application_model_fields: set[str] | frozenset[str],
) -> None:
    payload = effective_settings_payload()
    allowed = set(application_model_fields) | set(LOGGING_SETTING_KEYS)
    unknown = set(payload.values).difference(allowed)
    if unknown:
        raise ValueError(f"stored operator setting is not recognized: {min(unknown)}")
