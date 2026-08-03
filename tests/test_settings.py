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
    _resolve_persisted_token,
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

    def test_scraper_modes_normalize_environment_boolean_strings(self):
        settings = AppSettings(
            _env_file=None,
            SCRAPE_ZILEAN="True",
            SCRAPE_STREMTHRU="false",
            SCRAPE_TORRENTIO="LIVE",
        )

        self.assertIs(settings.SCRAPE_ZILEAN, True)
        self.assertIs(settings.SCRAPE_STREMTHRU, False)
        self.assertEqual(settings.SCRAPE_TORRENTIO, "live")

    def test_indexer_languages_are_normalized_and_deduplicated(self):
        settings = AppSettings(
            _env_file=None,
            INDEXER_LANGUAGES=[" IT ", "fr", "it"],
        )

        self.assertEqual(settings.INDEXER_LANGUAGES, ["it", "fr"])

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
