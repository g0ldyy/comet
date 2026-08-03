import json
import unittest
from unittest.mock import patch

from comet.core.models import AppSettings
from comet.observability.logging import LoggingSettings, configure
from comet.observability.startup import (
    log_cometnet_standalone_configuration,
    log_startup_configuration,
)


class StartupConfigurationTests(unittest.TestCase):
    @staticmethod
    def generated_secrets(info):
        return {
            call.kwargs["setting_name"]: call.kwargs["generated_secret"]
            for call in info.call_args_list
            if call.args[0] == "config.generated_secret"
        }

    def test_web_startup_delivers_only_useful_generated_secrets(self):
        configured = AppSettings(
            _env_file=None,
            PROXY_DEBRID_STREAM=True,
        )

        with (
            patch("comet.observability.startup.log.info"),
            patch("comet.observability.startup.log.warning") as warning,
        ):
            log_startup_configuration(
                configured,
                workers=2,
                server_name="Gunicorn",
            )

        generated = self.generated_secrets(warning)
        self.assertEqual(
            generated["ADMIN_DASHBOARD_PASSWORD"],
            configured.ADMIN_DASHBOARD_PASSWORD,
        )
        self.assertEqual(
            generated["PROXY_DEBRID_STREAM_PASSWORD"],
            configured.PROXY_DEBRID_STREAM_PASSWORD,
        )
        self.assertNotIn("COMETNET_API_KEY", generated)
        self.assertNotIn("USENET_NATIVE_ACCESS_TOKEN", generated)

    def test_generated_usenet_access_token_is_delivered_at_startup(self):
        configured = AppSettings(_env_file=None, USENET_ENABLED=True)

        with (
            patch("comet.observability.startup.log.info"),
            patch("comet.observability.startup.log.warning") as warning,
        ):
            log_startup_configuration(
                configured,
                workers=1,
                server_name="Uvicorn",
            )

        self.assertEqual(
            self.generated_secrets(warning)["USENET_NATIVE_ACCESS_TOKEN"],
            configured.USENET_NATIVE_ACCESS_TOKEN,
        )

    def test_shared_generated_secrets_are_delivered_at_startup(self):
        configured = AppSettings(
            _env_file=None,
            ADMIN_DASHBOARD_PASSWORD="generated-admin",
            PROXY_DEBRID_STREAM=True,
            PROXY_DEBRID_STREAM_PASSWORD="generated-proxy",
            COMETNET_RELAY_URL="https://relay.example",
            COMETNET_API_KEY="generated-cometnet",
            USENET_ENABLED=True,
            USENET_NATIVE_ACCESS_TOKEN="u" * 32,
            COMET_CAPABILITY_SECRET="A" * 43,
            PUBLIC_API_TOKEN="generated-public-api",
        )
        configured = configured.model_copy(
            update={
                "admin_dashboard_password_source": "generated_shared",
                "proxy_debrid_stream_password_source": "generated_shared",
                "cometnet_api_key_source": "generated_shared",
                "usenet_native_access_token_source": "generated_shared",
            }
        )
        object.__setattr__(
            configured,
            "_public_api_token_source",
            "generated_shared",
        )
        object.__setattr__(
            configured,
            "_comet_capability_secret_source",
            "generated_shared",
        )

        with (
            patch("comet.observability.startup.log.info"),
            patch("comet.observability.startup.log.warning") as warning,
        ):
            log_startup_configuration(
                configured,
                workers=1,
                server_name="Uvicorn",
            )

        self.assertEqual(
            self.generated_secrets(warning),
            {
                "ADMIN_DASHBOARD_PASSWORD": "generated-admin",
                "PROXY_DEBRID_STREAM_PASSWORD": "generated-proxy",
                "COMETNET_API_KEY": "generated-cometnet",
                "USENET_NATIVE_ACCESS_TOKEN": "u" * 32,
                "PUBLIC_API_TOKEN": "generated-public-api",
                "COMET_CAPABILITY_SECRET": "A" * 43,
            },
        )

    def test_configured_secrets_are_never_echoed(self):
        configured = AppSettings(
            _env_file=None,
            ADMIN_DASHBOARD_PASSWORD="configured-admin-secret",
            PROXY_DEBRID_STREAM=True,
            PROXY_DEBRID_STREAM_PASSWORD="configured-proxy-secret",
            COMETNET_RELAY_URL="https://relay.example",
            COMETNET_API_KEY="configured-cometnet-secret",
            USENET_ENABLED=True,
            USENET_NATIVE_ACCESS_TOKEN="configured-usenet-access-token-123",
        )

        with (
            patch("comet.observability.startup.log.info") as info,
            patch("comet.observability.startup.log.warning") as warning,
        ):
            log_startup_configuration(
                configured,
                workers=1,
                server_name="Uvicorn",
            )

        rendered = repr(info.call_args_list + warning.call_args_list)
        self.assertNotIn("configured-admin-secret", rendered)
        self.assertNotIn("configured-proxy-secret", rendered)
        self.assertNotIn("configured-cometnet-secret", rendered)
        self.assertNotIn("configured-usenet-access-token-123", rendered)
        self.assertEqual(self.generated_secrets(warning), {})

    def test_standalone_delivers_generated_cometnet_key(self):
        configured = AppSettings(_env_file=None)

        with (
            patch("comet.observability.startup.log.info"),
            patch("comet.observability.startup.log.warning") as warning,
        ):
            log_cometnet_standalone_configuration(configured)

        self.assertEqual(
            self.generated_secrets(warning),
            {"COMETNET_API_KEY": configured.COMETNET_API_KEY},
        )

    def test_full_startup_summary_satisfies_the_strict_log_contract(self):
        configured = AppSettings(
            _env_file=None,
            BACKGROUND_SCRAPER_ENABLED=True,
            PROXY_DEBRID_STREAM=True,
        )
        configure(
            LoggingSettings(
                _env_file=None,
                LOG_PROFILE="normal",
                LOG_FORMAT="json",
            ),
            process_role="web_master",
            strict=True,
        )
        writes = []
        with patch(
            "comet.observability.logging.os.write",
            side_effect=lambda _fd, payload: writes.append(payload) or len(payload),
        ):
            log_startup_configuration(
                configured,
                workers=2,
                server_name="Gunicorn",
            )

        events = {json.loads(payload)["event"] for payload in writes}
        self.assertIn("config.identity", events)
        self.assertIn("config.network", events)
        self.assertIn("config.background", events)
        self.assertIn("config.generated_secret", events)

    def test_network_summary_counts_every_configured_scraper_proxy(self):
        configured = AppSettings(
            _env_file=None,
            DMM_PROXY_URL="http://dmm-proxy.example:8080",
            USER_PROVIDED_PROXY_URL=(
                "socks5h://proxy-user:proxy-secret@user-proxy.example:1080"
            ),
        )
        with (
            patch("comet.observability.startup.log.info") as info,
            patch("comet.observability.startup.log.warning"),
        ):
            log_startup_configuration(
                configured,
                workers=1,
                server_name="Uvicorn",
            )

        network = next(
            call for call in info.call_args_list if call.args[0] == "config.network"
        )
        self.assertIn("scraper_proxies=1", network.kwargs["details"])
        self.assertIn("user_provided_proxy=configured", network.kwargs["details"])
        self.assertNotIn("proxy-secret", repr(info.call_args_list))

    def test_every_explicit_operator_setting_is_logged_and_secrets_are_redacted(self):
        configured = AppSettings(
            _env_file=None,
            HTTP_CLIENT_LIMIT=75,
            TMDB_READ_ACCESS_TOKEN="secret-token",
        )
        with (
            patch("comet.observability.startup.log.info") as info,
            patch("comet.observability.startup.log.warning"),
            patch(
                "comet.observability.startup.deployment_setting_keys",
                return_value=frozenset({"HTTP_CLIENT_LIMIT", "TMDB_READ_ACCESS_TOKEN"}),
            ),
        ):
            log_startup_configuration(
                configured,
                workers=1,
                server_name="Uvicorn",
            )

        overrides = {
            call.kwargs["setting_name"]: call.kwargs
            for call in info.call_args_list
            if call.args[0] == "config.override"
        }
        self.assertEqual(overrides["HTTP_CLIENT_LIMIT"]["details"], "75")
        self.assertEqual(overrides["TMDB_READ_ACCESS_TOKEN"]["details"], "configured")
        self.assertEqual(overrides["HTTP_CLIENT_LIMIT"]["source_type"], "environment")
        self.assertNotIn("PROMETHEUS_MULTIPROC_DIR", overrides)
        self.assertNotIn("secret-token", repr(info.call_args_list))
