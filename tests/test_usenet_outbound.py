import unittest
from unittest.mock import AsyncMock, patch

from yarl import URL

from comet.usenet.outbound import (
    OutboundUrlError,
    ValidatedUrl,
    configured_http_origin,
    fetch_http_bytes,
    http_url_with_basic_auth,
    validate_http_url,
    validate_public_http_url,
)


class _Content:
    def __init__(self, body):
        self._body = body

    async def iter_chunked(self, _size):
        yield self._body


class _Response:
    def __init__(self, status, url, *, headers=None, body=b""):
        self.status = status
        self.url = URL(url)
        self.headers = headers or {}
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self._responses)


class PublicUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_allowlist_can_admit_a_configured_private_origin(self):
        validated = await validate_http_url(
            "http://127.0.0.1:8080/release.nzb",
            allowed_private_origins=frozenset({"http://127.0.0.1:8080"}),
        )

        self.assertEqual(validated.origin, "http://127.0.0.1:8080")

    async def test_cross_origin_redirect_strips_origin_credentials(self):
        source = ValidatedUrl(
            "https://addon.example/release",
            "https",
            "addon.example",
            443,
            "https://addon.example:443",
            (),
        )
        target = ValidatedUrl(
            "https://cdn.example/release",
            "https",
            "cdn.example",
            443,
            "https://cdn.example:443",
            (),
        )
        session = _Session(
            (
                _Response(
                    302,
                    source.url,
                    headers={"Location": target.url},
                ),
                _Response(200, target.url, body=b"<nzb/>"),
            )
        )
        with (
            patch(
                "comet.usenet.outbound.validate_http_url",
                AsyncMock(side_effect=[source, target]),
            ),
            patch(
                "comet.usenet.outbound.aiohttp.TCPConnector",
                return_value=object(),
            ),
            patch(
                "comet.usenet.outbound.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            body = await fetch_http_bytes(
                source.url,
                max_bytes=1024,
                headers={"Accept": "application/x-nzb"},
                origin_headers={"Authorization": "Bearer secret"},
                credential_origin=source.origin,
            )

        self.assertEqual(body, b"<nzb/>")
        self.assertEqual(
            session.calls[0][1]["headers"]["Authorization"],
            "Bearer secret",
        )
        self.assertNotIn("Authorization", session.calls[1][1]["headers"])
        self.assertEqual(
            session.calls[1][1]["headers"]["Accept"],
            "application/x-nzb",
        )

    async def test_rejects_userinfo_and_fragments_before_network_access(self):
        for value in (
            "https://user:password@example.test/file.nzb",
            "https://example.test/file.nzb#fragment",
            "https://example.test/file name.nzb",
            "https://example.test/file\x7fname.nzb",
            "file:///tmp/release.nzb",
        ):
            with self.subTest(value=value), self.assertRaises(OutboundUrlError):
                await validate_public_http_url(value)

    async def test_rejects_credentials_and_framing_in_redirect_persistent_headers(self):
        for headers in (
            {"Authorization": "Bearer secret"},
            {"Cookie": "session=secret"},
            {"X-Api-Key": "secret"},
            {"Host": "internal.example"},
            {"Accept-Encoding": "gzip"},
            {"Transfer-Encoding": "chunked"},
            {"X-Test": "safe\r\nAuthorization: Bearer secret"},
        ):
            with self.subTest(headers=headers), self.assertRaises(OutboundUrlError):
                await fetch_http_bytes(
                    "https://example.test/release.nzb",
                    max_bytes=1024,
                    headers=headers,
                )

    async def test_rejects_invalid_fetch_budgets_before_network_access(self):
        for max_bytes, redirects in (
            (True, 3),
            (0, 3),
            (151 * 1024 * 1024, 3),
            (1024, True),
            (1024, -1),
            (1024, 11),
        ):
            with (
                self.subTest(max_bytes=max_bytes, redirects=redirects),
                self.assertRaises(OutboundUrlError),
            ):
                await fetch_http_bytes(
                    "https://example.test/release.nzb",
                    max_bytes=max_bytes,
                    redirects=redirects,
                )

    async def test_http_failure_preserves_bounded_status_for_callers(self):
        target = ValidatedUrl(
            "https://indexer.example/release",
            "https",
            "indexer.example",
            443,
            "https://indexer.example:443",
            (),
        )
        session = _Session((_Response(429, target.url),))
        with (
            patch(
                "comet.usenet.outbound.validate_http_url",
                AsyncMock(return_value=target),
            ),
            patch(
                "comet.usenet.outbound.aiohttp.TCPConnector",
                return_value=object(),
            ),
            patch(
                "comet.usenet.outbound.aiohttp.ClientSession",
                return_value=session,
            ),
        ):
            with self.assertRaises(OutboundUrlError) as raised:
                await fetch_http_bytes(target.url, max_bytes=1024)

        self.assertEqual(raised.exception.http_status, 429)

    async def test_user_destination_proxy_pool_is_used_without_direct_fallback(self):
        target = ValidatedUrl(
            "https://addon.example/release",
            "https",
            "addon.example",
            443,
            "https://addon.example:443",
            (),
        )
        session = _Session((_Response(200, target.url, body=b"<nzb/>"),))
        with (
            patch(
                "comet.usenet.outbound.validate_http_url",
                AsyncMock(return_value=target),
            ),
            patch(
                "comet.usenet.outbound.settings.USER_PROVIDED_PROXY_URL",
                "socks5h://proxy.example:1080",
            ),
            patch(
                "comet.usenet.outbound.http_client_manager.get_user_session",
                AsyncMock(return_value=session),
            ) as get_user_session,
            patch("comet.usenet.outbound.aiohttp.TCPConnector") as connector,
        ):
            body = await fetch_http_bytes(target.url, max_bytes=1024)

        self.assertEqual(body, b"<nzb/>")
        get_user_session.assert_awaited_once_with()
        connector.assert_not_called()

    async def test_rejects_private_literal_addresses(self):
        for value in (
            "http://127.0.0.1/release.nzb",
            "http://[::1]/release.nzb",
            "http://169.254.169.254/latest/meta-data",
        ):
            with self.subTest(value=value), self.assertRaises(OutboundUrlError):
                await validate_public_http_url(value)

    async def test_rejects_an_explicit_zero_port_without_normalizing_it(self):
        with self.assertRaises(OutboundUrlError):
            await validate_public_http_url("https://example.test:0/release.nzb")
        with self.assertRaises(ValueError):
            configured_http_origin("https://example.test:0/base")
        with self.assertRaises(ValueError):
            http_url_with_basic_auth(
                "https://example.test:0/file",
                "user",
                "password",
            )
