import unittest
from unittest.mock import patch

from pydantic import ValidationError

from comet.core.logger import log_startup_info
from comet.core.models import AppSettings, settings as runtime_settings


class AppSettingsTests(unittest.TestCase):
    def test_startup_logs_indexer_title_configuration(self):
        with (
            patch.object(runtime_settings, "INDEXER_LANGUAGES", ["it", "fr"]),
            patch.object(runtime_settings, "INDEXER_INCLUDE_CANONICAL_TITLE", True),
            patch.object(runtime_settings, "INDEXER_INCLUDE_ORIGINAL_TITLE", False),
            patch("comet.core.logger.logger.log") as log,
            patch("comet.core.logger.logger.warning"),
        ):
            log_startup_info(runtime_settings)

        self.assertIn(
            (
                "COMET",
                "Indexer Title Search: INDEXER_INCLUDE_CANONICAL_TITLE=True - "
                "INDEXER_INCLUDE_ORIGINAL_TITLE=False - INDEXER_LANGUAGES=it, fr",
            ),
            [call.args for call in log.call_args_list],
        )

    def test_indexer_title_sources_have_recall_oriented_defaults(self):
        settings = AppSettings(_env_file=None)

        self.assertTrue(settings.INDEXER_INCLUDE_CANONICAL_TITLE)
        self.assertTrue(settings.INDEXER_INCLUDE_ORIGINAL_TITLE)

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
            SCRAPE_TORBOX="both",
            SCRAPE_DMM="false",
        )

        self.assertEqual(settings.SCRAPE_NYAA, "live")
        self.assertIs(settings.SCRAPE_TORBOX, True)
        self.assertIs(settings.SCRAPE_DMM, False)

    def test_invalid_scraper_mode_fails_configuration(self):
        with self.assertRaisesRegex(
            ValidationError,
            "scraper mode must be false, true, both, live, or background",
        ):
            AppSettings(_env_file=None, SCRAPE_NYAA="lvie")

    def test_non_positive_concurrency_fails_configuration(self):
        for field in (
            "NYAA_MAX_CONCURRENT_PAGES",
            "ANIMETOSHO_MAX_CONCURRENT_PAGES",
            "DMM_INGEST_CONCURRENT_WORKERS",
            "DMM_INGEST_BATCH_SIZE",
            "BITMAGNET_MAX_CONCURRENT_PAGES",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValidationError, "work count must be a positive integer"
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
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ValidationError,
                    "CometNet operational values must be finite and greater than zero",
                ):
                    AppSettings(_env_file=None, **{field: 0})

    def test_non_finite_cometnet_interval_fails_configuration(self):
        with self.assertRaisesRegex(
            ValidationError,
            "CometNet operational values must be finite and greater than zero",
        ):
            AppSettings(_env_file=None, COMETNET_GOSSIP_INTERVAL=float("inf"))

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

    def test_session_configuration_requires_current_secure_shape(self):
        for field in (
            "ADMIN_DASHBOARD_SESSION_TTL",
            "CONFIGURE_PAGE_SESSION_TTL",
        ):
            for value in (True, 59, 0, None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        AppSettings(_env_file=None, **{field: value})

        for password in ("", None):
            with self.subTest(password=password):
                with self.assertRaisesRegex(
                    ValidationError, "must be a non-empty string"
                ):
                    AppSettings(_env_file=None, ADMIN_DASHBOARD_PASSWORD=password)
