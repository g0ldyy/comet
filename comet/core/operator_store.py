"""Atomic operator-setting revisions and secret-safe audit records."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import orjson

from comet.core.operator_settings import (
    BOOTSTRAP_SETTING_KEYS,
)
from comet.core.settings_policy import restart_required


@dataclass(frozen=True, slots=True)
class SettingsMutationResult:
    revision: int
    changed_keys: tuple[str, ...]
    restart_required: bool


def _json_value(value: Any) -> str:
    try:
        return orjson.dumps(value).decode("utf-8")
    except (TypeError, orjson.JSONEncodeError):
        raise ValueError("operator setting value is not JSON serializable") from None


class OperatorSettingsStore:
    def __init__(self, database):
        self._database = database
        self._save_lock = asyncio.Lock()
        self._is_postgres = str(database.url).startswith(
            ("postgres://", "postgresql://", "postgresql+")
        )

    async def load_overrides(self) -> dict[str, Any]:
        rows = await self._database.fetch_all(
            """
            SELECT key, value_json
            FROM operator_settings
            ORDER BY key
            """,
            force_primary=True,
        )
        overrides: dict[str, Any] = {}
        for row in rows:
            overrides[row["key"]] = orjson.loads(row["value_json"])
        return overrides

    async def current_revision(self) -> int:
        revision = await self._database.fetch_val(
            """
            SELECT current_revision
            FROM operator_settings_state
            WHERE id = 1
            """,
            force_primary=True,
        )
        return revision

    async def _locked_revision(self) -> int:
        suffix = " FOR UPDATE" if self._is_postgres else ""
        revision = await self._database.fetch_val(
            f"""
            SELECT current_revision
            FROM operator_settings_state
            WHERE id = 1{suffix}
            """,
            force_primary=True,
        )
        return revision

    async def record_access(
        self,
        *,
        key: str,
        action: Literal["reveal", "session_invalidate"],
        actor: str,
    ) -> None:
        await self._database.execute(
            """
            INSERT INTO operator_settings_audit (
                id, revision, key, action, previous_source,
                next_source, changed_at, changed_by
            ) VALUES (
                :id, NULL, :key, :action, NULL,
                NULL, :changed_at, :changed_by
            )
            """,
            {
                "id": uuid.uuid4().hex,
                "key": key,
                "action": action,
                "changed_at": time.time(),
                "changed_by": actor,
            },
        )

    async def save(
        self,
        updates: dict[str, Any],
        *,
        reset_keys: set[str] | frozenset[str] = frozenset(),
        actor: str,
    ) -> SettingsMutationResult:
        from comet.core.models import AppSettings
        from comet.observability.logging import LoggingSettings

        reset = set(reset_keys)
        overlap = set(updates).intersection(reset)
        if overlap:
            raise ValueError(f"setting cannot be updated and reset: {min(overlap)}")
        requested = set(updates) | reset
        bootstrap = requested.intersection(BOOTSTRAP_SETTING_KEYS)
        if bootstrap:
            raise ValueError(f"bootstrap setting is deployment-owned: {min(bootstrap)}")
        allowed = {key for key in AppSettings.model_fields if key.isupper()} | set(
            LoggingSettings.model_fields
        )
        unknown = requested.difference(allowed)
        if unknown:
            raise ValueError(f"operator setting is not recognized: {min(unknown)}")

        async with self._save_lock, self._database.transaction():
            current_revision = await self._locked_revision()
            current = await self.load_overrides()
            proposed = dict(current)
            proposed.update(updates)
            for key in reset:
                proposed.pop(key, None)

            app_keys = set(AppSettings.model_fields)
            logging_keys = set(LoggingSettings.model_fields)
            application = AppSettings(
                **{key: value for key, value in proposed.items() if key in app_keys}
            )
            logging = LoggingSettings(
                **{key: value for key, value in proposed.items() if key in logging_keys}
            )

            normalized_updates: dict[str, Any] = {}
            for key in updates:
                if key in app_keys:
                    normalized_updates[key] = getattr(application, key)
                else:
                    normalized_updates[key] = getattr(logging, key)

            changed = sorted(
                key
                for key in requested
                if (
                    (key in reset and key in current)
                    or (
                        key in normalized_updates
                        and current.get(key, object()) != normalized_updates[key]
                    )
                )
            )
            if not changed:
                return SettingsMutationResult(
                    revision=current_revision,
                    changed_keys=(),
                    restart_required=False,
                )

            now = time.time()
            changed_json = _json_value(changed)
            revision = current_revision + 1
            await self._database.execute(
                """
                UPDATE operator_settings_state
                SET current_revision = :revision
                WHERE id = 1 AND current_revision = :current_revision
                """,
                {
                    "revision": revision,
                    "current_revision": current_revision,
                },
            )
            await self._database.execute(
                """
                INSERT INTO operator_settings_revisions (
                    revision, created_at, created_by, changed_keys_json
                ) VALUES (
                    :revision, :created_at, :created_by, :changed_keys_json
                )
                """,
                {
                    "revision": revision,
                    "created_at": now,
                    "created_by": actor,
                    "changed_keys_json": changed_json,
                },
            )

            for key in changed:
                previous_source = (
                    "dashboard"
                    if key in current
                    else "environment"
                    if key in os.environ
                    else "default"
                )
                if key in reset:
                    await self._database.execute(
                        "DELETE FROM operator_settings WHERE key = :key",
                        {"key": key},
                    )
                    next_source = "environment" if key in os.environ else "default"
                    action = "reset"
                else:
                    await self._database.execute(
                        """
                        INSERT INTO operator_settings (
                            key, value_json, revision, updated_at, updated_by
                        ) VALUES (
                            :key, :value_json, :revision, :updated_at, :updated_by
                        )
                        ON CONFLICT (key) DO UPDATE SET
                            value_json = excluded.value_json,
                            revision = excluded.revision,
                            updated_at = excluded.updated_at,
                            updated_by = excluded.updated_by
                        """,
                        {
                            "key": key,
                            "value_json": _json_value(normalized_updates[key]),
                            "revision": revision,
                            "updated_at": now,
                            "updated_by": actor,
                        },
                    )
                    next_source = "dashboard"
                    action = "set"

                await self._database.execute(
                    """
                    INSERT INTO operator_settings_audit (
                        id, revision, key, action, previous_source,
                        next_source, changed_at, changed_by
                    ) VALUES (
                        :id, :revision, :key, :action, :previous_source,
                        :next_source, :changed_at, :changed_by
                    )
                    """,
                    {
                        "id": uuid.uuid4().hex,
                        "revision": revision,
                        "key": key,
                        "action": action,
                        "previous_source": previous_source,
                        "next_source": next_source,
                        "changed_at": now,
                        "changed_by": actor,
                    },
                )

        return SettingsMutationResult(
            revision=revision,
            changed_keys=tuple(changed),
            restart_required=restart_required(changed),
        )
