import unittest

import aiohttp
from aiohttp_socks import ProxyConnector

from comet.core.models import AppSettings, settings
from comet.utils.http_client import HttpClientManager


class HttpClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_initialization_is_idempotent_and_closes_cleanly(self):
        manager = HttpClientManager()
        try:
            first = await manager.init()
            second = await manager.get_session()

            self.assertIs(first, second)
        finally:
            await manager.close()

        self.assertIsNone(manager._session)

    async def test_replacement_drains_the_leased_generation(self):
        manager = HttpClientManager()
        first = await manager.init()
        replacement = manager.build(settings)
        try:
            async with manager.bind() as leased:
                await manager.replace(replacement)
                self.assertIs(leased, first)
                self.assertFalse(first.closed)
                self.assertIs(await manager.get_session(), first)

            self.assertTrue(first.closed)
            self.assertIs(await manager.get_session(), replacement)
        finally:
            await manager.close()

    async def test_user_destination_http_pool_is_strict_and_cookie_free(self):
        manager = HttpClientManager()
        session = manager.build_user(
            AppSettings(
                _env_file=None,
                USER_PROVIDED_PROXY_URL="http://proxy.example:8080",
            )
        )
        try:
            self.assertEqual(str(session._default_proxy), "http://proxy.example:8080")
            self.assertIsInstance(session.cookie_jar, aiohttp.DummyCookieJar)
        finally:
            await session.close()

    async def test_user_destination_socks_pool_uses_remote_dns(self):
        manager = HttpClientManager()
        session = manager.build_user(
            AppSettings(
                _env_file=None,
                USER_PROVIDED_PROXY_URL="socks5h://proxy.example:1080",
            )
        )
        try:
            self.assertIsInstance(session.connector, ProxyConnector)
            self.assertIsNone(session._default_proxy)
            self.assertIsInstance(session.cookie_jar, aiohttp.DummyCookieJar)
        finally:
            await session.close()
