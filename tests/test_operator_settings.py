import asyncio
import os
import sqlite3
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from databases import Database

from comet.core.db_router import ReplicaAwareDatabase
from comet.core.live_settings import prepare_settings_application
from comet.core.models import AppSettings, settings
from comet.core.operator_settings import (
    BootstrapSettings,
    _encode_payload,
    _GeneratedSecretInputs,
    _prepare_effective_payload,
    _shared_secret,
    deployment_setting_keys,
    fresh_runtime_environment,
)
from comet.core.operator_store import OperatorSettingsStore
from comet.core.runtime_registry import (
    RuntimeIdentity,
    RuntimeRegistry,
    restart_target_pid,
)
from comet.core.schema_migrations import run_schema_migrations
from comet.core.settings_catalog import build_settings_catalog


class OperatorSettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temp_dir.name}/operator.db")
        )
        await self.database.connect()
        await run_schema_migrations(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        self.store = OperatorSettingsStore(self.database)

    async def asyncTearDown(self):
        await self.database.disconnect()
        self.temp_dir.cleanup()

    async def test_control_plane_migration_owns_all_shared_tables(self):
        rows = await self.database.fetch_all(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        )
        names = {row["name"] for row in rows}
        self.assertTrue(
            {
                "operator_settings",
                "operator_settings_state",
                "operator_settings_revisions",
                "operator_settings_audit",
                "operator_generated_secrets",
                "operator_session_revocations",
                "runtime_instances",
                "runtime_processes",
            }.issubset(names)
        )

    async def test_generated_secret_race_converges_on_one_shared_value(self):
        values = await asyncio.gather(
            *(
                _shared_secret(self.database, "ADMIN_DASHBOARD_PASSWORD")
                for _ in range(32)
            )
        )
        self.assertEqual(len(set(values)), 1)

    async def test_revision_save_is_atomic_and_audit_contains_no_values(self):
        secret = "admin-secret-that-must-not-enter-audit"
        result = await self.store.save(
            {
                "ADMIN_DASHBOARD_PASSWORD": secret,
                "HTTP_CLIENT_LIMIT": 77,
            },
            actor="admin",
        )
        self.assertEqual(result.revision, 1)
        self.assertEqual(
            result.changed_keys,
            ("ADMIN_DASHBOARD_PASSWORD", "HTTP_CLIENT_LIMIT"),
        )
        self.assertFalse(result.restart_required)

        stored = await self.store.load_overrides()
        self.assertEqual(stored["ADMIN_DASHBOARD_PASSWORD"], secret)
        self.assertEqual(stored["HTTP_CLIENT_LIMIT"], 77)
        audit = await self.database.fetch_all(
            """
            SELECT key, action, previous_source, next_source
            FROM operator_settings_audit
            ORDER BY key
            """
        )
        self.assertNotIn(secret, repr([dict(row) for row in audit]))
        self.assertEqual({row["next_source"] for row in audit}, {"dashboard"})

    async def test_concurrent_mutations_receive_distinct_coherent_revisions(self):
        first, second = await asyncio.gather(
            self.store.save({"HTTP_CLIENT_LIMIT": 71}, actor="admin-a"),
            self.store.save({"METRICS_CACHE_TTL": 91}, actor="admin-b"),
        )
        self.assertEqual({first.revision, second.revision}, {1, 2})
        self.assertEqual(await self.store.current_revision(), 2)
        self.assertEqual(
            await self.store.load_overrides(),
            {"HTTP_CLIENT_LIMIT": 71, "METRICS_CACHE_TTL": 91},
        )
        revisions = await self.database.fetch_all(
            """
            SELECT revision, changed_keys_json
            FROM operator_settings_revisions
            ORDER BY revision
            """
        )
        self.assertEqual([row["revision"] for row in revisions], [1, 2])

    async def test_save_waits_for_a_concurrent_sqlite_writer(self):
        blocker = sqlite3.connect(f"{self.temp_dir.name}/operator.db")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            """
            UPDATE operator_settings_state
            SET current_revision = current_revision
            WHERE id = 1
            """
        )

        save = asyncio.create_task(
            self.store.save({"HTTP_CLIENT_LIMIT": 71}, actor="admin")
        )
        await asyncio.sleep(0.1)
        self.assertFalse(save.done())

        blocker.commit()
        blocker.close()

        result = await asyncio.wait_for(save, timeout=1)
        self.assertEqual(result.revision, 1)

    async def test_usenet_enablement_and_engine_wait_for_the_same_restart(self):
        await self.store.save(
            {
                "USENET_ENABLED": True,
                "USENET_ENGINE_ENABLED": True,
                "USENET_ENGINE_REQUIRED": True,
            },
            actor="admin",
        )
        current = AppSettings(
            _env_file=None,
            USENET_ENABLED=False,
            USENET_ENGINE_ENABLED=False,
            USENET_ENGINE_REQUIRED=False,
        )
        deployment = _GeneratedSecretInputs(_env_file=None)

        with (
            patch(
                "comet.core.operator_settings._GeneratedSecretInputs",
                return_value=deployment,
            ),
            patch.object(type(settings), "active_snapshot", return_value=current),
        ):
            prepared = await prepare_settings_application(self.database)

        self.assertFalse(prepared.candidate.USENET_ENABLED)
        self.assertFalse(prepared.candidate.USENET_ENGINE_ENABLED)
        self.assertFalse(prepared.candidate.USENET_ENGINE_REQUIRED)
        expected_usenet_keys = (
            "USENET_ENABLED",
            "USENET_ENGINE_ENABLED",
            "USENET_ENGINE_REQUIRED",
        )
        self.assertEqual(
            tuple(
                key
                for key in prepared.application.restart_keys
                if key in expected_usenet_keys
            ),
            expected_usenet_keys,
        )
        self.assertIn(
            "USENET_NATIVE_ACCESS_TOKEN",
            prepared.application.restart_keys,
        )
        self.assertIn("COMET_CAPABILITY_SECRET", prepared.application.restart_keys)
        self.assertNotIn("USENET_ENGINE_ENABLED", prepared.changed_keys)

    async def test_reset_returns_to_environment_source(self):
        with patch.dict(os.environ, {"HTTP_CLIENT_LIMIT": "88"}):
            await self.store.save(
                {"HTTP_CLIENT_LIMIT": 77},
                actor="admin",
            )
            result = await self.store.save(
                {},
                reset_keys={"HTTP_CLIENT_LIMIT"},
                actor="admin",
            )
        self.assertEqual(result.revision, 2)
        self.assertNotIn("HTTP_CLIENT_LIMIT", await self.store.load_overrides())
        row = await self.database.fetch_one(
            """
            SELECT action, next_source
            FROM operator_settings_audit
            WHERE revision = 2
            """
        )
        self.assertEqual(dict(row), {"action": "reset", "next_source": "environment"})

    async def test_bootstrap_settings_cannot_be_overridden(self):
        with self.assertRaisesRegex(ValueError, "deployment-owned"):
            await self.store.save(
                {"DATABASE_PATH": "other.db"},
                actor="admin",
            )


class SharedSettingsBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_bootstrap_enables_wal(self):
        with TemporaryDirectory() as temp_dir:
            path = f"{temp_dir}/bootstrap.db"
            config = BootstrapSettings(_env_file=None, DATABASE_PATH=path)
            with patch.dict(os.environ, {"USENET_ENABLED": "false"}, clear=True):
                await _prepare_effective_payload(config)

            connection = sqlite3.connect(path)
            try:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(journal_mode, "wal")

    async def test_internal_environment_is_not_reclassified_as_deployment(self):
        with TemporaryDirectory() as temp_dir:
            config = BootstrapSettings(
                _env_file=None,
                DATABASE_PATH=f"{temp_dir}/bootstrap.db",
            )
            with patch.dict(os.environ, {}, clear=True):
                payload = await _prepare_effective_payload(config)
                document = _encode_payload(payload)

            with patch.dict(
                os.environ,
                {
                    "COMET_EFFECTIVE_SETTINGS_JSON": document,
                    "PROMETHEUS_MULTIPROC_DIR": "/tmp/comet-prometheus",
                },
                clear=True,
            ):
                self.assertNotIn(
                    "PROMETHEUS_MULTIPROC_DIR",
                    deployment_setting_keys(),
                )
                self.assertNotIn(
                    "PROMETHEUS_MULTIPROC_DIR",
                    fresh_runtime_environment(),
                )

    async def test_bootstrap_persists_generated_credentials_across_starts(self):
        with TemporaryDirectory() as temp_dir:
            config = BootstrapSettings(
                _env_file=None,
                DATABASE_PATH=f"{temp_dir}/bootstrap.db",
            )
            with patch.dict(
                os.environ,
                {"USENET_ENABLED": "false"},
                clear=True,
            ):
                first = await _prepare_effective_payload(config)
                second = await _prepare_effective_payload(config)
        self.assertEqual(first.values, second.values)
        self.assertEqual(first.revision, 0)
        self.assertEqual(
            first.generated_keys,
            {
                "ADMIN_DASHBOARD_PASSWORD",
                "PROXY_DEBRID_STREAM_PASSWORD",
                "COMETNET_API_KEY",
            },
        )

    async def test_dashboard_override_has_priority_over_environment(self):
        with TemporaryDirectory() as temp_dir:
            config = BootstrapSettings(
                _env_file=None,
                DATABASE_PATH=f"{temp_dir}/bootstrap.db",
            )
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/bootstrap.db")
            )
            await database.connect()
            try:
                await run_schema_migrations(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                store = OperatorSettingsStore(database)
                with patch.dict(os.environ, {"HTTP_CLIENT_LIMIT": "55"}):
                    await store.save(
                        {"HTTP_CLIENT_LIMIT": 77},
                        actor="admin",
                    )
                    payload = await _prepare_effective_payload(config)
            finally:
                await database.disconnect()
        inputs = AppSettings(
            _env_file=None,
            HTTP_CLIENT_LIMIT=payload.values["HTTP_CLIENT_LIMIT"],
        )
        self.assertEqual(inputs.HTTP_CLIENT_LIMIT, 77)
        self.assertEqual(payload.revision, 1)
        self.assertIn("HTTP_CLIENT_LIMIT", payload.dashboard_keys)

    async def test_explicit_deployment_secrets_are_not_replaced(self):
        with TemporaryDirectory() as temp_dir:
            config = BootstrapSettings(
                _env_file=None,
                DATABASE_PATH=f"{temp_dir}/bootstrap.db",
            )
            with patch.dict(
                os.environ,
                {
                    "ADMIN_DASHBOARD_PASSWORD": "deployed-admin",
                    "PROXY_DEBRID_STREAM_PASSWORD": "deployed-proxy",
                    "COMETNET_API_KEY": "deployed-network",
                    "USENET_ENABLED": "false",
                },
                clear=True,
            ):
                payload = await _prepare_effective_payload(config)
                configured = AppSettings(_env_file=None, **payload.values)
        self.assertEqual(payload.generated_keys, frozenset())
        self.assertEqual(configured.ADMIN_DASHBOARD_PASSWORD, "deployed-admin")
        self.assertEqual(
            configured.PROXY_DEBRID_STREAM_PASSWORD,
            "deployed-proxy",
        )
        self.assertEqual(configured.COMETNET_API_KEY, "deployed-network")


class SettingsCatalogTests(unittest.TestCase):
    def test_catalog_covers_every_application_and_logging_setting_once(self):
        from comet.observability.logging import LoggingSettings

        catalog = build_settings_catalog()
        keys = [entry.key for entry in catalog]
        expected = [key for key in AppSettings.model_fields if key.isupper()] + list(
            LoggingSettings.model_fields
        )
        self.assertEqual(keys, expected)

        by_key = {entry.key: entry for entry in catalog}
        for key in (
            "DATABASE_TYPE",
            "DATABASE_URL",
            "DATABASE_PATH",
            "DATABASE_FORCE_IPV4_RESOLUTION",
        ):
            self.assertTrue(by_key[key].deployment_owned)
        self.assertTrue(by_key["ADMIN_DASHBOARD_PASSWORD"].sensitive)
        self.assertFalse(by_key["ADMIN_DASHBOARD_PASSWORD"].restart_required)
        self.assertEqual(
            by_key["ADMIN_DASHBOARD_PASSWORD"].apply_mode,
            "component",
        )
        self.assertTrue(by_key["FASTAPI_PORT"].restart_required)
        self.assertEqual(by_key["FASTAPI_PORT"].apply_mode, "process")
        self.assertTrue(by_key["DATABASE_URL"].sensitive)
        self.assertIsNone(by_key["DATABASE_URL"].default)
        self.assertEqual(by_key["DATABASE_PATH"].default, "data/comet.db")
        self.assertEqual(
            by_key["USENET_NATIVE_SERVERS"].structured_editor,
            "nntp_servers",
        )
        self.assertEqual(by_key["HTTP_CLIENT_LIMIT"].value_kind, "integer")
        self.assertEqual(by_key["COMET_URL"].value_kind, "string_or_list")
        self.assertEqual(by_key["COMET_URL"].item_kind, "string")
        self.assertEqual(
            by_key["SCRAPE_COMET"].choices,
            (False, True, "live", "background"),
        )
        for entry in catalog:
            if entry.value_kind in {"boolean", "integer", "number"}:
                with self.subTest(key=entry.key):
                    self.assertFalse(entry.nullable)
        for key in (
            "DATABASE_TYPE",
            "PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE",
            "PROXY_ETHOS",
            "COMETNET_CONTRIBUTION_MODE",
        ):
            with self.subTest(key=key):
                self.assertFalse(by_key[key].nullable)
        self.assertTrue(by_key["INDEXER_MANAGER_TYPE"].nullable)


class RuntimeRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_restart_uses_the_official_launcher_process(self):
        with (
            patch.dict(os.environ, {"COMET_WEB_MASTER_PID": "4242"}),
            patch("comet.core.runtime_registry.os.getpid", return_value=1000),
        ):
            self.assertEqual(restart_target_pid(), 4242)
        with (
            patch.dict(os.environ, {"COMET_WEB_MASTER_PID": "0"}),
            patch("comet.core.runtime_registry.os.getpid", return_value=1000),
        ):
            self.assertEqual(restart_target_pid(), 1000)

    async def test_heartbeat_tracks_process_and_expires_stale_instances(self):
        with TemporaryDirectory() as temp_dir:
            database = ReplicaAwareDatabase(
                Database(f"sqlite+aiosqlite:///{temp_dir}/runtime.db")
            )
            await database.connect()
            try:
                await run_schema_migrations(
                    database,
                    is_sqlite=True,
                    is_postgres=False,
                )
                settings = AppSettings(
                    _env_file=None,
                    RUNTIME_STALE_SECONDS=15,
                )
                identity = RuntimeIdentity(
                    instance_id="a" * 32,
                    started_at=100.0,
                    hostname="replica-a",
                )
                registry = RuntimeRegistry(database, settings, identity=identity)
                await registry.heartbeat(
                    role="web_worker",
                    readiness={"state": "ready"},
                    process_id=123,
                    observed_at=200.0,
                )
                instance = await database.fetch_one("SELECT * FROM runtime_instances")
                process = await database.fetch_one("SELECT * FROM runtime_processes")
                self.assertEqual(instance["instance_id"], "a" * 32)
                self.assertEqual(instance["applied_revision"], 0)
                self.assertEqual(process["role"], "web_worker")

                await registry.expire_stale(observed_at=216.0)
                self.assertEqual(
                    await database.fetch_val("SELECT COUNT(*) FROM runtime_instances"),
                    0,
                )
            finally:
                await database.disconnect()
