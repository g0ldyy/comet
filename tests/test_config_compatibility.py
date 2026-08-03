import base64
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import orjson
from httpx import ASGITransport, AsyncClient

from comet.api import app as app_module
from comet.core.config_codec import encode_configuration_segment
from comet.core.config_validation import config_check

DEVELOPMENT_CONFIGURATOR_DOCUMENT = {
    "maxResultsPerResolution": 12,
    "maxSize": 53_687_091_200.0,
    "cachedOnly": True,
    "sortCachedUncachedTogether": True,
    "removeTrash": False,
    "resultFormat": ["title", "size"],
    "debridServices": [{"service": "realdebrid", "apiKey": "existing-install-key"}],
    "enableTorrent": True,
    "deduplicateStreams": True,
    "scrapeDebridAccountTorrents": True,
    "debridStreamProxyPassword": "proxy-password",
    "languages": {
        "required": ["fr"],
        "allowed": [],
        "exclude": [],
        "preferred": ["en"],
    },
    "resolutions": {"r2160p": False},
    "options": {
        "remove_ranks_under": -5_000.0,
        "allow_english_in_languages": True,
        "remove_unknown_languages": False,
    },
}


def _encode(payload: dict, *, urlsafe: bool = True) -> str:
    encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return encoder(orjson.dumps(payload)).decode().rstrip("=")


class ConfigCompatibilityTests(unittest.TestCase):
    def test_single_debrid_config_normalizes_without_reinstallation(self):
        encoded = base64.b64encode(
            orjson.dumps(
                {
                    "debridService": "realdebrid",
                    "debridApiKey": "existing-install-key",
                }
            )
        ).decode()

        config = config_check(encoded)

        self.assertIsNotNone(config)
        self.assertEqual(
            config["_debridEntries"],
            [{"service": "realdebrid", "apiKey": "existing-install-key"}],
        )
        self.assertFalse(config["_enableTorrent"])

    def test_development_configurator_document_remains_valid(self):
        config = config_check(_encode(DEVELOPMENT_CONFIGURATOR_DOCUMENT, urlsafe=False))

        self.assertIsNotNone(config)
        self.assertNotIn("remove_ranks_under", config["options"])
        self.assertEqual(
            config["rtnSettings"].options.remove_ranks_under,
            -10_000_000_000,
        )
        self.assertEqual(
            config["_debridEntries"],
            [{"service": "realdebrid", "apiKey": "existing-install-key"}],
        )
        self.assertTrue(config["_enableTorrent"])

    def test_removed_rank_threshold_is_not_accepted_in_v2(self):
        payload = {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent"],
            "options": {
                "remove_ranks_under": -5_000,
                "allow_english_in_languages": False,
                "remove_unknown_languages": False,
            },
        }

        self.assertIsNone(config_check(_encode(payload)))

    def test_urlsafe_unpadded_utf8_config_is_accepted(self):
        encoded = (
            base64.urlsafe_b64encode(
                orjson.dumps(
                    {
                        "debridService": "realdebrid",
                        "debridApiKey": "clé-déjà-installée",
                    }
                )
            )
            .decode()
            .rstrip("=")
        )

        config = config_check(encoded)

        self.assertIsNotNone(config)
        self.assertEqual(config["debridApiKey"], "clé-déjà-installée")
        self.assertNotIn("+", encoded)
        self.assertNotIn("/", encoded)
        self.assertNotIn("=", encoded)

    def test_mixed_standard_and_urlsafe_alphabet_is_rejected(self):
        self.assertIsNone(config_check("a+_"))

    def test_v2_disabled_transport_preserves_but_does_not_activate_debrid(self):
        provider_id = str(uuid.uuid4())
        encoded = (
            base64.urlsafe_b64encode(
                orjson.dumps(
                    {
                        "schemaVersion": 2,
                        "enabledTransports": ["usenet"],
                        "accounts": {
                            "account": {
                                "kind": "realdebrid",
                                "apiKey": "secret",
                            }
                        },
                        "playbackProviders": [
                            {
                                "configurationId": provider_id,
                                "displayName": "Dormant",
                                "kind": "realdebrid",
                                "enabled": True,
                                "accountId": "account",
                            }
                        ],
                    }
                )
            )
            .decode()
            .rstrip("=")
        )

        config = config_check(encoded)

        self.assertEqual(config["_debridEntries"], [])
        self.assertFalse(config["_enableTorrent"])

    def test_v2_debrid_account_envelope_is_the_only_secret_copy(self):
        provider_id = str(uuid.uuid4())
        account_id = str(uuid.uuid4())
        encoded = (
            base64.urlsafe_b64encode(
                orjson.dumps(
                    {
                        "schemaVersion": 2,
                        "enabledTransports": ["bittorrent"],
                        "accounts": {
                            account_id: {
                                "kind": "realdebrid",
                                "apiKey": "one-secret-copy",
                            }
                        },
                        "playbackProviders": [
                            {
                                "configurationId": provider_id,
                                "displayName": "Living room",
                                "kind": "realdebrid",
                                "enabled": True,
                                "accountId": account_id,
                            }
                        ],
                    }
                )
            )
            .decode()
            .rstrip("=")
        )

        config = config_check(encoded)

        self.assertEqual(
            config["_debridEntries"],
            [
                {
                    "configurationId": provider_id,
                    "service": "realdebrid",
                    "apiKey": "one-secret-copy",
                }
            ],
        )

    def test_v2_ignores_unused_accounts_and_unconsumed_provider_options(self):
        provider_id = str(uuid.uuid4())
        account_id = str(uuid.uuid4())
        payload = {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent"],
            "accounts": {
                account_id: {
                    "kind": "realdebrid",
                    "apiKey": "account-secret",
                }
            },
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "displayName": "Living room",
                    "kind": "realdebrid",
                    "enabled": True,
                    "accountId": account_id,
                }
            ],
        }

        payload["accounts"]["unused"] = {
            "kind": "torbox",
            "apiKey": "dead-secret",
        }
        encoded = base64.urlsafe_b64encode(orjson.dumps(payload)).decode().rstrip("=")
        self.assertIsNotNone(config_check(encoded))

        del payload["accounts"]["unused"]
        payload["playbackProviders"][0]["options"] = {"apiKey": "conflicting-secret"}
        encoded = base64.urlsafe_b64encode(orjson.dumps(payload)).decode().rstrip("=")
        self.assertIsNotNone(config_check(encoded))

    def test_invalid_rtn_options_are_rejected(self):
        for field, invalid_value in (
            ("allow_english_in_languages", 1),
            ("remove_unknown_languages", "false"),
        ):
            with self.subTest(value=invalid_value):
                encoded = base64.b64encode(
                    orjson.dumps(
                        {
                            "options": {
                                field: invalid_value,
                            },
                        }
                    )
                ).decode()

                self.assertIsNone(config_check(encoded))

    def test_config_json_codec_uses_normal_duplicate_key_semantics(self):
        for document in (
            b'{"maxSize":NaN}',
            b'{"maxSize":Infinity}',
        ):
            encoded = base64.urlsafe_b64encode(document).decode().rstrip("=")
            with self.subTest(document=document):
                self.assertIsNone(config_check(encoded))
        duplicate = (
            base64.urlsafe_b64encode(b'{"maxSize":1,"maxSize":2}').decode().rstrip("=")
        )
        self.assertEqual(
            config_check(duplicate)["maxSize"],
            2.0,
        )

    def test_v2_native_tokens_are_bounded(self):
        payload = {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent"],
            "nativeAccessToken": "é" * 129,
        }

        self.assertIsNone(config_check(_encode(payload)))


class InstalledAddonCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_development_install_url_still_serves_a_valid_manifest(self):
        encoded = _encode(DEVELOPMENT_CONFIGURATOR_DOCUMENT, urlsafe=False)
        with patch(
            "comet.api.endpoints.manifest.eligible_usenet_provider_badges",
            new=AsyncMock(return_value=[]),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_module.app),
                base_url="http://testserver",
            ) as client:
                response = await client.get(f"/{encoded}/manifest.json")

        self.assertEqual(response.status_code, 200, response.text)
        manifest = response.json()
        self.assertNotEqual(manifest["name"], "❌ | Comet")
        self.assertNotIn("OBSOLETE CONFIGURATION", manifest["description"])
        self.assertEqual(manifest["resources"][0]["name"], "stream")

    async def test_compact_install_url_serves_a_valid_manifest(self):
        encoded = encode_configuration_segment(
            orjson.dumps(DEVELOPMENT_CONFIGURATOR_DOCUMENT)
        )
        self.assertTrue(encoded.startswith("z1."))
        with patch(
            "comet.api.endpoints.manifest.eligible_usenet_provider_badges",
            new=AsyncMock(return_value=[]),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_module.app),
                base_url="http://testserver",
            ) as client:
                response = await client.get(f"/{encoded}/manifest.json")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["resources"][0]["name"], "stream")


if __name__ == "__main__":
    unittest.main()
