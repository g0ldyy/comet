import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dotenv import dotenv_values
from pydantic import ValidationError

from comet.core.models import (
    AppSettings,
    _build_database_instance,
    _resolve_persisted_token,
)
from comet.core.scrape import (
    normalize_scraper_name,
    normalize_scraper_timeout_selector,
)
from comet.observability.logging import LoggingSettings


class AppSettingsTests(unittest.TestCase):
    def test_env_sample_is_a_loadable_complete_settings_contract(self):
        values = dotenv_values(".env-sample")
        self.assertFalse(
            {
                key: value
                for key, value in values.items()
                if isinstance(value, str) and value.startswith("#")
            }
        )
        modeled = set(AppSettings.model_fields) | set(LoggingSettings.model_fields)
        compose_only = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
        self.assertEqual(set(values) - modeled - compose_only, set())

        with patch.dict(os.environ, {}, clear=True):
            configured = AppSettings(
                _env_file=".env-sample",
                DATABASE_URL="comet:secret@postgres/comet",
            )
            logging = LoggingSettings(_env_file=".env-sample")

        self.assertEqual(configured.DATABASE_TYPE, "postgresql")
        self.assertEqual(
            configured.admin_dashboard_password_source,
            "generated_memory",
        )
        self.assertEqual(logging.LOG_PROFILE.value, "normal")

    def test_indexer_title_sources_have_recall_oriented_defaults(self):
        settings = AppSettings(_env_file=None)

        self.assertTrue(settings.INDEXER_INCLUDE_CANONICAL_TITLE)
        self.assertTrue(settings.INDEXER_INCLUDE_ORIGINAL_TITLE)

    def test_branding_paths_indexers_and_urls_are_bounded(self):
        configured = AppSettings(
            _env_file=None,
            ADDON_ID="com.example-addon",
            ADDON_NAME="Comet Test",
            JACKETT_INDEXERS=[" One ", "one", "TWO"],
            INDEXER_LANGUAGES=["EN", "fr", "en"],
            COMET_URL=["https://one.example/", "https://one.example"],
        )
        self.assertEqual(configured.JACKETT_INDEXERS, ["one", "two"])
        self.assertEqual(configured.INDEXER_LANGUAGES, ["en", "fr"])
        self.assertEqual(configured.COMET_URL, ["https://one.example"])

        invalid = (
            {"ADDON_ID": "bad id"},
            {"ADDON_NAME": "name\ninjection"},
            {"PROMETHEUS_MULTIPROC_DIR": "metrics\npath"},
            {"USENET_ARTIFACT_DIR": ""},
            {"JACKETT_INDEXERS": ["x"] * 65},
            {"JACKETT_INDEXERS": ["x" * 129]},
            {"INDEXER_LANGUAGES": ["en"] * 33},
            {"COMET_URL": ["https://example.test"] * 65},
            {"COMET_URL": "ftp://example.test/path"},
            {"COMET_URL": "https://user:secret@example.test/path"},
            {"PUBLIC_BASE_URL": "https://example.test?token=secret"},
            {"PUBLIC_BASE_URL": "none"},
            {"TORRENT_DISABLED_STREAM_URL": "https://example.test/#fragment"},
        )
        for values in invalid:
            with (
                self.subTest(values=next(iter(values))),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **values)

    def test_database_batch_size_has_a_memory_bound(self):
        self.assertEqual(
            AppSettings(
                _env_file=None, DATABASE_BATCH_SIZE=100_000
            ).DATABASE_BATCH_SIZE,
            100_000,
        )
        with self.assertRaisesRegex(ValidationError, "at most 100000"):
            AppSettings(_env_file=None, DATABASE_BATCH_SIZE=100_001)

    def test_database_configuration_has_one_closed_backend_contract(self):
        for alias in ("postgres", "postgresql+asyncpg", "pgsql", "psql"):
            with self.subTest(alias=alias):
                configured = AppSettings(
                    _env_file=None,
                    DATABASE_TYPE=alias,
                    DATABASE_URL="operator:secret@db.example:5432/comet",
                )
                self.assertEqual(configured.DATABASE_TYPE, "postgresql")

        for value in ("mysql", "", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, DATABASE_TYPE=value)

        for value in (
            "operator:secret@db.example",
            "operator:secret@:5432/comet",
            "mysql://operator:secret@db.example/comet",
            "operator:secret@db.example:70000/comet",
            "operator:secret@db.example/comet\ninjected",
            "x" * 4_097,
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AppSettings(
                    _env_file=None,
                    DATABASE_TYPE="postgresql",
                    DATABASE_URL=value,
                )

        configured = AppSettings(
            _env_file=None,
            DATABASE_TYPE="postgresql",
            DATABASE_URL="postgresql+asyncpg://operator:secret@db.example/comet",
        )
        with patch("comet.core.models.IS_SQLITE", False):
            database = _build_database_instance(configured.DATABASE_URL)
        self.assertEqual(database.url.scheme, "postgresql+asyncpg")
        self.assertEqual(database.url.hostname, "db.example")

    def test_database_paths_and_replicas_are_bounded(self):
        for path in ("", " path", "path\nname", "x" * 4_097, ":memory:", None):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                AppSettings(
                    _env_file=None,
                    DATABASE_TYPE="sqlite",
                    DATABASE_PATH=path,
                )

        with self.assertRaisesRegex(ValidationError, "does not support read replicas"):
            AppSettings(
                _env_file=None,
                DATABASE_TYPE="sqlite",
                DATABASE_READ_REPLICA_URLS=["reader:secret@replica.example/comet"],
            )

        replica = "reader:secret@replica.example/comet"
        configured = AppSettings(
            _env_file=None,
            DATABASE_TYPE="postgresql",
            DATABASE_URL="operator:secret@primary.example/comet",
            DATABASE_READ_REPLICA_URLS=[
                replica,
                f"postgresql://{replica}",
            ],
        )
        self.assertEqual(configured.DATABASE_READ_REPLICA_URLS, [replica])
        with self.assertRaisesRegex(ValidationError, "at most 64 URLs"):
            AppSettings(
                _env_file=None,
                DATABASE_TYPE="postgresql",
                DATABASE_URL="operator:secret@primary.example/comet",
                DATABASE_READ_REPLICA_URLS=[
                    f"reader:secret@replica-{index}.example/comet"
                    for index in range(65)
                ],
            )

    def test_scrape_timeout_defaults_and_overrides_are_normalized(self):
        settings = AppSettings(
            _env_file=None,
            SCRAPER_TIMEOUT_OVERRIDES={
                " ZileanScraper ": 90,
                "Jackett:LIVE": 20.5,
            },
        )

        self.assertEqual(settings.LIVE_SCRAPE_TIMEOUT, 30.0)
        self.assertEqual(settings.BACKGROUND_SCRAPE_TIMEOUT, 30.0)
        self.assertEqual(
            settings.SCRAPER_TIMEOUT_OVERRIDES,
            {"zilean": 90.0, "jackett:live": 20.5},
        )

    def test_scraper_selectors_are_bounded_and_error_safe(self):
        self.assertEqual(normalize_scraper_name("JackettScraper"), "jackett")
        self.assertEqual(
            normalize_scraper_timeout_selector("Jackett:LIVE"),
            "jackett:live",
        )
        secret = "selector-secret-that-must-not-appear"
        for value in (
            f"{secret}:live",
            "jackett:live:extra",
            "é",
            "x" * 81,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                normalize_scraper_timeout_selector(value)
            self.assertNotIn(secret, str(raised.exception))

    def test_invalid_scrape_timeout_configuration_fails(self):
        for field in ("LIVE_SCRAPE_TIMEOUT", "BACKGROUND_SCRAPE_TIMEOUT"):
            for value in (True, 0, -1, 3_601, float("inf"), None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(
                        ValidationError,
                        "finite numbers greater than zero",
                    ):
                        AppSettings(_env_file=None, **{field: value})

        invalid_overrides = (
            [],
            {"Zilean": True},
            {"Zilean": 0},
            {"Zilean": 3_601},
            {"Zilean:batch": 10},
            {"Zilean:live:extra": 10},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    AppSettings(
                        _env_file=None,
                        SCRAPER_TIMEOUT_OVERRIDES=overrides,
                    )

        settings = AppSettings(
            _env_file=None,
            SCRAPER_TIMEOUT_OVERRIDES={"Zilean": 10, "zileanScraper": 20},
        )
        self.assertEqual(settings.SCRAPER_TIMEOUT_OVERRIDES, {"zilean": 20.0})

    def test_indexer_languages_are_normalized_and_deduplicated(self):
        settings = AppSettings(
            _env_file=None,
            INDEXER_LANGUAGES=[" IT ", "fr", "it"],
        )

        self.assertEqual(settings.INDEXER_LANGUAGES, ["it", "fr"])

    def test_invalid_indexer_language_fails_configuration(self):
        for language in ("ita", "i", "fr-FR", "1t"):
            with (
                self.subTest(language=language),
                self.assertRaisesRegex(ValidationError, "ISO 639-1"),
            ):
                AppSettings(_env_file=None, INDEXER_LANGUAGES=[language])

        with self.assertRaises(ValidationError):
            AppSettings(_env_file=None, INDEXER_LANGUAGES=[1])

    def test_scraper_modes_normalize_documented_values(self):
        settings = AppSettings(
            _env_file=None,
            SCRAPE_NYAA="live",
            SCRAPE_DMM="false",
        )

        self.assertEqual(settings.SCRAPE_NYAA, "live")
        self.assertIs(settings.SCRAPE_DMM, False)
        self.assertTrue(settings.is_scraper_enabled(settings.SCRAPE_NYAA, "live"))
        self.assertFalse(
            settings.is_scraper_enabled(settings.SCRAPE_NYAA, "background")
        )
        self.assertEqual(settings.format_scraper_mode(settings.SCRAPE_NYAA), "live")
        self.assertTrue(settings.is_any_context_enabled(settings.SCRAPE_NYAA))
        self.assertFalse(settings.is_any_context_enabled(settings.SCRAPE_DMM))

    def test_invalid_scraper_mode_fails_configuration(self):
        with self.assertRaisesRegex(
            ValidationError,
            "scraper mode must be false, true, both, live, or background",
        ):
            AppSettings(_env_file=None, SCRAPE_NYAA="lvie")

    def test_animetosho_usenet_discovery_is_independent_from_torrent_discovery(self):
        secret = "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M"
        settings = AppSettings(
            _env_file=None,
            USENET_ENABLED=True,
            COMET_CAPABILITY_SECRET=secret,
            SCRAPE_ANIMETOSHO=False,
            SCRAPE_ANIMETOSHO_USENET=True,
        )

        self.assertIs(settings.SCRAPE_ANIMETOSHO, False)
        self.assertIs(settings.SCRAPE_ANIMETOSHO_USENET, True)

    def test_instance_nntp_servers_use_the_shared_canonical_contract(self):
        settings = AppSettings(
            _env_file=None,
            USENET_NATIVE_SERVERS=[
                {
                    "name": "Primary",
                    "host": "NËWS.Example.COM",
                    "port": 563,
                    "tls_mode": "implicit",
                    "username": "member",
                    "password": "secret",
                    "connections": 4,
                    "priority": 0,
                }
            ],
        )

        self.assertEqual(
            settings.USENET_NATIVE_SERVERS,
            [
                {
                    "name": "Primary",
                    "host": "xn--nws-jma.example.com",
                    "port": 563,
                    "tls_mode": "implicit",
                    "username": "member",
                    "password": "secret",
                    "connections": 4,
                    "priority": 0,
                    "backup": False,
                    "pipeline": 16,
                }
            ],
        )

    def test_instance_nntp_server_count_fails_before_runtime(self):
        servers = [
            {
                "name": f"server_{index}",
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "connections": 1,
                "priority": index,
            }
            for index in range(17)
        ]

        with self.assertRaisesRegex(ValidationError, "one to 16"):
            AppSettings(_env_file=None, USENET_NATIVE_SERVERS=servers)

    def test_capability_secret_is_optional_in_single_replica_configuration(self):
        settings = AppSettings(
            _env_file=None,
            USENET_ENABLED=True,
            COMET_CAPABILITY_SECRET=None,
        )

        self.assertIsNone(settings.COMET_CAPABILITY_SECRET)
        self.assertEqual(
            settings.COMET_CAPABILITY_SECRET_FILE,
            "data/comet_capability_secret.txt",
        )
        with self.assertRaisesRegex(
            ValidationError,
            "COMET_CAPABILITY_SECRET or COMET_CAPABILITY_SECRET_FILE",
        ):
            AppSettings(
                _env_file=None,
                USENET_ENABLED=True,
                COMET_CAPABILITY_SECRET=None,
                COMET_CAPABILITY_SECRET_FILE=None,
            )

    def test_usenet_access_token_is_generated_only_when_useful(self):
        disabled = AppSettings(_env_file=None)
        generated = AppSettings(_env_file=None, USENET_ENABLED=True)
        configured = AppSettings(
            _env_file=None,
            USENET_NATIVE_ACCESS_TOKEN="x",
        )

        self.assertIsNone(disabled.USENET_NATIVE_ACCESS_TOKEN)
        self.assertEqual(disabled.usenet_native_access_token_source, "disabled")
        self.assertIsInstance(generated.USENET_NATIVE_ACCESS_TOKEN, str)
        self.assertGreaterEqual(len(generated.USENET_NATIVE_ACCESS_TOKEN), 32)
        self.assertEqual(
            generated.usenet_native_access_token_source,
            "generated_memory",
        )
        self.assertEqual(
            configured.usenet_native_access_token_source,
            "configured",
        )
        self.assertEqual(configured.USENET_NATIVE_ACCESS_TOKEN, "x")
        with self.assertRaises(ValidationError):
            AppSettings(_env_file=None, USENET_NATIVE_ACCESS_TOKEN="x" * 257)

    def test_capability_secret_accepts_an_operator_passphrase(self):
        configured = AppSettings(
            _env_file=None,
            COMET_CAPABILITY_SECRET="x",
        )

        self.assertEqual(configured.COMET_CAPABILITY_SECRET, "x")

    def test_generated_capability_secret_is_persisted_and_reused(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "capability-secret"

            first, first_source = _resolve_persisted_token(
                None,
                str(path),
                "COMET_CAPABILITY_SECRET",
            )
            second, second_source = _resolve_persisted_token(
                None,
                str(path),
                "COMET_CAPABILITY_SECRET",
            )

            self.assertEqual(first_source, "generated_file")
            self.assertEqual(second_source, "file")
            self.assertEqual(first, second)
            self.assertEqual(AppSettings.validate_comet_capability_secret(first), first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_persisted_secret_resolution_is_race_safe_and_relay_can_not_generate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "shared-secret"
            with ThreadPoolExecutor(max_workers=8) as executor:
                resolved = list(
                    executor.map(
                        lambda _index: _resolve_persisted_token(
                            None,
                            str(path),
                            "ADMIN_DASHBOARD_PASSWORD",
                        ),
                        range(8),
                    )
                )

            self.assertEqual(len({value for value, _source in resolved}), 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, "COMETNET_API_KEY is required"):
                _resolve_persisted_token(
                    None,
                    str(Path(directory) / "missing-relay-key"),
                    "COMETNET_API_KEY",
                    allow_generate=False,
                )

    def test_existing_secret_files_must_be_private_regular_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public"
            public.write_text("credential", encoding="utf-8")
            os.chmod(public, 0o644)
            with self.assertRaisesRegex(RuntimeError, "is unreadable"):
                _resolve_persisted_token(None, str(public), "ADMIN_DASHBOARD_PASSWORD")

            private = root / "private"
            private.write_text("credential", encoding="utf-8")
            os.chmod(private, 0o600)
            linked = root / "linked"
            linked.symlink_to(private)
            with self.assertRaisesRegex(RuntimeError, "is unreadable"):
                _resolve_persisted_token(None, str(linked), "ADMIN_DASHBOARD_PASSWORD")

    def test_persisted_tokens_have_bounded_files_and_url_safe_api_shape(self):
        self.assertEqual(
            AppSettings(_env_file=None, PUBLIC_API_TOKEN="x").PUBLIC_API_TOKEN,
            "x",
        )
        for token in ("contains/slash", "contains space", "x" * 257):
            with self.subTest(token=token[:16]), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, PUBLIC_API_TOKEN=token)

        for path in ("secret\nfile", "x" * 4_097):
            with self.subTest(path=path[:16]), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, PUBLIC_API_TOKEN_FILE=path)

        with TemporaryDirectory() as directory:
            path = Path(directory) / "oversized-token"
            path.write_bytes(b"x" * 4_097)
            with (
                self.assertRaisesRegex(RuntimeError, "is unreadable"),
            ):
                _resolve_persisted_token(
                    None,
                    str(path),
                    "PUBLIC_API_TOKEN",
                )

            exact = Path(directory) / "exact-token"
            exact.write_text(" credential \n", encoding="utf-8")
            exact.chmod(0o600)
            token, source = _resolve_persisted_token(
                None,
                str(exact),
                "PUBLIC_API_TOKEN",
            )
            self.assertEqual((token, source), (" credential ", "file"))

    def test_usenet_discovery_scraper_requires_a_valid_server_topology(self):
        with self.assertRaisesRegex(
            ValidationError,
            "Usenet discovery scrapers require USENET_ENABLED",
        ):
            AppSettings(
                _env_file=None,
                SCRAPE_ANIMETOSHO_USENET=True,
            )

    def test_non_positive_concurrency_fails_configuration(self):
        for field in (
            "NYAA_MAX_CONCURRENT_PAGES",
            "ANIMETOSHO_MAX_CONCURRENT_PAGES",
            "DMM_INGEST_CONCURRENT_WORKERS",
            "DMM_INGEST_BATCH_SIZE",
            "BITMAGNET_MAX_CONCURRENT_PAGES",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    ValidationError, "work count must be a positive integer"
                ),
            ):
                AppSettings(_env_file=None, **{field: 0})

    def test_boolean_concurrency_fails_configuration(self):
        with self.assertRaisesRegex(
            ValidationError, "operational numeric values cannot be booleans"
        ):
            AppSettings(_env_file=None, NYAA_MAX_CONCURRENT_PAGES=True)

    def test_non_positive_cometnet_operations_fail_configuration(self):
        for field in (
            "COMETNET_STATE_SAVE_INTERVAL",
            "COMETNET_GOSSIP_INTERVAL",
            "COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE",
            "COMETNET_PEX_BATCH_SIZE",
            "COMETNET_TRANSPORT_MAX_MESSAGE_SIZE",
            "COMETNET_TRANSPORT_PING_INTERVAL",
            "COMETNET_TRANSPORT_RATE_LIMIT_WINDOW",
        ):
            with (
                self.subTest(field=field),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **{field: 0})

    def test_non_finite_cometnet_interval_fails_configuration(self):
        with self.assertRaises(ValidationError):
            AppSettings(_env_file=None, COMETNET_GOSSIP_INTERVAL=float("inf"))

    def test_cometnet_numeric_settings_have_closed_domains(self):
        invalid = (
            ("COMETNET_LISTEN_PORT", 0),
            ("COMETNET_HTTP_PORT", 65_536),
            ("COMETNET_MAX_PEERS", 1_001),
            ("COMETNET_MIN_PEERS", 0),
            ("COMETNET_TIME_CHECK_TOLERANCE", -1),
            ("COMETNET_TIME_CHECK_TIMEOUT", 301),
            ("COMETNET_REACHABILITY_RETRIES", 101),
            ("COMETNET_REACHABILITY_RETRY_DELAY", -1),
            ("COMETNET_REACHABILITY_TIMEOUT", 301),
            ("COMETNET_UPNP_LEASE_DURATION", 604_801),
            ("COMETNET_STATE_SAVE_INTERVAL", 86_401),
            ("COMETNET_GOSSIP_FANOUT", 1_001),
            ("COMETNET_GOSSIP_MESSAGE_TTL", 65),
            ("COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE", 10_001),
            ("COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE", -1),
            ("COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE", 604_801),
            ("COMETNET_GOSSIP_TORRENT_MAX_AGE", 315_360_001),
            ("COMETNET_PEX_BATCH_SIZE", 1_001),
            ("COMETNET_PEER_CONNECT_BACKOFF_MAX", 86_401),
            ("COMETNET_PEER_MAX_FAILURES", 101),
            ("COMETNET_PEER_CLEANUP_AGE", 315_360_001),
            ("COMETNET_TRANSPORT_MAX_MESSAGE_SIZE", 67_108_865),
            ("COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP", 1_001),
            ("COMETNET_TRANSPORT_RATE_LIMIT_COUNT", 1_000_001),
        )
        for field, value in invalid:
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **{field: value})

        for field in (
            "COMETNET_LISTEN_PORT",
            "COMETNET_MAX_PEERS",
            "COMETNET_TIME_CHECK_TOLERANCE",
            "COMETNET_REACHABILITY_RETRY_DELAY",
            "COMETNET_GOSSIP_FANOUT",
            "COMETNET_TRANSPORT_RATE_LIMIT_COUNT",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValidationError, "cannot be booleans"),
            ):
                AppSettings(_env_file=None, **{field: True})

    def test_cometnet_float_and_reputation_settings_are_finite_and_bounded(self):
        float_fields = (
            "COMETNET_GOSSIP_INTERVAL",
            "COMETNET_TRANSPORT_PING_INTERVAL",
            "COMETNET_TRANSPORT_CONNECTION_TIMEOUT",
            "COMETNET_TRANSPORT_MAX_LATENCY_MS",
            "COMETNET_TRANSPORT_RATE_LIMIT_WINDOW",
        )
        for field in float_fields:
            for value in (0, float("nan"), float("inf"), None, True):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValidationError),
                ):
                    AppSettings(_env_file=None, **{field: value})

        for field in (
            "COMETNET_REPUTATION_INITIAL",
            "COMETNET_REPUTATION_MIN",
            "COMETNET_REPUTATION_MAX",
            "COMETNET_REPUTATION_THRESHOLD_UNTRUSTED",
            "COMETNET_REPUTATION_THRESHOLD_TRUSTED",
            "COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION",
            "COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY",
            "COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY",
            "COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION",
            "COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE",
        ):
            for value in (float("nan"), float("inf"), None, True):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValidationError),
                ):
                    AppSettings(_env_file=None, **{field: value})

        for field in (
            "COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION",
            "COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY",
            "COMETNET_REPUTATION_BONUS_MAX_ANCIENNETY",
            "COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION",
            "COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE",
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **{field: -0.1})

    def test_cometnet_topology_and_reputation_relations_are_closed(self):
        invalid = (
            {"COMETNET_LISTEN_PORT": 9000, "COMETNET_HTTP_PORT": 9000},
            {"COMETNET_MIN_PEERS": 51, "COMETNET_MAX_PEERS": 50},
            {"COMETNET_GOSSIP_FANOUT": 51, "COMETNET_MAX_PEERS": 50},
            {"COMETNET_PEX_BATCH_SIZE": 51, "COMETNET_MAX_PEERS": 50},
            {
                "COMETNET_REPUTATION_MIN": 101,
                "COMETNET_REPUTATION_INITIAL": 100,
            },
            {
                "COMETNET_REPUTATION_THRESHOLD_UNTRUSTED": 1_000,
                "COMETNET_REPUTATION_THRESHOLD_TRUSTED": 1_000,
            },
            {
                "COMETNET_REPUTATION_THRESHOLD_TRUSTED": 10_001,
                "COMETNET_REPUTATION_MAX": 10_000,
            },
            {"COMETNET_PRIVATE_NETWORK": True},
            {
                "COMETNET_PRIVATE_NETWORK": True,
                "COMETNET_NETWORK_ID": "network",
            },
            {"COMETNET_ENABLED": True},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **values)

        private = AppSettings(
            _env_file=None,
            COMETNET_PRIVATE_NETWORK=True,
            COMETNET_NETWORK_ID="private-network",
            COMETNET_NETWORK_PASSWORD="shared secret",
        )
        self.assertEqual(private.COMETNET_NETWORK_ID, "private-network")

        public = AppSettings(
            _env_file=None,
            COMETNET_ENABLED=True,
            COMETNET_ADVERTISE_URL="wss://node.example/cometnet/ws",
        )
        self.assertTrue(public.COMETNET_ENABLED)

    def test_cometnet_text_lists_and_urls_are_bounded(self):
        configured = AppSettings(
            _env_file=None,
            COMETNET_BOOTSTRAP_NODES=[
                "wss://bootstrap.example/cometnet/ws",
                "wss://bootstrap.example/cometnet/ws",
            ],
            COMETNET_MANUAL_PEERS=["ws://127.0.0.1:8765/cometnet/ws"],
            COMETNET_ADVERTISE_URL="wss://node.example/cometnet/ws",
            COMETNET_RELAY_URL="http://relay.example:8766",
            COMETNET_CONTRIBUTION_MODE=" Consumer ",
            COMETNET_TRUSTED_POOLS=["official-sources", "official-sources"],
            COMETNET_NETWORK_ID="private-network",
            COMETNET_NODE_ALIAS="Nœud principal",
        )
        self.assertEqual(configured.COMETNET_CONTRIBUTION_MODE, "consumer")
        self.assertEqual(configured.COMETNET_RELAY_URL, "http://relay.example:8766")
        self.assertEqual(
            AppSettings(_env_file=None, COMETNET_NETWORK_ID="none").COMETNET_NETWORK_ID,
            "none",
        )
        self.assertEqual(
            configured.COMETNET_BOOTSTRAP_NODES,
            ["wss://bootstrap.example/cometnet/ws"],
        )
        self.assertEqual(configured.COMETNET_TRUSTED_POOLS, ["official-sources"])
        self.assertNotIn("COMETNET_INGEST_POOLS", AppSettings.model_fields)

        invalid = (
            {"COMETNET_BOOTSTRAP_NODES": "wss://peer.example"},
            {"COMETNET_BOOTSTRAP_NODES": ["wss://peer.example"] * 65},
            {"COMETNET_MANUAL_PEERS": ["https://peer.example"]},
            {"COMETNET_MANUAL_PEERS": ["wss://user:secret@peer.example"]},
            {"COMETNET_ADVERTISE_URL": "wss://peer.example?token=secret"},
            {"COMETNET_ADVERTISE_URL": "wss://peer.example/#fragment"},
            {"COMETNET_RELAY_URL": "http://relay.example?token=secret"},
            {"COMETNET_CONTRIBUTION_MODE": "invalid"},
            {"COMETNET_TRUSTED_POOLS": "official-sources"},
            {"COMETNET_TRUSTED_POOLS": ["official-sources"] * 65},
            {"COMETNET_TRUSTED_POOLS": ["équipe"]},
            {"COMETNET_NETWORK_ID": "bad network"},
            {"COMETNET_NODE_ALIAS": "bad\nalias"},
            {"COMETNET_NODE_ALIAS": "x" * 129},
        )
        for values in invalid:
            with (
                self.subTest(values=next(iter(values))),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **values)

    def test_http_operational_values_reject_invalid_ranges(self):
        nonnegative_fields = (
            "MEMORY_TRIM_INTERVAL",
            "RATELIMIT_MAX_RETRIES",
            "HTTP_CLIENT_TTL_DNS_CACHE",
            "HTTP_CACHE_STREAMS_TTL",
            "HTTP_CACHE_STALE_WHILE_REVALIDATE",
            "HTTP_CACHE_MANIFEST_TTL",
            "HTTP_CACHE_CONFIGURE_TTL",
        )
        positive_fields = (
            "RATELIMIT_RETRY_BASE_DELAY",
            "HTTP_CLIENT_LIMIT",
            "HTTP_CLIENT_LIMIT_PER_HOST",
            "HTTP_CLIENT_KEEPALIVE_TIMEOUT",
            "HTTP_CLIENT_TIMEOUT_TOTAL",
        )

        for field in nonnegative_fields:
            for value in (-1, float("inf"), None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        AppSettings(_env_file=None, **{field: value})

        for field in positive_fields:
            for value in (0, -1, float("nan"), None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        AppSettings(_env_file=None, **{field: value})

    def test_http_operational_values_reject_booleans_and_excessive_retries(self):
        for field in (
            "MEMORY_TRIM_INTERVAL",
            "RATELIMIT_MAX_RETRIES",
            "RATELIMIT_RETRY_BASE_DELAY",
            "HTTP_CLIENT_LIMIT",
            "HTTP_CLIENT_TIMEOUT_TOTAL",
            "HTTP_CACHE_STREAMS_TTL",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValidationError, "cannot be booleans"):
                    AppSettings(_env_file=None, **{field: True})

        with self.assertRaisesRegex(ValidationError, "cannot exceed 20"):
            AppSettings(_env_file=None, RATELIMIT_MAX_RETRIES=21)

        current = AppSettings(
            _env_file=None,
            MEMORY_TRIM_INTERVAL=0,
            RATELIMIT_MAX_RETRIES=0,
            HTTP_CLIENT_TTL_DNS_CACHE=0,
            HTTP_CACHE_STREAMS_TTL=0,
        )
        self.assertEqual(current.MEMORY_TRIM_INTERVAL, 0)
        self.assertEqual(current.RATELIMIT_MAX_RETRIES, 0)

    def test_general_operational_integers_have_closed_ranges(self):
        accepted = AppSettings(
            _env_file=None,
            DATABASE_STARTUP_CLEANUP_INTERVAL=-1,
            TORRENT_CACHE_TTL=-1,
            LIVE_TORRENT_CACHE_TTL=-1,
            METADATA_CACHE_TTL=0,
            DEBRID_CACHE_TTL=0,
            METRICS_CACHE_TTL=0,
            BITMAGNET_MAX_OFFSET=0,
            ANIME_MAPPING_REFRESH_INTERVAL=0,
            FILTER_PARSE_CACHE_SIZE=0,
        )
        self.assertEqual(accepted.DATABASE_STARTUP_CLEANUP_INTERVAL, -1)
        self.assertEqual(accepted.TORRENT_CACHE_TTL, -1)
        self.assertEqual(accepted.FILTER_PARSE_CACHE_SIZE, 0)

        invalid = (
            ("DATABASE_STARTUP_CLEANUP_INTERVAL", -2),
            ("METADATA_CACHE_TTL", -1),
            ("TORRENT_CACHE_TTL", -2),
            ("LIVE_TORRENT_CACHE_TTL", -2),
            ("DEBRID_CACHE_TTL", -1),
            ("METRICS_CACHE_TTL", -1),
            ("SCRAPE_LOCK_TTL", 0),
            ("INDEXER_MANAGER_TIMEOUT", 0),
            ("INDEXER_MANAGER_UPDATE_INTERVAL", 0),
            ("INDEXER_MANAGER_WAIT_TIMEOUT", 0),
            ("GET_TORRENT_TIMEOUT", 0),
            ("MAGNET_RESOLVE_TIMEOUT", 0),
            ("CATALOG_TIMEOUT", 0),
            ("DMM_INGEST_INTERVAL", 0),
            ("BITMAGNET_MAX_OFFSET", 1_000_001),
            ("PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD", -1),
            ("DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL", 0),
            ("DEBRID_ACCOUNT_SCRAPE_CACHE_TTL", 0),
            ("DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS", 100_001),
            ("DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS", 100_001),
            ("ANIME_MAPPING_REFRESH_INTERVAL", -1),
            ("FILTER_PARSE_CACHE_SIZE", -1),
        )
        for field, value in invalid:
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **{field: value})

        for field in (
            "DATABASE_STARTUP_CLEANUP_INTERVAL",
            "METADATA_CACHE_TTL",
            "SCRAPE_LOCK_TTL",
            "DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS",
            "FILTER_PARSE_CACHE_SIZE",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValidationError, "cannot be booleans"),
            ):
                AppSettings(_env_file=None, **{field: True})

    def test_general_operational_floats_and_relations_are_closed(self):
        for value in (-0.01, 1.01, float("nan"), float("inf"), None, True):
            with self.subTest(ratio=value), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, DEBRID_CACHE_CHECK_RATIO=value)

        for value in (-0.01, 301, float("nan"), float("inf"), True):
            with self.subTest(timeout=value), self.assertRaises(ValidationError):
                AppSettings(
                    _env_file=None,
                    DEBRID_ACCOUNT_SCRAPE_INITIAL_WARM_TIMEOUT=value,
                )

        for values in (
            {
                "DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS": 99,
                "DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS": 100,
            },
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **values)

    def test_general_operational_values_have_allocation_and_delay_caps(self):
        invalid = (
            ("EXECUTOR_MAX_WORKERS", 257),
            ("NYAA_MAX_CONCURRENT_PAGES", 65),
            ("DMM_INGEST_BATCH_SIZE", 10_001),
            ("FILTER_PARSE_CACHE_SHARDS", 65),
            ("LIVE_SCRAPE_TIMEOUT", 3_601),
            ("MEMORY_TRIM_INTERVAL", 31_536_001),
            ("RATELIMIT_RETRY_BASE_DELAY", 3_601),
            ("HTTP_CLIENT_LIMIT", 100_001),
            ("HTTP_CLIENT_KEEPALIVE_TIMEOUT", 3_601),
            ("HTTP_CACHE_STREAMS_TTL", 315_360_001),
            ("ADMIN_DASHBOARD_SESSION_TTL", 31_536_001),
        )
        for field, value in invalid:
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **{field: value})

    def test_proxy_connection_limit_uses_one_canonical_sentinel(self):
        self.assertEqual(
            AppSettings(
                _env_file=None, PROXY_DEBRID_STREAM_MAX_CONNECTIONS=-1
            ).PROXY_DEBRID_STREAM_MAX_CONNECTIONS,
            -1,
        )
        for value in (-2, 0, 100_001, None, True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AppSettings(
                    _env_file=None,
                    PROXY_DEBRID_STREAM_MAX_CONNECTIONS=value,
                )

    def test_background_scheduler_values_have_closed_domains(self):
        accepted = AppSettings(
            _env_file=None,
            BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN=0,
            BACKGROUND_SCRAPER_SUCCESS_TTL=0,
            BACKGROUND_SCRAPER_MAX_RETRIES=-1,
            BACKGROUND_SCRAPER_RUN_TIME_BUDGET=0,
            BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN=0,
            BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL=0,
            BACKGROUND_SCRAPER_DEMAND_LOOKBACK=0,
            BACKGROUND_SCRAPER_DEFER_COOLDOWN=0,
            BACKGROUND_SCRAPER_ALERT_QUEUE_AGE=0,
            BACKGROUND_SCRAPER_RUN_RETENTION_DAYS=0,
        )
        self.assertEqual(accepted.BACKGROUND_SCRAPER_MAX_RETRIES, -1)
        self.assertEqual(accepted.BACKGROUND_SCRAPER_RUN_TIME_BUDGET, 0)

        invalid = (
            ("BACKGROUND_SCRAPER_INTERVAL", 0),
            ("BACKGROUND_SCRAPER_MAX_MOVIES_PER_RUN", 10_001),
            ("BACKGROUND_SCRAPER_MAX_SERIES_PER_RUN", -1),
            ("BACKGROUND_SCRAPER_SUCCESS_TTL", -1),
            ("BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF", 0),
            ("BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF", 315_360_001),
            ("BACKGROUND_SCRAPER_MAX_RETRIES", -2),
            ("BACKGROUND_SCRAPER_RUN_TIME_BUDGET", 604_801),
            ("BACKGROUND_SCRAPER_DISCOVERY_MULTIPLIER", 0),
            ("BACKGROUND_SCRAPER_MAX_EPISODES_PER_SERIES_PER_RUN", 10_001),
            ("BACKGROUND_SCRAPER_EPISODE_REFRESH_TTL", -1),
            ("BACKGROUND_SCRAPER_DEMAND_LOOKBACK", -1),
            ("BACKGROUND_SCRAPER_DEFER_COOLDOWN", -1),
            ("BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK", -1),
            ("BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK", 10_000_001),
            ("BACKGROUND_SCRAPER_QUEUE_HARD_CAP", 10_000_001),
            ("BACKGROUND_SCRAPER_ALERT_QUEUE_AGE", -1),
            ("BACKGROUND_SCRAPER_RUN_RETENTION_DAYS", 3_651),
        )
        for field, value in invalid:
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **{field: value})

        for field in (
            "BACKGROUND_SCRAPER_INTERVAL",
            "BACKGROUND_SCRAPER_MAX_RETRIES",
            "BACKGROUND_SCRAPER_RUN_TIME_BUDGET",
            "BACKGROUND_SCRAPER_QUEUE_HARD_CAP",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValidationError, "cannot be booleans"),
            ):
                AppSettings(_env_file=None, **{field: True})

    def test_background_scheduler_rates_and_relations_are_closed(self):
        for field, invalid_values in (
            (
                "BACKGROUND_SCRAPER_MIN_PRIORITY_SCORE",
                (-1, 1_000_001, float("nan"), float("inf"), None, True),
            ),
            (
                "BACKGROUND_SCRAPER_PRIORITY_DECAY_ON_MISS",
                (-0.1, 1.1, float("nan"), float("inf"), None, True),
            ),
            (
                "BACKGROUND_SCRAPER_ALERT_FAIL_RATE",
                (-0.1, 1.1, float("nan"), float("inf"), None, True),
            ),
        ):
            for value in invalid_values:
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValidationError),
                ):
                    AppSettings(_env_file=None, **{field: value})

        invalid_relations = (
            {
                "BACKGROUND_SCRAPER_FAILURE_BASE_BACKOFF": 11,
                "BACKGROUND_SCRAPER_FAILURE_MAX_BACKOFF": 10,
            },
            {
                "BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK": 0,
                "BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK": 10,
                "BACKGROUND_SCRAPER_QUEUE_HARD_CAP": 20,
            },
            {
                "BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK": 10,
                "BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK": 10,
                "BACKGROUND_SCRAPER_QUEUE_HARD_CAP": 20,
            },
            {
                "BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK": 10,
                "BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK": 30,
                "BACKGROUND_SCRAPER_QUEUE_HARD_CAP": 20,
            },
        )
        for values in invalid_relations:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **values)

        disabled = AppSettings(
            _env_file=None,
            BACKGROUND_SCRAPER_QUEUE_LOW_WATERMARK=0,
            BACKGROUND_SCRAPER_QUEUE_HIGH_WATERMARK=0,
            BACKGROUND_SCRAPER_QUEUE_HARD_CAP=0,
        )
        self.assertEqual(disabled.BACKGROUND_SCRAPER_QUEUE_HARD_CAP, 0)

    def test_worker_count_rejects_boolean_and_excessive_fanout(self):
        for value in (True, -1, 65, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, FASTAPI_WORKERS=value)

        self.assertEqual(
            AppSettings(_env_file=None, FASTAPI_WORKERS=64).FASTAPI_WORKERS,
            64,
        )
        self.assertEqual(
            AppSettings(_env_file=None, FASTAPI_WORKERS=0).FASTAPI_WORKERS,
            0,
        )

    def test_server_bind_configuration_has_closed_domains(self):
        for host in ("", " host", "bad_host", "host\nname", "x" * 254, None):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, FASTAPI_HOST=host)
        for host in ("0.0.0.0", "::", "localhost", "comet.example."):
            with self.subTest(host=host):
                self.assertEqual(
                    AppSettings(_env_file=None, FASTAPI_HOST=host).FASTAPI_HOST,
                    host,
                )

        for port in (True, 0, 65_536, None):
            with self.subTest(port=port), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, FASTAPI_PORT=port)
        self.assertEqual(
            AppSettings(_env_file=None, FASTAPI_PORT=65_535).FASTAPI_PORT,
            65_535,
        )

    def test_settings_validation_errors_hide_rejected_secret_values(self):
        secret = "secret-value-that-must-not-appear\n"
        with self.assertRaises(ValidationError) as raised:
            AppSettings(_env_file=None, ADMIN_DASHBOARD_PASSWORD=secret)

        self.assertNotIn(secret.strip(), str(raised.exception))

    def test_operator_credentials_are_bounded_before_consumers(self):
        configured = AppSettings(
            _env_file=None,
            COMETNET_KEY_PASSWORD="x",
            MEDIAFUSION_URL=[
                "https://one.example",
                "https://two.example",
                "https://three.example",
            ],
            MEDIAFUSION_API_PASSWORD=["first", "", "second"],
            DEBRIDIO_PROVIDER="real_debrid",
        )
        self.assertEqual(
            configured.MEDIAFUSION_API_PASSWORD,
            ["first", "", "second"],
        )

        invalid = (
            {"TMDB_READ_ACCESS_TOKEN": "x" * 4_097},
            {"PROMETHEUS_AUTH_TOKEN": "non-ascii-é"},
            {"INDEXER_MANAGER_API_KEY": 123},
            {"MEDIAFUSION_API_PASSWORD": ["x"] * 65},
            {"AIOSTREAMS_USER_UUID_AND_PASSWORD": ["x" * 4_097]},
            {"DEBRIDIO_PROVIDER": "../provider"},
        )
        for values in invalid:
            with (
                self.subTest(values=next(iter(values))),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **values)

    def test_session_configuration_requires_current_secure_shape(self):
        for field in (
            "ADMIN_DASHBOARD_SESSION_TTL",
            "CONFIGURE_PAGE_SESSION_TTL",
        ):
            for value in (True, 59, 0, None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        AppSettings(_env_file=None, **{field: value})

        for password in ("\n", "x" * 4_097):
            with (
                self.subTest(password=password[:8]),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, ADMIN_DASHBOARD_PASSWORD=password)

        for field in ("ADMIN_DASHBOARD_PASSWORD", "CONFIGURE_PAGE_PASSWORD"):
            self.assertEqual(
                getattr(AppSettings(_env_file=None, **{field: "x"}), field),
                "x",
            )
            for password in ("x" * 4_097, "secret\nvalue"):
                with (
                    self.subTest(field=field, password=password[:16]),
                    self.assertRaises(ValidationError),
                ):
                    AppSettings(_env_file=None, **{field: password})

    def test_prometheus_paths_are_static_and_secrets_are_exact(self):
        configured = AppSettings(
            _env_file=None,
            PROMETHEUS_PATH="/internal/metrics/",
            PROMETHEUS_AUTH_TOKEN="metrics-secret",
            PROMETHEUS_AUTH_TOKEN_FILE="/run/secrets/metrics",
        )

        self.assertEqual(configured.PROMETHEUS_PATH, "/internal/metrics")
        self.assertEqual(configured.PROMETHEUS_AUTH_TOKEN, "metrics-secret")
        self.assertEqual(
            configured.PROMETHEUS_AUTH_TOKEN_FILE,
            "/run/secrets/metrics",
        )

        for path in ("metrics", "/", "/metrics?format=text", "/{tenant}/metrics"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, PROMETHEUS_PATH=path)
        for field, value in (
            ("PROMETHEUS_AUTH_TOKEN", " metrics-secret "),
            ("PROMETHEUS_AUTH_TOKEN_FILE", " /run/secrets/metrics "),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **{field: value})

    def test_empty_environment_values_use_defaults(self):
        with patch.dict(
            os.environ,
            {
                "ADMIN_DASHBOARD_PASSWORD": "",
                "PROXY_DEBRID_STREAM_PASSWORD": "",
                "COMETNET_API_KEY": "",
                "PUBLIC_BASE_URL": "",
            },
            clear=True,
        ):
            configured = AppSettings(_env_file=None)

        self.assertEqual(
            configured.admin_dashboard_password_source,
            "generated_memory",
        )
        self.assertEqual(
            configured.proxy_debrid_stream_password_source,
            "generated_memory",
        )
        self.assertEqual(configured.cometnet_api_key_source, "generated_memory")
        self.assertIsNone(configured.PUBLIC_BASE_URL)
        for value in (
            configured.ADMIN_DASHBOARD_PASSWORD,
            configured.PROXY_DEBRID_STREAM_PASSWORD,
            configured.COMETNET_API_KEY,
        ):
            self.assertIsInstance(value, str)
            self.assertGreaterEqual(len(value), 32)

    def test_empty_logging_environment_values_use_defaults(self):
        with patch.dict(
            os.environ,
            {
                "LOG_PROFILE": "",
                "LOG_FORMAT": "",
            },
            clear=True,
        ):
            configured = LoggingSettings(_env_file=None)

        self.assertEqual(configured.LOG_PROFILE.value, "normal")
        self.assertEqual(configured.LOG_FORMAT.value, "pretty")

    def test_proxy_configuration_is_closed_and_supports_socks(self):
        supported = (
            "http://proxy.example:8080",
            "https://user:pass@proxy.example:8443",
            "socks4://proxy.example:1080",
            "socks4a://proxy.example:1080",
            "socks5://proxy.example:1080",
            "socks5h://user:pass@proxy.example:1080",
        )
        for proxy_url in supported:
            with self.subTest(proxy_url=proxy_url):
                configured = AppSettings(
                    _env_file=None,
                    GLOBAL_PROXY_URL=proxy_url,
                    USER_PROVIDED_PROXY_URL=proxy_url,
                    TORRENTIO_PROXY_URL=proxy_url,
                )
                self.assertEqual(configured.GLOBAL_PROXY_URL, proxy_url)
                self.assertEqual(configured.USER_PROVIDED_PROXY_URL, proxy_url)
                self.assertEqual(configured.TORRENTIO_PROXY_URL, proxy_url)

        for values in (
            {"GLOBAL_PROXY_URL": "ftp://proxy.example"},
            {"USER_PROVIDED_PROXY_URL": "ftp://proxy.example"},
            {"TORRENTIO_PROXY_URL": "socks5h://proxy.example:70000"},
            {"TORRENTIO_PROXY_URL": "socks5h://user:%0A@proxy.example:1080"},
            {"TORRENTIO_PROXY_URL": "socks5h://user:%ZZ@proxy.example:1080"},
            {"PROXY_ETHOS": "sometimes"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **values)

    def test_scraper_url_modes_and_credentials_are_validated_at_startup(self):
        configured = AppSettings(
            _env_file=None,
            SCRAPE_MEDIAFUSION="live",
            MEDIAFUSION_URL=[
                "https://one.example/:LIVE",
                "https://two.example:background",
            ],
            MEDIAFUSION_API_PASSWORD=["one", "two"],
        )
        self.assertEqual(
            configured.MEDIAFUSION_URL,
            [
                "https://one.example:live",
                "https://two.example:background",
            ],
        )

        for values in (
            {"SCRAPE_AIOSTREAMS": True, "AIOSTREAMS_URL": None},
            {
                "AIOSTREAMS_URL": "https://one.example",
                "AIOSTREAMS_USER_UUID_AND_PASSWORD": ["one", "two"],
            },
            {
                "MEDIAFUSION_URL": [
                    "https://one.example",
                    "https://two.example",
                ],
                "MEDIAFUSION_API_PASSWORD": ["one"],
            },
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                AppSettings(_env_file=None, **values)

    def test_enabled_external_scrapers_require_real_credentials(self):
        invalid = (
            ({"SCRAPE_JACKETT": True}, "SCRAPE_JACKETT requires JACKETT_API_KEY"),
            (
                {"SCRAPE_PROWLARR": "live"},
                "SCRAPE_PROWLARR requires PROWLARR_API_KEY",
            ),
            (
                {
                    "SCRAPE_DEBRIDIO": True,
                    "DEBRIDIO_API_KEY": "addon-key",
                },
                "SCRAPE_DEBRIDIO requires DEBRIDIO_PROVIDER, DEBRIDIO_PROVIDER_KEY",
            ),
        )
        for values, reason in invalid:
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(
                    ValidationError,
                    reason,
                ),
            ):
                AppSettings(_env_file=None, **values)

        configured = AppSettings(
            _env_file=None,
            INDEXER_MANAGER_TYPE="jackett",
            INDEXER_MANAGER_API_KEY="configured-jackett-key",
        )
        self.assertIs(configured.SCRAPE_JACKETT, True)
        self.assertEqual(configured.JACKETT_API_KEY, "configured-jackett-key")

    def test_open_operator_text_and_metadata_fields_are_bounded(self):
        invalid = (
            {"INDEXER_MANAGER_TYPE": "sonarr"},
            {"PROXY_DEBRID_STREAM_DEBRID_DEFAULT_SERVICE": "unknown"},
            {"TORRENT_DISABLED_STREAM_NAME": "bad\nname"},
            {"TORRENT_DISABLED_STREAM_DESCRIPTION": "x" * 1_025},
            {"CUSTOM_HEADER_HTML": "x" * 262_145},
            {"COMET_COMMIT_HASH": "not-a-sha"},
            {"COMET_BUILD_DATE": "2026-07-29"},
            {"COMET_BRANCH": "bad branch"},
        )
        for values in invalid:
            with (
                self.subTest(values=next(iter(values))),
                self.assertRaises(ValidationError),
            ):
                AppSettings(_env_file=None, **values)
