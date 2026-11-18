import asyncio
import json
import socket
import unittest
from unittest.mock import patch

from aiohttp import ClientConnectorError
from aiohttp.test_utils import TestServer
from aiohttp.web import Application, HTTPTemporaryRedirect, Response
from aiohttp_socks import ProxyConnectionError
from mediaflow_proxy.configs import TransportConfig
from mediaflow_proxy.utils.http_client import (
    get_routing_config,
    initialize_routing_from_config,
)

from comet.cometnet.utils import check_advertise_url_reachability
from comet.core.models import settings
from comet.debrid.stremthru import StremThru
from comet.metadata.imdb import _get_cinemeta_metadata
from comet.services.streaming.wrapper import configure_stream_proxy
from comet.utils.http_client import HttpClientManager
from comet.utils.network_manager import AsyncClientWrapper


class ProxyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_socks5_routes_metadata_debrid_scrapers_and_websocket_probe(
        self,
    ):
        requests = []

        async def proxy_request(reader, writer):
            try:
                version, methods = await reader.readexactly(2)
                self.assertEqual(version, 5)
                self.assertIn(0, await reader.readexactly(methods))
                writer.write(b"\x05\x00")
                await writer.drain()
                self.assertEqual(await reader.readexactly(4), b"\x05\x01\x00\x03")
                length = (await reader.readexactly(1))[0]
                hostname = (await reader.readexactly(length)).decode()
                await reader.readexactly(2)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
                await writer.drain()

                request_headers = await reader.readuntil(b"\r\n\r\n")
                method, path, _ = request_headers.split(b"\r\n", 1)[0].decode().split()
                requests.append((hostname, method, path))
                if hostname == "v3-cinemeta.strem.io":
                    status = (
                        "302 Found\r\nLocation: http://cinemeta-live.strem.io" + path
                    )
                    body = b""
                else:
                    status = "200 OK"
                    payload = {
                        "cinemeta-live.strem.io": {
                            "meta": {"name": "Example", "year": "2026"}
                        },
                        "stremthru.13377001.xyz": {
                            "data": {"subscription_status": "premium"}
                        },
                    }.get(hostname, {"ok": True})
                    body = json.dumps(payload).encode()
                writer.write(
                    f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(proxy_request, "127.0.0.1", 0) as proxy:
            proxy_port = proxy.sockets[0].getsockname()[1]
            manager = HttpClientManager()
            self.addAsyncCleanup(manager.close)
            with (
                patch.object(
                    settings, "GLOBAL_PROXY_URL", f"socks5://localhost:{proxy_port}"
                ),
                patch.object(settings, "PROXY_ETHOS", "always"),
                patch.object(
                    settings, "STREMTHRU_URL", "http://stremthru.13377001.xyz"
                ),
                patch(
                    "comet.metadata.imdb._CINEMETA_META_URL",
                    "http://v3-cinemeta.strem.io/meta/{media_type}/{id}.json",
                ),
                patch.dict("os.environ", {"NO_PROXY": "*", "no_proxy": "*"}),
            ):
                session = await manager.get_session()
                self.assertEqual(
                    await _get_cinemeta_metadata(session, "tt1234567", "movie"),
                    ("Example", 2026, None),
                )
                debrid = StremThru(
                    session, "tt1234567", "tt1234567", "rd:test", "127.0.0.1"
                )
                await debrid.check_premium()
                for impersonate in (None, "chrome"):
                    client = AsyncClientWrapper("TestScraper", impersonate=impersonate)
                    try:
                        async with client.get(
                            "http://scraper.invalid/search"
                        ) as response:
                            self.assertEqual(await response.json(), {"ok": True})
                    finally:
                        await client.close()
                self.assertEqual(
                    await check_advertise_url_reachability("ws://cometnet.invalid/ws"),
                    (False, "Server returned HTTP 200 instead of WebSocket upgrade"),
                )

        self.assertEqual(
            requests,
            [
                ("v3-cinemeta.strem.io", "GET", "/meta/movie/tt1234567.json"),
                ("cinemeta-live.strem.io", "GET", "/meta/movie/tt1234567.json"),
                ("stremthru.13377001.xyz", "GET", "/v0/store/user?client_ip=127.0.0.1"),
                ("scraper.invalid", "GET", "/search"),
                ("scraper.invalid", "GET", "/search"),
                ("cometnet.invalid", "GET", "/ws"),
            ],
        )

    async def test_proxy_modes_and_scraper_override(self):
        hits = []

        async def origin(request):
            hits.append(request.raw_path)
            return Response(text="direct")

        app = Application()
        app.router.add_get("/", origin)
        with socket.socket() as unavailable_proxy:
            unavailable_proxy.bind(("127.0.0.1", 0))
            proxy_url = f"socks5://127.0.0.1:{unavailable_proxy.getsockname()[1]}"
            async with TestServer(app) as server:
                for ethos in ("always", "never"):
                    manager = HttpClientManager()
                    self.addAsyncCleanup(manager.close)
                    with (
                        patch.object(settings, "PROXY_ETHOS", ethos),
                        patch.object(settings, "GLOBAL_PROXY_URL", proxy_url),
                    ):
                        session = await manager.get_session()
                        if ethos == "always":
                            with self.assertRaises(ProxyConnectionError):
                                async with session.get(server.make_url("/")):
                                    self.fail("Broken proxy should block the request")
                            self.assertEqual(hits, [])
                        else:
                            async with session.get(server.make_url("/")) as response:
                                self.assertEqual(await response.text(), "direct")

                with (
                    patch.object(settings, "PROXY_ETHOS", "always"),
                    patch.object(settings, "GLOBAL_PROXY_URL", proxy_url),
                    patch.dict(
                        "os.environ", {"OVERRIDE_PROXY_URL": str(server.make_url("/"))}
                    ),
                ):
                    client = AsyncClientWrapper("Override")
                    self.addAsyncCleanup(client.close)
                    async with client.get("http://override.invalid/") as response:
                        self.assertEqual(await response.text(), "direct")
        self.assertEqual(hits, ["/", "http://override.invalid/"])

    async def test_on_failure_retries_reads_and_does_not_replay_writes(self):
        proxy_requests = []
        writes = []

        async def proxy(request):
            proxy_requests.append(request.raw_path)
            return Response(text="proxied")

        async def accept_write(request):
            writes.append(await request.json())
            raise HTTPTemporaryRedirect(location=origin_url)

        proxy_app = Application()
        proxy_app.router.add_route("*", "/{path:.*}", proxy)
        origin_app = Application()
        origin_app.router.add_post("/write", accept_write)
        manager = HttpClientManager()
        self.addAsyncCleanup(manager.close)
        with socket.socket() as unavailable_origin:
            unavailable_origin.bind(("127.0.0.1", 0))
            origin_url = f"http://127.0.0.1:{unavailable_origin.getsockname()[1]}/test"
            async with (
                TestServer(proxy_app) as proxy_server,
                TestServer(origin_app) as origin_server,
            ):
                with (
                    patch.object(settings, "PROXY_ETHOS", "on_failure"),
                    patch.object(
                        settings,
                        "GLOBAL_PROXY_URL",
                        str(proxy_server.make_url("/")),
                    ),
                ):
                    session = await manager.get_session()
                    async with session.get(origin_url) as response:
                        self.assertEqual(await response.text(), "proxied")
                    with self.assertRaises(ClientConnectorError):
                        async with session.post(
                            origin_server.make_url("/write"), json={"value": 1}
                        ):
                            self.fail("The redirect destination is unavailable")
        self.assertEqual(writes, [{"value": 1}])
        self.assertEqual(proxy_requests, [origin_url])

    def test_mediaflow_always_uses_the_global_proxy_for_every_route(self):
        config = TransportConfig(
            proxy_url="http://other:8080",
            transport_routes={
                "https://stream.example": {"proxy": False, "verify_ssl": False}
            },
        )
        from mediaflow_proxy.configs import settings as mediaflow_settings

        self.addCleanup(
            initialize_routing_from_config, mediaflow_settings.transport_config
        )
        with (
            patch.object(settings, "PROXY_ETHOS", "always"),
            patch.object(settings, "GLOBAL_PROXY_URL", "socks5://proxy:1080"),
            patch.object(mediaflow_settings, "transport_config", config),
        ):
            configure_stream_proxy()

        routing = get_routing_config()
        self.assertEqual(
            routing.match_url("https://elsewhere.test").proxy_url,
            "socks5h://proxy:1080",
        )
        explicit = routing.match_url("https://stream.example")
        self.assertEqual(explicit.proxy_url, "socks5h://proxy:1080")
        self.assertFalse(explicit.verify_ssl)
