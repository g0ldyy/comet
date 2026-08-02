import asyncio
import unittest
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from databases import Database

import comet.core.capability_states as capability_states_module
from comet.core.capability_states import (
    FRESH_TTL_SECONDS,
    LAST_KNOWN_GOOD_TTL_SECONDS,
    STATE_RETENTION_SECONDS,
    CapabilityBinding,
    CapabilityStateRepository,
    CapabilityValidationOutcome,
    EffectiveCapabilityState,
    _deterministic_cbor,
    binding_fingerprint,
    shutdown_capability_refreshes,
)
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.schema_migrations import run_schema_migrations
from comet.core.schema_specs import CAPABILITY_VALIDATION_STATES_TABLE_SPEC


class CapabilityStateDatabaseTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real SQL. The mocked tests below cannot catch a bind-parameter mismatch."""

    @staticmethod
    @asynccontextmanager
    async def _acquired_lock(*args, **kwargs):
        yield True

    async def asyncSetUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temporary_directory.name}/states.db")
        )
        await self.database.connect()
        await run_schema_migrations(self.database, is_sqlite=True, is_postgres=False)
        self.repository = CapabilityStateRepository(self.database)

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temporary_directory.cleanup()

    async def test_every_failure_state_is_persisted_against_a_real_database(self):
        for index, state in enumerate(
            sorted({"transiently_unreachable", "auth_failed", "plan_incompatible"})
        ):
            fingerprint = f"{index:x}" * 64
            await self.repository.record_failure(
                CapabilityBinding(fingerprint, "nzbdav", 2, "nzbdav-v1"),
                state,
                error_code="provider_timeout",
                retry_after=30,
                now=100,
            )
            effective = await self.repository.effective(fingerprint, now=101)
            self.assertIn(
                effective.state,
                {state, "pending_validation"},
                f"{state} was not persisted",
            )

    async def test_success_then_failure_round_trips_against_a_real_database(self):
        fingerprint = "a" * 64
        await self.repository.record_success(
            CapabilityBinding(fingerprint, "nzbdav", 2, "nzbdav-v1"),
            now=100,
        )
        self.assertEqual(
            (await self.repository.effective(fingerprint, now=101)).state, "valid"
        )

        await self.repository.record_failure(
            CapabilityBinding(fingerprint, "nzbdav", 2, "nzbdav-v1"),
            "auth_failed",
            error_code="credentials_rejected",
            retry_after=None,
            now=200,
        )

        effective = await self.repository.effective(fingerprint, now=201)
        self.assertEqual(effective.state, "auth_failed")
        self.assertFalse(effective.eligible)

    async def test_cleanup_reclaims_only_expired_fingerprints_in_a_real_database(self):
        await self.repository.record_failure(
            CapabilityBinding("a" * 64, "nzbdav", 2, "nzbdav-v1"),
            "auth_failed",
            error_code="credentials_rejected",
            now=100,
        )
        await self.repository.record_success(
            CapabilityBinding("b" * 64, "nzbdav", 2, "nzbdav-v1"),
            now=100,
        )

        await self.repository.cleanup_expired(now=100 + STATE_RETENTION_SECONDS + 1)

        rows = await self.database.fetch_all(
            """
            SELECT binding_fingerprint
            FROM capability_validation_states
            ORDER BY binding_fingerprint
            """,
            force_primary=True,
        )
        self.assertEqual(
            [row["binding_fingerprint"] for row in rows],
            ["b" * 64],
        )

    async def test_unexpected_validator_failure_propagates(self):
        binding = CapabilityBinding(
            binding_fingerprint="c" * 64,
            binding_kind="nzbdav",
            schema_version=2,
            validator_version="nzbdav-v1",
        )

        async def unreachable(_binding):
            raise RuntimeError("upstream is down")

        with patch.object(
            capability_states_module, "_binding_backend_lock", self._acquired_lock
        ):
            with self.assertRaisesRegex(RuntimeError, "upstream is down"):
                await self.repository.ensure_validated([binding], unreachable, now=100)


class CapabilityStateRepositoryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    @asynccontextmanager
    async def _acquired_lock(*args, **kwargs):
        yield True

    async def test_success_uses_the_fixed_fresh_and_last_known_good_ttls(self):
        database = type("Database", (), {"execute": AsyncMock()})()

        await CapabilityStateRepository(database).record_success(
            CapabilityBinding("a" * 64, "easynews", 2, "easynews-v1"),
            now=100,
        )

        values = database.execute.await_args.args[1]
        self.assertEqual(values["fresh_until"], 100 + FRESH_TTL_SECONDS)
        self.assertEqual(
            values["last_known_good_until"],
            100 + LAST_KNOWN_GOOD_TTL_SECONDS,
        )
        self.assertNotIn("credential", values)

    async def test_cleanup_discards_only_evidence_past_its_retention_window(self):
        database = type("Database", (), {"execute": AsyncMock()})()

        await CapabilityStateRepository(database).cleanup_expired(now=10_000)

        sql, values = database.execute.await_args.args
        self.assertIn("last_known_good_until < :cutoff", sql)
        self.assertEqual(values["cutoff"], 10_000 - STATE_RETENTION_SECONDS)
        self.assertTrue(database.execute.await_args.kwargs["force_primary"])

    async def test_effective_state_refreshes_without_delaying_last_known_good(self):
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(
            return_value={
                "state": "valid",
                "last_success_at": 100,
                "observed_at": 100,
                "fresh_until": 100 + FRESH_TTL_SECONDS,
                "last_known_good_until": 100 + LAST_KNOWN_GOOD_TTL_SECONDS,
                "next_refresh_at": 100 + FRESH_TTL_SECONDS,
                "error_code": None,
                "retry_after": None,
            }
        )
        repository = CapabilityStateRepository(database)

        fresh = await repository.effective("a" * 64, now=101)
        stale = await repository.effective("a" * 64, now=101 + FRESH_TTL_SECONDS)
        expired = await repository.effective(
            "a" * 64, now=100 + LAST_KNOWN_GOOD_TTL_SECONDS
        )

        self.assertTrue(fresh.eligible)
        self.assertFalse(fresh.refresh_due)
        self.assertTrue(stale.eligible)
        self.assertTrue(stale.refresh_due)
        self.assertEqual(expired.state, "pending_validation")
        self.assertFalse(expired.eligible)

    async def test_transient_failure_preserves_success_only_inside_lkg_window(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)

        await repository.record_failure(
            CapabilityBinding("a" * 64, "nzbdav", 2, "nzbdav-v1"),
            "transiently_unreachable",
            error_code="provider_timeout",
            retry_after=30,
            now=100,
        )

        sql, values = database.execute.await_args.args
        self.assertIn("last_known_good_until > excluded.observed_at", sql)
        self.assertNotIn("last_success_at =", sql)
        self.assertEqual(values["next_refresh_at"], 130)

    async def test_transient_failure_honors_retry_after_before_refresh(self):
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(
            return_value={
                "state": "transiently_unreachable",
                "last_success_at": 100,
                "observed_at": 200,
                "fresh_until": 200,
                "last_known_good_until": 100 + LAST_KNOWN_GOOD_TTL_SECONDS,
                "next_refresh_at": 230,
                "error_code": "provider_timeout",
                "retry_after": 30,
            }
        )
        repository = CapabilityStateRepository(database)

        cooling_down = await repository.effective("a" * 64, now=229)
        retry_due = await repository.effective("a" * 64, now=230)

        self.assertTrue(cooling_down.eligible)
        self.assertTrue(cooling_down.degraded)
        self.assertFalse(cooling_down.refresh_due)
        self.assertEqual(cooling_down.retry_after, 30)
        self.assertTrue(retry_due.eligible)
        self.assertTrue(retry_due.refresh_due)

    async def test_terminal_failure_is_immediately_ineligible(self):
        database = type("Database", (), {})()
        database.fetch_one = AsyncMock(
            return_value={
                "state": "auth_failed",
                "last_success_at": 1,
                "observed_at": 2,
                "fresh_until": 2,
                "last_known_good_until": 1000,
                "next_refresh_at": 2,
                "error_code": "authentication_failed",
                "retry_after": None,
            }
        )

        state = await CapabilityStateRepository(database).effective("a" * 64, now=3)

        self.assertEqual(state.state, "auth_failed")
        self.assertFalse(state.eligible)
        self.assertEqual(state.error_code, "authentication_failed")

    async def test_first_use_validation_coalesces_duplicate_pending_bindings(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)
        pending = EffectiveCapabilityState("pending_validation", False, False, False)
        valid = EffectiveCapabilityState("valid", True, False, False)
        repository.effective = AsyncMock(side_effect=[pending, pending, valid])
        validator = AsyncMock(return_value=CapabilityValidationOutcome("valid"))
        binding = CapabilityBinding(
            "a" * 64,
            "easynews",
            2,
            "easynews-v1",
        )

        with patch(
            "comet.core.capability_states._binding_backend_lock",
            new=self._acquired_lock,
        ):
            states = await repository.ensure_validated(
                [binding, binding], validator, now=100
            )

        validator.assert_awaited_once_with(binding)
        self.assertEqual(states, {"a" * 64: valid})
        self.assertIn("'valid'", database.execute.await_args.args[0])

    async def test_first_use_keeps_terminal_state_without_network_validation(self):
        database = type("Database", (), {})()
        repository = CapabilityStateRepository(database)
        terminal = EffectiveCapabilityState(
            "auth_failed",
            False,
            False,
            False,
            "authentication_failed",
        )
        repository.effective = AsyncMock(return_value=terminal)
        validator = AsyncMock()

        states = await repository.ensure_validated(
            [CapabilityBinding("a" * 64, "easynews", 2, "easynews-v1")],
            validator,
            now=100,
        )

        validator.assert_not_awaited()
        self.assertEqual(states["a" * 64], terminal)

    async def test_first_use_timeout_records_only_a_safe_transient_failure(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)
        pending = EffectiveCapabilityState("pending_validation", False, False, False)
        repository.effective = AsyncMock(return_value=pending)

        async def blocked_validator(binding):
            await asyncio.sleep(1)

        with (
            patch(
                "comet.core.capability_states._binding_backend_lock",
                new=self._acquired_lock,
            ),
            patch(
                "comet.core.capability_states._VALIDATION_TIMEOUT_SECONDS",
                0.001,
            ),
        ):
            await repository.ensure_validated(
                [CapabilityBinding("a" * 64, "easynews", 2, "easynews-v1")],
                blocked_validator,
                now=100,
            )

        sql, values = database.execute.await_args.args
        self.assertIn("'pending_validation'", sql)
        self.assertIn("last_known_good_until > excluded.observed_at", sql)
        self.assertNotIn("state", values)
        self.assertEqual(values["error_code"], "validation_timeout")
        self.assertNotIn("exception", values)

    async def test_first_use_rejects_conflicting_duplicate_metadata(self):
        repository = CapabilityStateRepository(object())
        with self.assertRaisesRegex(ValueError, "metadata conflicts"):
            await repository.ensure_validated(
                [
                    CapabilityBinding("a" * 64, "easynews", 2, "easynews-v1"),
                    CapabilityBinding("a" * 64, "nzbdav", 2, "nzbdav-v1"),
                ],
                AsyncMock(),
            )

    async def test_explicit_retest_replaces_a_terminal_state(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)
        terminal = EffectiveCapabilityState(
            "auth_failed",
            False,
            False,
            False,
            "authentication_failed",
        )
        valid = EffectiveCapabilityState("valid", True, False, False)
        repository.effective = AsyncMock(side_effect=[terminal, terminal, valid])
        validator = AsyncMock(return_value=CapabilityValidationOutcome("valid"))
        binding = CapabilityBinding(
            "a" * 64,
            "easynews",
            2,
            "easynews-v1",
        )

        with patch(
            "comet.core.capability_states._binding_backend_lock",
            new=self._acquired_lock,
        ):
            states = await repository.retest([binding], validator, now=100)

        validator.assert_awaited_once_with(binding)
        self.assertEqual(states[binding.binding_fingerprint], valid)
        self.assertIn("'valid'", database.execute.await_args.args[0])

    async def test_stale_refresh_is_non_blocking_and_duplicate_suppressed(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)
        stale = EffectiveCapabilityState("valid", True, False, True)
        fresh = EffectiveCapabilityState("valid", True, False, False)
        repository.effective = AsyncMock(side_effect=[stale, fresh])
        started = asyncio.Event()
        release = asyncio.Event()

        async def validator(binding):
            started.set()
            await release.wait()
            return CapabilityValidationOutcome("valid")

        binding = CapabilityBinding(
            "a" * 64,
            "easynews",
            2,
            "easynews-v1",
        )
        with patch(
            "comet.core.capability_states._binding_backend_lock",
            new=self._acquired_lock,
        ):
            repository.schedule_refresh(
                [binding, binding],
                validator,
                {binding.binding_fingerprint: stale},
            )
            self.assertFalse(started.is_set())
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertEqual(
                len(capability_states_module._REFRESH_TASKS),
                1,
            )
            release.set()
            await asyncio.gather(*tuple(capability_states_module._REFRESH_TASKS))

        self.assertEqual(database.execute.await_count, 1)
        self.assertFalse(capability_states_module._REFRESH_TASKS)

    async def test_stale_refresh_concurrency_is_bounded(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)
        stale = EffectiveCapabilityState("valid", True, False, True)
        fresh = EffectiveCapabilityState("valid", True, False, False)
        observations = {}

        async def effective(fingerprint, *, now=None):
            count = observations.get(fingerprint, 0)
            observations[fingerprint] = count + 1
            return stale if count == 0 else fresh

        repository.effective = effective
        active = 0
        maximum_active = 0
        two_started = asyncio.Event()
        release = asyncio.Event()

        async def validator(binding):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1
            return CapabilityValidationOutcome("valid")

        bindings = [
            CapabilityBinding(
                character * 64,
                "easynews",
                2,
                "easynews-v1",
            )
            for character in "abc"
        ]
        with patch(
            "comet.core.capability_states._binding_backend_lock",
            new=self._acquired_lock,
        ):
            repository.schedule_refresh(
                bindings,
                validator,
                {binding.binding_fingerprint: stale for binding in bindings},
            )
            await two_started.wait()
            release.set()
            await asyncio.gather(*tuple(capability_states_module._REFRESH_TASKS))

        self.assertEqual(maximum_active, 2)
        self.assertFalse(capability_states_module._REFRESH_TASKS)

    async def test_shutdown_cancels_refreshes_before_dependencies_close(self):
        database = type("Database", (), {"execute": AsyncMock()})()
        repository = CapabilityStateRepository(database)
        stale = EffectiveCapabilityState("valid", True, False, True)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def effective(_fingerprint, *, now=None):
            return stale

        async def validator(_binding):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        repository.effective = effective
        binding = CapabilityBinding(
            "a" * 64,
            "easynews",
            2,
            "easynews-v1",
        )
        with patch(
            "comet.core.capability_states._binding_backend_lock",
            new=self._acquired_lock,
        ):
            repository.schedule_refresh(
                [binding],
                validator,
                {binding.binding_fingerprint: stale},
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            await shutdown_capability_refreshes()

        self.assertTrue(cancelled.is_set())
        self.assertFalse(capability_states_module._REFRESH_TASKS)
        self.assertFalse(capability_states_module._REFRESH_KEYS)

    async def test_failed_refresh_is_reported_without_leaking_task_warnings(self):
        secret = "credential=must-not-be-logged"
        task = asyncio.create_task(
            self._raise_refresh_error(secret),
            name="capability-refresh:test",
        )
        await asyncio.gather(task, return_exceptions=True)
        capability_states_module._finish_refresh_task(task, (id(task), "a" * 64))

    @staticmethod
    async def _raise_refresh_error(message):
        raise RuntimeError(message)

    def test_schema_is_closed_and_never_stores_endpoints_or_credentials(self):
        schema = CAPABILITY_VALIDATION_STATES_TABLE_SPEC.create_sql
        self.assertIn("binding_fingerprint CHAR(64) PRIMARY KEY", schema)
        self.assertIn("'pending_validation'", schema)
        self.assertIn("'plan_incompatible'", schema)
        self.assertNotIn("'disabled'", schema)
        self.assertNotIn("endpoint", schema)
        self.assertNotIn("credential", schema)

    def test_observation_times_must_be_finite(self):
        repository = CapabilityStateRepository(object())
        for value in (float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "observation time"),
            ):
                asyncio.run(repository.effective("a" * 64, now=value))

    def test_binding_fingerprint_is_partitioned_deterministic_cbor(self):
        options = {
            "range_required": True,
            "endpoint": "https://example.test",
        }

        first = binding_fingerprint(
            b"a" * 32,
            binding_kind="easynews",
            schema_version=2,
            normalized_endpoint_and_behavior_options=options,
            credential_fingerprint="b" * 64,
            instance_capability_version="easynews-v1",
        )
        reordered = binding_fingerprint(
            b"a" * 32,
            binding_kind="easynews",
            schema_version=2,
            normalized_endpoint_and_behavior_options=dict(reversed(options.items())),
            credential_fingerprint="b" * 64,
            instance_capability_version="easynews-v1",
        )
        other_partition = binding_fingerprint(
            b"c" * 32,
            binding_kind="easynews",
            schema_version=2,
            normalized_endpoint_and_behavior_options=options,
            credential_fingerprint="b" * 64,
            instance_capability_version="easynews-v1",
        )

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, other_partition)
        self.assertEqual(
            _deterministic_cbor([1, "a", None]).hex(),
            "83016161f6",
        )

    def test_binding_fingerprint_rejects_unbounded_types(self):
        with self.assertRaisesRegex(ValueError, "type is unsupported"):
            binding_fingerprint(
                b"a" * 32,
                binding_kind="easynews",
                schema_version=2,
                normalized_endpoint_and_behavior_options={"ratio": 1.5},
                credential_fingerprint="b" * 64,
                instance_capability_version="easynews-v1",
            )
        with self.assertRaisesRegex(ValueError, "fingerprint budget"):
            binding_fingerprint(
                b"a" * 32,
                binding_kind="easynews",
                schema_version=2,
                normalized_endpoint_and_behavior_options=["x" * 40_000] * 2,
                credential_fingerprint="b" * 64,
                instance_capability_version="easynews-v1",
            )
