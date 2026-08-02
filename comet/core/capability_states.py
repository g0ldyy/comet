"""Persisted, credential-free capability validation evidence."""

import asyncio
import hashlib
import hmac
import math
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

FRESH_TTL_SECONDS = 6 * 60 * 60
LAST_KNOWN_GOOD_TTL_SECONDS = 24 * 60 * 60
STATE_RETENTION_SECONDS = 60 * 60
_VALIDATION_TIMEOUT_SECONDS = 15.0
_VALIDATION_CONCURRENCY = 4
_REFRESH_CONCURRENCY = 2
_MAX_REFRESH_TASKS = 64
_MAX_BINDINGS = 64
_LOCK_RETRY_SECONDS = 0.05
_TERMINAL_STATES = frozenset({"auth_failed", "plan_incompatible"})
_FAILURE_STATES = frozenset(
    {"transiently_unreachable", "auth_failed", "plan_incompatible"}
)
_MAX_FINGERPRINT_CBOR_BYTES = 64 * 1024
_REFRESH_TASKS: set[asyncio.Task] = set()
_REFRESH_KEYS: set[tuple[int, str]] = set()
_REFRESH_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Semaphore
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class EffectiveCapabilityState:
    state: str
    eligible: bool
    degraded: bool
    refresh_due: bool
    error_code: str | None = None
    retry_after: float | None = None
    evidence_observed_at: float | None = None


@dataclass(frozen=True)
class CapabilityBinding:
    binding_fingerprint: str
    binding_kind: str
    schema_version: int
    validator_version: str

    def __post_init__(self) -> None:
        _validate_binding(
            self.binding_fingerprint,
            self.binding_kind,
            self.schema_version,
            self.validator_version,
        )


@dataclass(frozen=True)
class CapabilityValidationOutcome:
    state: str
    error_code: str | None = None
    retry_after: float | None = None


CapabilityValidator = Callable[
    [CapabilityBinding], Awaitable[CapabilityValidationOutcome]
]


class CapabilityStateRepository:
    def __init__(self, database):
        self._database = database

    async def effective(
        self,
        binding_fingerprint: str,
        *,
        now: float | None = None,
    ) -> EffectiveCapabilityState:
        fingerprint = _fingerprint(binding_fingerprint)
        observed_at = time.time() if now is None else _timestamp(now)
        row = await self._database.fetch_one(
            """
            SELECT state, last_success_at, observed_at, fresh_until, last_known_good_until,
                   next_refresh_at, error_code, retry_after
            FROM capability_validation_states
            WHERE binding_fingerprint = :binding_fingerprint
            """,
            {"binding_fingerprint": fingerprint},
            force_primary=True,
        )
        if row is None:
            return EffectiveCapabilityState("pending_validation", False, False, False)
        state = row["state"]
        if state in _TERMINAL_STATES:
            return EffectiveCapabilityState(
                state,
                False,
                False,
                False,
                row["error_code"],
                row["retry_after"],
                row["observed_at"],
            )
        last_success_at = row["last_success_at"]
        if (
            state not in {"valid", "transiently_unreachable"}
            or last_success_at is None
            or observed_at >= row["last_known_good_until"]
        ):
            return EffectiveCapabilityState(
                "pending_validation",
                False,
                False,
                False,
                evidence_observed_at=row["observed_at"],
            )
        refresh_due = observed_at >= (
            row["next_refresh_at"]
            if state == "transiently_unreachable"
            else row["fresh_until"]
        )
        return EffectiveCapabilityState(
            state,
            True,
            state == "transiently_unreachable",
            refresh_due,
            row["error_code"],
            row["retry_after"],
            row["observed_at"],
        )

    async def cleanup_expired(self, *, now: float | None = None) -> None:
        """Discard fingerprints after their validation evidence is no longer useful."""
        observed_at = time.time() if now is None else _timestamp(now)
        await self._database.execute(
            """
            DELETE FROM capability_validation_states
            WHERE last_known_good_until < :cutoff
            """,
            {"cutoff": observed_at - STATE_RETENTION_SECONDS},
            force_primary=True,
        )

    async def record_success(
        self,
        binding: CapabilityBinding,
        *,
        now: float | None = None,
    ) -> None:
        values = _binding_values(binding, now)
        await self._database.execute(
            """
            INSERT INTO capability_validation_states (
                binding_fingerprint, binding_kind, schema_version,
                validator_version, state, last_success_at, observed_at,
                fresh_until, last_known_good_until, next_refresh_at,
                error_code, retry_after
            ) VALUES (
                :binding_fingerprint, :binding_kind, :schema_version,
                :validator_version, 'valid', :now, :now,
                :fresh_until, :last_known_good_until, :fresh_until,
                NULL, NULL
            ) ON CONFLICT (binding_fingerprint) DO UPDATE SET
                binding_kind = excluded.binding_kind,
                schema_version = excluded.schema_version,
                validator_version = excluded.validator_version,
                state = 'valid',
                last_success_at = excluded.last_success_at,
                observed_at = excluded.observed_at,
                fresh_until = excluded.fresh_until,
                last_known_good_until = excluded.last_known_good_until,
                next_refresh_at = excluded.next_refresh_at,
                error_code = NULL,
                retry_after = NULL
            """,
            values,
            force_primary=True,
        )

    async def record_failure(
        self,
        binding: CapabilityBinding,
        state: str,
        *,
        error_code: str | None,
        retry_after: float | None = None,
        now: float | None = None,
    ) -> None:
        if state not in _FAILURE_STATES:
            raise ValueError("capability failure state is invalid")
        if error_code is not None and (
            not isinstance(error_code, str)
            or not 1 <= len(error_code) <= 64
            or not all(
                character.isascii() and (character.isalnum() or character == "_")
                for character in error_code
            )
        ):
            raise ValueError("capability error code is invalid")
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, (int, float))
            or not 0 <= retry_after <= LAST_KNOWN_GOOD_TTL_SECONDS
        ):
            raise ValueError("capability retry-after is invalid")
        values = _binding_values(binding, now)
        values.update(
            {
                "state": state,
                "error_code": error_code,
                "retry_after": retry_after,
                "next_refresh_at": values["now"] + (retry_after or 0),
            }
        )
        del values["fresh_until"], values["last_known_good_until"]
        if state == "transiently_unreachable":
            await self._record_transient_failure(values)
            return
        await self._database.execute(
            """
            INSERT INTO capability_validation_states (
                binding_fingerprint, binding_kind, schema_version,
                validator_version, state, observed_at, fresh_until,
                last_known_good_until, next_refresh_at, error_code, retry_after
            ) VALUES (
                :binding_fingerprint, :binding_kind, :schema_version,
                :validator_version, :state, :now, :now, :now,
                :next_refresh_at, :error_code, :retry_after
            ) ON CONFLICT (binding_fingerprint) DO UPDATE SET
                binding_kind = excluded.binding_kind,
                schema_version = excluded.schema_version,
                validator_version = excluded.validator_version,
                state = excluded.state,
                observed_at = excluded.observed_at,
                fresh_until = excluded.fresh_until,
                next_refresh_at = excluded.next_refresh_at,
                error_code = excluded.error_code,
                retry_after = excluded.retry_after
            """,
            values,
            force_primary=True,
        )

    async def _record_transient_failure(self, values: dict[str, object]) -> None:
        del values["state"]
        await self._database.execute(
            """
            INSERT INTO capability_validation_states (
                binding_fingerprint, binding_kind, schema_version,
                validator_version, state, observed_at, fresh_until,
                last_known_good_until, next_refresh_at, error_code, retry_after
            ) VALUES (
                :binding_fingerprint, :binding_kind, :schema_version,
                :validator_version, 'pending_validation', :now, :now, :now,
                :next_refresh_at, :error_code, :retry_after
            ) ON CONFLICT (binding_fingerprint) DO UPDATE SET
                binding_kind = excluded.binding_kind,
                schema_version = excluded.schema_version,
                validator_version = excluded.validator_version,
                state = CASE
                    WHEN capability_validation_states.last_success_at IS NOT NULL
                     AND capability_validation_states.last_known_good_until > excluded.observed_at
                    THEN 'transiently_unreachable'
                    ELSE 'pending_validation'
                END,
                observed_at = excluded.observed_at,
                fresh_until = excluded.fresh_until,
                next_refresh_at = excluded.next_refresh_at,
                error_code = excluded.error_code,
                retry_after = excluded.retry_after
            """,
            values,
            force_primary=True,
        )

    async def ensure_validated(
        self,
        bindings: Sequence[CapabilityBinding],
        validator: CapabilityValidator,
        *,
        now: float | None = None,
    ) -> dict[str, EffectiveCapabilityState]:
        """Validate only pending bindings under bounded database singleflight."""
        unique = _unique_bindings(bindings)

        semaphore = asyncio.Semaphore(_VALIDATION_CONCURRENCY)

        async def ensure_one(
            binding: CapabilityBinding,
        ) -> tuple[str, EffectiveCapabilityState]:
            async with semaphore:
                state = await self.effective(binding.binding_fingerprint, now=now)
                if state.state != "pending_validation":
                    return binding.binding_fingerprint, state
                state = await self._validate_pending(binding, validator, now=now)
                return binding.binding_fingerprint, state

        pairs = await asyncio.gather(
            *(ensure_one(binding) for binding in unique.values())
        )
        return dict(pairs)

    async def retest(
        self,
        bindings: Sequence[CapabilityBinding],
        validator: CapabilityValidator,
        *,
        now: float | None = None,
    ) -> dict[str, EffectiveCapabilityState]:
        """Explicitly replace any binding state under bounded singleflight."""
        unique = _unique_bindings(bindings)
        semaphore = asyncio.Semaphore(_VALIDATION_CONCURRENCY)

        async def retest_one(
            binding: CapabilityBinding,
        ) -> tuple[str, EffectiveCapabilityState]:
            async with semaphore:
                initial = await self.effective(
                    binding.binding_fingerprint,
                    now=now,
                )
                state = await self._validate_under_lock(
                    binding,
                    validator,
                    now=now,
                    should_validate=lambda current: (
                        current.evidence_observed_at == initial.evidence_observed_at
                    ),
                )
                return binding.binding_fingerprint, state

        pairs = await asyncio.gather(
            *(retest_one(binding) for binding in unique.values())
        )
        return dict(pairs)

    async def _validate_pending(
        self,
        binding: CapabilityBinding,
        validator: CapabilityValidator,
        *,
        now: float | None,
    ) -> EffectiveCapabilityState:
        return await self._validate_under_lock(
            binding,
            validator,
            now=now,
            should_validate=lambda state: state.state == "pending_validation",
        )

    def schedule_refresh(
        self,
        bindings: Sequence[CapabilityBinding],
        validator: CapabilityValidator,
        states: Mapping[str, EffectiveCapabilityState],
    ) -> None:
        """Schedule stale last-known-good refresh without delaying the caller."""
        loop = asyncio.get_running_loop()
        semaphore = _REFRESH_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(_REFRESH_CONCURRENCY)
            _REFRESH_SEMAPHORES[loop] = semaphore
        for binding in bindings:
            state = states[binding.binding_fingerprint]
            if not state.eligible or not state.refresh_due:
                continue
            key = (id(loop), binding.binding_fingerprint)
            if key in _REFRESH_KEYS:
                continue
            if len(_REFRESH_TASKS) >= _MAX_REFRESH_TASKS:
                break
            _REFRESH_KEYS.add(key)
            task = loop.create_task(
                self._scheduled_refresh(binding, validator, semaphore),
                name=f"capability-refresh:{binding.binding_fingerprint[:12]}",
            )
            _REFRESH_TASKS.add(task)
            task.add_done_callback(
                lambda completed, refresh_key=key: _finish_refresh_task(
                    completed, refresh_key
                )
            )

    async def _scheduled_refresh(
        self,
        binding: CapabilityBinding,
        validator: CapabilityValidator,
        semaphore: asyncio.Semaphore,
    ) -> None:
        await asyncio.sleep(0)
        async with semaphore:
            await self._validate_under_lock(
                binding,
                validator,
                now=None,
                should_validate=lambda state: state.eligible and state.refresh_due,
            )

    async def _validate_under_lock(
        self,
        binding: CapabilityBinding,
        validator: CapabilityValidator,
        *,
        now: float | None,
        should_validate: Callable[[EffectiveCapabilityState], bool],
    ) -> EffectiveCapabilityState:
        deadline = time.monotonic() + _VALIDATION_TIMEOUT_SECONDS
        async with _binding_backend_lock(binding, deadline=deadline) as acquired:
            if not acquired:
                return await self.effective(binding.binding_fingerprint, now=now)
            state = await self.effective(binding.binding_fingerprint, now=now)
            if not should_validate(state):
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return state
            try:
                outcome = await asyncio.wait_for(validator(binding), timeout=remaining)
            except TimeoutError:
                outcome = CapabilityValidationOutcome(
                    "transiently_unreachable",
                    "validation_timeout",
                    30,
                )
            await self._record_outcome(binding, outcome, now=now)
            return await self.effective(binding.binding_fingerprint, now=now)

    async def _record_outcome(
        self,
        binding: CapabilityBinding,
        outcome: CapabilityValidationOutcome,
        *,
        now: float | None,
    ) -> None:
        if outcome.state == "valid":
            if outcome.error_code is not None or outcome.retry_after is not None:
                raise ValueError(
                    "valid capability outcome cannot contain failure metadata"
                )
            await self.record_success(
                binding,
                now=now,
            )
            return
        await self.record_failure(
            binding,
            outcome.state,
            error_code=outcome.error_code,
            retry_after=outcome.retry_after,
            now=now,
        )


def _finish_refresh_task(
    task: asyncio.Task,
    key: tuple[int, str],
) -> None:
    _REFRESH_TASKS.discard(task)
    _REFRESH_KEYS.discard(key)
    if task.cancelled():
        return
    task.exception()


async def shutdown_capability_refreshes() -> None:
    """Cancel refreshes before their database and HTTP dependencies close."""
    tasks = tuple(_REFRESH_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _unique_bindings(
    bindings: Sequence[CapabilityBinding],
) -> dict[str, CapabilityBinding]:
    unique = {}
    for binding in bindings:
        previous = unique.setdefault(binding.binding_fingerprint, binding)
        if previous != binding:
            raise ValueError("capability binding metadata conflicts")
    if len(unique) > _MAX_BINDINGS:
        raise ValueError("too many capability bindings")
    return unique


@asynccontextmanager
async def _binding_backend_lock(binding: CapabilityBinding, *, deadline: float):
    from comet.core.database import backend_lock, settings

    digest = hashlib.sha256(
        b"comet-capability-validation-lock-v1\0"
        + bytes.fromhex(binding.binding_fingerprint)
    ).digest()
    postgres_lock_id = int.from_bytes(digest[:8], "big", signed=True)
    sqlite_shard = digest[8]
    while True:
        async with backend_lock(
            postgres_lock_id=postgres_lock_id,
            sqlite_lock_path=(
                f"{settings.DATABASE_PATH}.capability-validation-"
                f"{sqlite_shard:02x}.lock"
            ),
            wait_message="Waiting for capability validation lock",
            wait=False,
        ) as acquired:
            if acquired:
                yield True
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield False
            return
        await asyncio.sleep(min(_LOCK_RETRY_SECONDS, remaining))


def binding_fingerprint(
    partition_key: bytes,
    *,
    binding_kind: str,
    schema_version: int,
    normalized_endpoint_and_behavior_options: object,
    credential_fingerprint: str,
    instance_capability_version: str,
) -> str:
    """Derive the exact partitioned key without persisting its inputs."""
    if not isinstance(partition_key, bytes) or len(partition_key) != 32:
        raise ValueError("capability partition key is invalid")
    _validate_binding(
        "0" * 64,
        binding_kind,
        schema_version,
        instance_capability_version,
    )
    _fingerprint(credential_fingerprint)
    payload = deterministic_cbor(
        [
            binding_kind,
            schema_version,
            normalized_endpoint_and_behavior_options,
            credential_fingerprint,
            instance_capability_version,
        ]
    )
    return hmac.digest(
        partition_key,
        b"comet-capability-binding-v1\0" + payload,
        hashlib.sha256,
    ).hex()


def _deterministic_cbor(value: object, depth: int = 0) -> bytes:
    encoded = bytearray()
    _encode_deterministic_cbor(value, encoded, depth)
    return bytes(encoded)


def deterministic_cbor(value: object) -> bytes:
    """Encode one bounded RFC 8949 core deterministic value."""
    return _deterministic_cbor(value)


def _encode_deterministic_cbor(value: object, encoded: bytearray, depth: int) -> None:
    if depth > 16:
        raise ValueError("capability fingerprint value is too deeply nested")
    if value is None:
        chunk = b"\xf6"
    elif value is False:
        chunk = b"\xf4"
    elif value is True:
        chunk = b"\xf5"
    elif isinstance(value, int):
        if value >= 0:
            chunk = _cbor_major(0, value)
        else:
            chunk = _cbor_major(1, -1 - value)
    elif isinstance(value, bytes):
        if len(value) > _MAX_FINGERPRINT_CBOR_BYTES:
            raise ValueError("capability fingerprint bytes are too large")
        chunk = _cbor_major(2, len(value)) + value
    elif isinstance(value, str):
        if len(value) > _MAX_FINGERPRINT_CBOR_BYTES:
            raise ValueError("capability fingerprint text is too large")
        utf8 = value.encode("utf-8")
        chunk = _cbor_major(3, len(utf8)) + utf8
    elif isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ValueError("capability fingerprint array is too large")
        _append_cbor(encoded, _cbor_major(4, len(value)))
        for item in value:
            _encode_deterministic_cbor(item, encoded, depth + 1)
        return
    elif isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError("capability fingerprint map is too large")
        encoded_items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("capability fingerprint map keys must be text")
            encoded_key = _deterministic_cbor(key, depth + 1)
            encoded_items.append((encoded_key, item))
        encoded_items.sort(key=lambda item: item[0])
        _append_cbor(encoded, _cbor_major(5, len(encoded_items)))
        for encoded_key, item in encoded_items:
            _append_cbor(encoded, encoded_key)
            _encode_deterministic_cbor(item, encoded, depth + 1)
        return
    else:
        raise ValueError("capability fingerprint value type is unsupported")
    _append_cbor(encoded, chunk)


def _append_cbor(encoded: bytearray, chunk: bytes) -> None:
    if len(encoded) + len(chunk) > _MAX_FINGERPRINT_CBOR_BYTES:
        raise ValueError("capability binding options exceed the fingerprint budget")
    encoded.extend(chunk)


def _cbor_major(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value <= 0xFF:
        return bytes([(major << 5) | 24, value])
    if value <= 0xFFFF:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    if value <= 0xFFFF_FFFF:
        return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")
    if value <= 0xFFFF_FFFF_FFFF_FFFF:
        return bytes([(major << 5) | 27]) + value.to_bytes(8, "big")
    raise ValueError("capability fingerprint integer is out of range")


def _fingerprint(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("capability binding fingerprint is invalid")
    return value


def _timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("capability observation time is invalid")
    return float(value)


def _validate_binding(
    binding_fingerprint: str,
    binding_kind: str,
    schema_version: int,
    validator_version: str,
) -> None:
    if (
        not isinstance(binding_kind, str)
        or not 1 <= len(binding_kind) <= 64
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or not 1 <= schema_version <= 65_535
        or not isinstance(validator_version, str)
        or not 1 <= len(validator_version) <= 64
    ):
        raise ValueError("capability binding metadata is invalid")
    _fingerprint(binding_fingerprint)


def _binding_values(
    binding: CapabilityBinding,
    now: float | None,
) -> dict[str, object]:
    observed_at = time.time() if now is None else _timestamp(now)
    return {
        "binding_fingerprint": binding.binding_fingerprint,
        "binding_kind": binding.binding_kind,
        "schema_version": binding.schema_version,
        "validator_version": binding.validator_version,
        "now": observed_at,
        "fresh_until": observed_at + FRESH_TTL_SECONDS,
        "last_known_good_until": observed_at + LAST_KNOWN_GOOD_TTL_SECONDS,
    }
