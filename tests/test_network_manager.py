import socket
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import orjson
from aiohttp_socks import ProxyConnector

from comet.core.models import settings
from comet.utils.network_manager import (
    AsyncClientWrapper,
    DiscoveryResponseTooLarge,
    NetworkManager,
    ResponseWrapper,
    _RequestContextManager,
    _retry_delay,
    resolve_proxy_url,
)


class _Content:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


class NetworkManagerContractTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_delays_are_finite_and_bounded(self):
        self.assertEqual(_retry_delay(None, 1.0, 0), 1.0)
        self.assertEqual(_retry_delay(None, 1.0, 20), 60.0)
        self.assertEqual(_retry_delay("0", 1.0, 0), 1.0)
        self.assertEqual(_retry_delay("-1", 1.0, 0), 1.0)
        self.assertIsNone(_retry_delay("61", 1.0, 0))

        for retry_after in ("nan", "inf", object()):
            with self.subTest(retry_after=retry_after):
                delay = _retry_delay(retry_after, 2.0, 20)
                self.assertGreaterEqual(delay, 2.0)
                self.assertLessEqual(delay, 60.0)

    async def test_long_provider_retry_window_returns_the_429_without_sleeping(self):
        response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "50309"},
            encoding="utf-8",
        )

        class Session:
            def __init__(self):
                self.requests = 0

            async def request(self, _method, _url, **_kwargs):
                self.requests += 1
                return response

        session = Session()
        wrapper = SimpleNamespace(
            impersonate="chrome",
            _resolved_proxy_url=None,
            _get_curl_session=AsyncMock(return_value=session),
        )
        request = _RequestContextManager(wrapper, "GET", "https://example.invalid")

        with patch("comet.utils.network_manager.log.warning") as warning:
            result = await request._attempt_request(None)

        self.assertEqual(result.status, 429)
        self.assertEqual(session.requests, 1)
        warning.assert_called_once_with(
            "http.upstream.rate_limited",
            "Upstream request rate limited",
            provider_host="example.invalid",
            http_method="get",
            http_status=429,
            attempt_count=1,
            retryable=False,
        )

    async def test_rate_limit_log_redacts_the_request_url_and_records_the_retry(self):
        rate_limited = SimpleNamespace(
            status_code=429,
            headers={},
            encoding="utf-8",
        )
        recovered = SimpleNamespace(status_code=200, headers={}, encoding="utf-8")

        class Session:
            def __init__(self):
                self.responses = iter((rate_limited, recovered))

            async def request(self, _method, _url, **_kwargs):
                return next(self.responses)

        wrapper = SimpleNamespace(
            impersonate="chrome",
            _resolved_proxy_url=None,
            _get_curl_session=AsyncMock(return_value=Session()),
        )
        request = _RequestContextManager(
            wrapper,
            "GET",
            "https://api.example.test/search?api_key=secret-canary",
        )

        with (
            patch("comet.utils.network_manager.log.warning") as warning,
            patch(
                "comet.utils.network_manager.asyncio.sleep", new=AsyncMock()
            ) as sleep,
        ):
            result = await request._attempt_request(None)

        self.assertEqual(result.status, 200)
        warning.assert_called_once_with(
            "http.upstream.rate_limited",
            "Upstream request rate limited",
            provider_host="api.example.test",
            http_method="get",
            http_status=429,
            attempt_count=1,
            retryable=True,
            backoff_ms=settings.RATELIMIT_RETRY_BASE_DELAY * 1_000,
        )
        sleep.assert_awaited_once_with(settings.RATELIMIT_RETRY_BASE_DELAY)

    def test_unexpected_proxy_resolution_failure_propagates(self):
        proxy_url = "http://user:secret@proxy.internal:8080"
        with (
            patch(
                "comet.utils.network_manager.socket.gethostbyname",
                side_effect=OSError("user:secret"),
            ),
            self.assertRaises(OSError),
        ):
            resolve_proxy_url(proxy_url)

    def test_proxy_dns_failure_leaves_resolution_to_the_http_backend(self):
        proxy_url = "http://user:secret@proxy.internal:8080"
        with patch(
            "comet.utils.network_manager.socket.gethostbyname",
            side_effect=socket.gaierror("unresolved"),
        ):
            self.assertEqual(resolve_proxy_url(proxy_url), proxy_url)

    def test_remote_dns_socks_proxy_keeps_hostname_for_the_proxy(self):
        proxy_url = "socks5h://user:secret@proxy.internal:1080"
        with patch("comet.utils.network_manager.socket.gethostbyname") as resolve:
            self.assertEqual(resolve_proxy_url(proxy_url), proxy_url)
        resolve.assert_not_called()

    def test_https_proxy_keeps_the_hostname_used_for_tls_verification(self):
        proxy_url = "https://user:secret@proxy.internal:8443"
        with patch("comet.utils.network_manager.socket.gethostbyname") as resolve:
            self.assertEqual(resolve_proxy_url(proxy_url), proxy_url)
        resolve.assert_not_called()

    async def test_non_impersonated_clients_build_a_socks_connector(self):
        client = AsyncClientWrapper(
            proxy_url="socks5h://user:secret@proxy.internal:1080"
        )
        try:
            session = await client._get_socks_session(client.proxy_url)
            self.assertIsInstance(session.connector, ProxyConnector)
        finally:
            await client.close()

    async def test_aiohttp_response_body_is_bounded_before_decode(self):
        response = SimpleNamespace(
            headers={},
            content=_Content((b"x" * (8 * 1024 * 1024), b"x")),
            charset=None,
        )
        wrapped = ResponseWrapper(response, "aiohttp")

        with self.assertRaises(DiscoveryResponseTooLarge):
            await wrapped.text()

    async def test_impersonated_response_is_bounded_during_transfer(self):
        class Session:
            async def request(self, _method, _url, **kwargs):
                self.kwargs = kwargs
                kwargs["content_callback"](b"x" * (8 * 1024 * 1024 + 1))

        session = Session()
        wrapper = SimpleNamespace(
            impersonate="chrome",
            _resolved_proxy_url=None,
            _get_curl_session=unittest.mock.AsyncMock(return_value=session),
        )
        request = _RequestContextManager(wrapper, "GET", "https://example.invalid")

        with self.assertRaises(DiscoveryResponseTooLarge):
            await request._attempt_request(None)
        self.assertFalse(session.kwargs["allow_redirects"])

    async def test_aiohttp_discovery_requests_do_not_follow_redirects(self):
        raw_response = SimpleNamespace(status=200, headers={})

        class Context:
            async def __aenter__(self):
                return raw_response

        class Session:
            def request(self, _method, _url, **kwargs):
                self.kwargs = kwargs
                return Context()

        session = Session()
        wrapper = SimpleNamespace(
            impersonate=None,
            _get_aiohttp_session=unittest.mock.AsyncMock(return_value=session),
        )
        request = _RequestContextManager(wrapper, "GET", "https://example.invalid")

        response = await request._attempt_request(None)

        self.assertEqual(response.status, 200)
        self.assertFalse(session.kwargs["allow_redirects"])

    async def test_json_body_is_read_once_and_requires_utf8_json(self):
        content = _Content((b'{"ok":true}',))
        response = SimpleNamespace(
            headers={},
            content=content,
            charset=None,
        )
        wrapped = ResponseWrapper(response, "aiohttp")

        self.assertEqual(await wrapped.json(), {"ok": True})
        self.assertEqual(await wrapped.text(), '{"ok":true}')

        malformed = ResponseWrapper(
            SimpleNamespace(
                headers={},
                content=_Content((b"\xff",)),
                charset=None,
            ),
            "aiohttp",
        )
        with self.assertRaises(orjson.JSONDecodeError):
            await malformed.json()

    async def test_proxy_fallback_handles_transport_errors_not_implementation_errors(
        self,
    ):
        wrapper = SimpleNamespace(
            proxy_url="http://proxy.internal:8080",
            proxy_ethos="on_failure",
            _request_started=Mock(),
            _request_finished=AsyncMock(),
        )
        request = _RequestContextManager(wrapper, "GET", "https://example.invalid")
        request._attempt_request = AsyncMock(
            side_effect=[aiohttp.ClientConnectionError(), "proxied"]
        )

        self.assertEqual(await request.__aenter__(), "proxied")
        self.assertEqual(
            request._attempt_request.await_args_list[1].args,
            (wrapper.proxy_url,),
        )

        request._attempt_request = AsyncMock(
            side_effect=RuntimeError("implementation failure")
        )
        with self.assertRaisesRegex(RuntimeError, "implementation failure"):
            await request.__aenter__()
        request._attempt_request.assert_awaited_once()

    async def test_manager_close_propagates_client_failure(self):
        manager = NetworkManager()
        manager._clients = {
            "broken": SimpleNamespace(
                close=AsyncMock(side_effect=RuntimeError("close failure"))
            )
        }

        with self.assertRaisesRegex(RuntimeError, "close failure"):
            await manager.close_all()

        self.assertEqual(manager._clients, {})

    async def test_manager_uses_the_exact_configured_scraper_proxy(self):
        manager = NetworkManager()
        await manager.close_all()

        with (
            patch.object(settings, "DMM_PROXY_URL", "http://dmm-proxy.internal:8080"),
            patch.object(settings, "GLOBAL_PROXY_URL", "http://global.internal:8080"),
        ):
            client = manager.get_client("DMM")

        self.assertEqual(client.proxy_url, "http://dmm-proxy.internal:8080")
        await manager.close_all()

        with self.assertRaises(KeyError):
            manager.get_client("FutureScraper")

    async def test_explicit_proxy_policy_does_not_inherit_the_global_proxy(self):
        manager = NetworkManager()
        await manager.close_all()

        with (
            patch.object(settings, "USER_PROVIDED_PROXY_URL", None),
            patch.object(settings, "GLOBAL_PROXY_URL", "http://global.internal:8080"),
        ):
            client = manager.get_client(
                "newznab",
                impersonate="chrome",
                proxy_ethos="always",
                proxy_setting="USER_PROVIDED_PROXY_URL",
            )

        self.assertIsNone(client.proxy_url)
        self.assertEqual(client.proxy_ethos, "never")
        await manager.close_all()


if __name__ == "__main__":
    unittest.main()
