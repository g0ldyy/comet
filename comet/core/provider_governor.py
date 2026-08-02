"""Database-coordinated limits for bounded external-provider calls."""

import math
import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowPermit:
    used: int
    limit: int
    reset_at_ms: int


@dataclass
class ProviderLease:
    lease_id: str
    scope_key: str
    operation: str
    slot: int
    expires_at_ms: int
    _database: object
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        await self._database.execute(
            """
            DELETE FROM provider_governor_leases
            WHERE lease_id = :lease_id
              AND scope_key = :scope_key
              AND operation = :operation
              AND slot = :slot
            """,
            {
                "lease_id": self.lease_id,
                "scope_key": self.scope_key,
                "operation": self.operation,
                "slot": self.slot,
            },
            force_primary=True,
        )
        self._released = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.release()


class ProviderGovernor:
    def __init__(self, database):
        self._database = database

    async def acquire_window(
        self,
        scope_key: bytes,
        operation: str,
        *,
        limit: int,
        window_seconds: float,
        priority: str = "interactive",
        interactive_reserve: int = 0,
        now: float | None = None,
    ) -> WindowPermit | None:
        scope = _scope(scope_key)
        operation = _operation(operation)
        limit = _positive_integer(limit, "provider window limit", maximum=1_000_000)
        reserve = _nonnegative_integer(
            interactive_reserve,
            "provider interactive reserve",
            maximum=limit,
        )
        if priority not in {"interactive", "background"}:
            raise ValueError("provider priority is invalid")
        duration_ms = _duration_ms(
            window_seconds,
            "provider window duration",
            maximum_seconds=31 * 24 * 60 * 60,
        )
        now_ms = _now_ms(now)
        window_start_ms = now_ms - (now_ms % duration_ms)
        admission_limit = limit if priority == "interactive" else limit - reserve
        if admission_limit <= 0:
            return None
        row = await self._database.fetch_one(
            """
            INSERT INTO provider_governor_windows (
                scope_key, operation, window_start_ms, window_duration_ms,
                limit_count, used_count, updated_at_ms, expires_at_ms
            ) VALUES (
                :scope_key, :operation, :window_start_ms, :window_duration_ms,
                :limit_count, 1, :now_ms, :expires_at_ms
            ) ON CONFLICT (
                scope_key, operation, window_start_ms, window_duration_ms
            ) DO UPDATE SET
                limit_count = CASE
                    WHEN excluded.limit_count <
                         provider_governor_windows.limit_count
                    THEN excluded.limit_count
                    ELSE provider_governor_windows.limit_count
                END,
                used_count = provider_governor_windows.used_count + 1,
                updated_at_ms = excluded.updated_at_ms,
                expires_at_ms = excluded.expires_at_ms
            WHERE provider_governor_windows.used_count < CASE
                WHEN provider_governor_windows.limit_count < :admission_limit
                THEN provider_governor_windows.limit_count
                ELSE :admission_limit
            END
            RETURNING used_count, limit_count
            """,
            {
                "scope_key": scope,
                "operation": operation,
                "window_start_ms": window_start_ms,
                "window_duration_ms": duration_ms,
                "limit_count": limit,
                "admission_limit": admission_limit,
                "now_ms": now_ms,
                "expires_at_ms": window_start_ms + 2 * duration_ms,
            },
            force_primary=True,
        )
        if row is None:
            return None
        return WindowPermit(
            row["used_count"],
            row["limit_count"],
            window_start_ms + duration_ms,
        )

    async def tighten_window(
        self,
        scope_key: bytes,
        operation: str,
        *,
        limit: int,
        window_seconds: float,
        now: float | None = None,
    ) -> bool:
        scope = _scope(scope_key)
        operation = _operation(operation)
        limit = _positive_integer(limit, "provider window limit", maximum=1_000_000)
        duration_ms = _duration_ms(
            window_seconds,
            "provider window duration",
            maximum_seconds=31 * 24 * 60 * 60,
        )
        now_ms = _now_ms(now)
        window_start_ms = now_ms - (now_ms % duration_ms)
        row = await self._database.fetch_one(
            """
            UPDATE provider_governor_windows
            SET limit_count = :limit_count,
                updated_at_ms = :now_ms
            WHERE scope_key = :scope_key
              AND operation = :operation
              AND window_start_ms = :window_start_ms
              AND window_duration_ms = :window_duration_ms
              AND limit_count > :limit_count
            RETURNING limit_count
            """,
            {
                "scope_key": scope,
                "operation": operation,
                "window_start_ms": window_start_ms,
                "window_duration_ms": duration_ms,
                "limit_count": limit,
                "now_ms": now_ms,
            },
            force_primary=True,
        )
        return row is not None

    async def acquire_concurrency(
        self,
        scope_key: bytes,
        operation: str,
        *,
        limit: int,
        owner_request_id: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> ProviderLease | None:
        scope = _scope(scope_key)
        operation = _operation(operation)
        limit = _positive_integer(
            limit,
            "provider concurrency limit",
            maximum=256,
        )
        owner_request_id = _bounded_text(
            owner_request_id,
            "provider request identifier",
            128,
        )
        lease_duration_ms = _duration_ms(
            lease_seconds,
            "provider lease duration",
            maximum_seconds=60 * 60,
        )
        now_ms = _now_ms(now)
        expires_at_ms = now_ms + lease_duration_ms
        for slot in range(limit):
            lease_id = str(uuid.uuid4())
            row = await self._database.fetch_one(
                """
                INSERT INTO provider_governor_leases (
                    lease_id, scope_key, operation, slot,
                    owner_request_id, acquired_at_ms, expires_at_ms
                ) VALUES (
                    :lease_id, :scope_key, :operation, :slot,
                    :owner_request_id, :now_ms, :expires_at_ms
                ) ON CONFLICT (scope_key, operation, slot) DO UPDATE SET
                    lease_id = excluded.lease_id,
                    owner_request_id = excluded.owner_request_id,
                    acquired_at_ms = excluded.acquired_at_ms,
                    expires_at_ms = excluded.expires_at_ms
                WHERE provider_governor_leases.expires_at_ms <= :now_ms
                RETURNING lease_id
                """,
                {
                    "lease_id": lease_id,
                    "scope_key": scope,
                    "operation": operation,
                    "slot": slot,
                    "owner_request_id": owner_request_id,
                    "now_ms": now_ms,
                    "expires_at_ms": expires_at_ms,
                },
                force_primary=True,
            )
            if row is not None:
                return ProviderLease(
                    row["lease_id"],
                    scope,
                    operation,
                    slot,
                    expires_at_ms,
                    self._database,
                )
        return None

    async def collect_expired(
        self,
        *,
        now: float | None = None,
        batch_size: int = 1_000,
    ) -> tuple[int, int]:
        now_ms = _now_ms(now)
        batch_size = _positive_integer(
            batch_size,
            "provider governor GC batch",
            maximum=10_000,
        )
        leases = await self._database.fetch_all(
            """
            DELETE FROM provider_governor_leases
            WHERE lease_id IN (
                SELECT lease_id
                FROM provider_governor_leases
                WHERE expires_at_ms <= :now_ms
                ORDER BY expires_at_ms, lease_id
                LIMIT :batch_size
            )
              AND expires_at_ms <= :now_ms
            RETURNING lease_id
            """,
            {"now_ms": now_ms, "batch_size": batch_size},
            force_primary=True,
        )
        remaining = batch_size - len(leases)
        windows = (
            await self._database.fetch_all(
                """
                DELETE FROM provider_governor_windows
                WHERE (
                    scope_key, operation, window_start_ms, window_duration_ms
                ) IN (
                    SELECT scope_key, operation, window_start_ms,
                           window_duration_ms
                    FROM provider_governor_windows
                    WHERE expires_at_ms <= :now_ms
                    ORDER BY expires_at_ms, scope_key, operation
                    LIMIT :batch_size
                )
                  AND expires_at_ms <= :now_ms
                RETURNING scope_key
                """,
                {"now_ms": now_ms, "batch_size": remaining},
                force_primary=True,
            )
            if remaining
            else ()
        )
        return len(leases), len(windows)


def _scope(value: bytes) -> str:
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError("provider governor scope must contain 32 bytes")
    return value.hex()


def _operation(value: str) -> str:
    value = _bounded_text(value, "provider operation", 64)
    if not all(
        character.isascii() and (character.isalnum() or character in {"_", "-", ":"})
        for character in value
    ):
        raise ValueError("provider operation is invalid")
    return value


def _bounded_text(value: str, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _positive_integer(value: int, field: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _nonnegative_integer(value: int, field: str, *, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= maximum
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _duration_ms(value: float, field: str, *, maximum_seconds: float) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum_seconds
    ):
        raise ValueError(f"{field} is invalid")
    return math.ceil(value * 1_000)


def _now_ms(value: float | None) -> int:
    timestamp = time.time() if value is None else value
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
        or timestamp < 0
    ):
        raise ValueError("provider governor timestamp is invalid")
    return int(timestamp * 1_000)
