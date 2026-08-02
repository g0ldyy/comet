import unittest
from urllib.parse import unquote, urlsplit

from comet.playback.base import Actionability, Readiness
from comet.playback.providers.easynews import EasynewsProvider, direct_target


class EasynewsProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.locator = {
            "downURL": "https://attacker.invalid/dl",
            "dlFarm": "farm-1",
            "dlPort": 443,
            "hash": "aBc_123",
            "id": "file-1",
            "extension": "mkv",
            "filename": "Episode 01",
        }

    async def test_provider_is_direct_and_validates_credentials(self):
        provider = EasynewsProvider(None, "member", "secret")

        status = await provider.validate_config({})

        self.assertEqual(status.readiness, Readiness.UNKNOWN)
        self.assertEqual(status.actionability, Actionability.SERVER_ON_DEMAND)
        self.assertEqual(
            {path.value for path in provider.descriptor.byte_paths},
            {"cloud_redirect"},
        )

    def test_direct_target_uses_only_the_fixed_member_origin(self):
        self.assertEqual(
            direct_target(self.locator),
            "https://members.easynews.com/dl/farm-1/443/"
            "aBc_123file-1.mkv/Episode%2001.mkv",
        )
        self.assertEqual(
            direct_target({**self.locator, "id": ""}),
            "https://members.easynews.com/dl/farm-1/443/aBc_123.mkv/Episode%2001.mkv",
        )

    def test_direct_target_quotes_provider_path_fields(self):
        target = direct_target(
            {
                **self.locator,
                "hash": "../escape",
                "filename": "bad\x00name",
                "extension": "m/kv",
            }
        )
        self.assertIn("..%2Fescape", target)
        self.assertIn("bad%00name.m%2Fkv", target)

    def test_playback_url_is_authenticated_without_network_access(self):
        provider = EasynewsProvider(
            object(),
            "member@example.com",
            "p@ss/word",
        )

        target = urlsplit(provider.playback_url(self.locator))

        self.assertEqual(target.scheme, "https")
        self.assertEqual(target.hostname, "members.easynews.com")
        self.assertEqual(unquote(target.username), "member@example.com")
        self.assertEqual(unquote(target.password), "p@ss/word")
        self.assertNotIn("attacker.invalid", target.geturl())

    def test_options_require_both_credentials(self):
        self.assertTrue(
            EasynewsProvider.has_valid_options(
                {"username": "member", "password": "secret"}
            )
        )
        self.assertFalse(EasynewsProvider.has_valid_options({"username": "member"}))
