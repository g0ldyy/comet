import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import orjson

from comet.debrid.exceptions import DebridAuthError, DebridLinkGenerationError
from comet.debrid.stremthru import (
    StremThru,
    _prepare_cached_torrents,
    batch_parse,
)


class _ResponseContext:
    def __init__(self, payload=None, *, status=200, error=None, text="raw"):
        self.status = status
        self.error = error
        self._body = (
            text.encode()
            if error is not None
            else orjson.dumps(payload if payload is not None else {})
        )
        self.content = self
        self.headers = {
            "Content-Type": "application/json",
            "Content-Encoding": "identity",
            "Content-Length": str(len(self._body)),
        }
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def read(self, _maximum=-1):
        body, self._body = self._body, b""
        return body


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append(("GET", args, kwargs))
        return self.response

    def post(self, *args, **kwargs):
        self.calls.append(("POST", args, kwargs))
        return self.response


class StremThruAvailabilityTests(unittest.TestCase):
    def test_usable_files_do_not_depend_on_remote_status_labels(self):
        responses = [
            {
                "data": {
                    "items": [
                        {
                            "status": "provider-new-state",
                            "hash": "a" * 40,
                            "files": [
                                {"name": "Sample.mkv", "index": 0, "size": 10},
                                {
                                    "name": "folder/First.S01E01.mkv",
                                    "index": 1,
                                    "size": 20,
                                },
                                {"name": "Second.S01E02.MP4", "index": 2, "size": 30},
                            ],
                        },
                        {"status": "downloading", "hash": "b" * 40, "files": []},
                    ]
                }
            },
        ]

        torrents, filenames = _prepare_cached_torrents(
            responses,
            is_offcloud=False,
        )

        self.assertEqual(
            filenames,
            ["Sample.mkv", "First.S01E01.mkv", "Second.S01E02.MP4"],
        )
        self.assertEqual([torrent["info_hash"] for torrent in torrents], ["a" * 40])
        self.assertEqual(
            [filename for _, filename in torrents[0]["files"]],
            filenames,
        )

    def test_malformed_file_exposes_the_provider_payload(self):
        responses = [
            {
                "data": {
                    "items": [
                        {
                            "hash": "a" * 40,
                            "files": [
                                {
                                    "name": "Movie.mkv",
                                    "index": 0,
                                    "size": 42,
                                },
                                {"name": 42},
                            ],
                        }
                    ]
                }
            }
        ]

        with self.assertRaises(KeyError):
            _prepare_cached_torrents(responses, is_offcloud=False)

    def test_store_cache_marker_is_ignored_without_losing_real_files(self):
        responses = [
            {
                "data": {
                    "items": [
                        {
                            "hash": "a" * 40,
                            "files": [
                                {
                                    "index": -1,
                                    "path": "",
                                    "name": "",
                                    "size": 42,
                                    "source": "dl",
                                },
                                {
                                    "index": 0,
                                    "path": "/Movie.2026.mkv",
                                    "name": "Movie.2026.mkv",
                                    "size": 42,
                                    "source": "tb",
                                },
                            ],
                        }
                    ]
                }
            }
        ]

        torrents, filenames = _prepare_cached_torrents(
            responses,
            is_offcloud=False,
        )

        self.assertEqual(filenames, ["Movie.2026.mkv"])
        self.assertEqual(len(torrents), 1)
        self.assertEqual(len(torrents[0]["files"]), 1)

    def test_unusable_empty_name_is_ignored(self):
        responses = [
            {
                "data": {
                    "items": [
                        {
                            "hash": "a" * 40,
                            "files": [
                                {
                                    "index": 0,
                                    "path": "",
                                    "name": "",
                                    "size": 42,
                                }
                            ],
                        }
                    ]
                }
            }
        ]

        torrents, filenames = _prepare_cached_torrents(
            responses,
            is_offcloud=False,
        )

        self.assertEqual(torrents, [])
        self.assertEqual(filenames, [])

    def test_torrents_listing_too_many_files_are_skipped(self):
        def torrent(info_hash, count):
            return {
                "hash": info_hash,
                "files": [
                    {"name": f"Show.S01E{i:02d}.mkv", "index": i, "size": 1}
                    for i in range(1, count + 1)
                ],
            }

        responses = [{"data": {"items": [torrent("a" * 40, 2), torrent("b" * 40, 3)]}}]

        for limit, expected in ((2, ["a" * 40]), (0, ["a" * 40, "b" * 40])):
            with (
                self.subTest(limit=limit),
                patch(
                    "comet.debrid.stremthru.settings.DEBRID_MAX_FILES_PER_TORRENT",
                    limit,
                ),
            ):
                torrents, filenames = _prepare_cached_torrents(
                    responses,
                    is_offcloud=False,
                )

            self.assertEqual([t["info_hash"] for t in torrents], expected)
            self.assertEqual(len(filenames), 2 if limit else 5)

    def test_parser_failure_is_not_converted_to_a_missing_file(self):
        parsed = object()
        with (
            patch(
                "comet.debrid.stremthru.parse",
                side_effect=[ValueError("hostile name"), parsed],
            ),
            patch("comet.debrid.stremthru.ensure_multi_language"),
            self.assertRaisesRegex(ValueError, "hostile name"),
        ):
            batch_parse(["hostile.mkv", "valid.mkv"])


class StremThruResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_instant_items_are_passed_through(self):
        client = StremThru(None, None, None, "debridlink:token", "")
        client.check_premium = AsyncMock()
        client.get_instant = AsyncMock(
            return_value={
                "data": {
                    "items": {},
                }
            }
        )

        self.assertEqual(
            await client.get_availability(["a" * 40], {}, {}, {}),
            [],
        )

    async def test_failed_availability_batch_cancels_and_settles_siblings(self):
        client = StremThru(None, None, None, "debridlink:token", "")
        client.check_premium = AsyncMock()
        second_started = asyncio.Event()
        second_settled = asyncio.Event()
        blocker = asyncio.Future()

        async def get_instant(hashes):
            if len(hashes) == 500:
                await second_started.wait()
                raise DebridLinkGenerationError("provider failure")
            second_started.set()
            try:
                await blocker
            finally:
                second_settled.set()

        client.get_instant = get_instant

        with self.assertRaises(DebridLinkGenerationError):
            await client.get_availability(
                [f"{index:040x}" for index in range(501)],
                {},
                {},
                {},
            )

        self.assertTrue(second_settled.is_set())

    async def test_availability_selects_feature_instead_of_last_sample(self):
        info_hash = "a" * 40
        client = StremThru(None, "tt1234567", "tt1234567", "torbox:token", "")
        client.check_premium = AsyncMock()
        client.get_instant = AsyncMock(
            return_value={
                "data": {
                    "items": [
                        {
                            "hash": info_hash,
                            "files": [
                                {
                                    "index": 1,
                                    "name": "Movie.2026.2160p.WEB-DL-GROUP.mkv",
                                    "size": 19_000,
                                    "media_info": {
                                        "video": {
                                            "codec": "hevc",
                                            "hdr": ["DV"],
                                            "w": 3840,
                                            "h": 2160,
                                        },
                                        "v": 1,
                                    },
                                },
                                {
                                    "index": 11,
                                    "name": "Sample.mkv",
                                    "size": 300,
                                },
                            ],
                        }
                    ]
                }
            }
        )

        with patch(
            "comet.debrid.stremthru.torrent_update_queue.add_torrent_info",
            new=AsyncMock(),
        ) as enqueue:
            files = await client.get_availability([info_hash], {}, {}, {})

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["index"], 1)
        self.assertEqual(files[0]["title"], "Movie.2026.2160p.WEB-DL-GROUP.mkv")
        self.assertEqual(files[0]["media_info"].video.codec, "hevc")
        self.assertEqual(files[0]["media_info"].video.hdr, ("DV",))
        enqueue.assert_awaited_once_with(files[0], "tt1234567")

    async def test_store_json_response_closes_after_complete_payload_read(self):
        response = _ResponseContext({"data": {"value": "complete"}})
        session = _Session(response)
        client = StremThru(session, None, None, "realdebrid:token", "")

        payload = await client._post_store_json("/endpoint", {}, "read store")

        self.assertEqual(payload, {"data": {"value": "complete"}})
        self.assertTrue(response.exited)
        method, _args, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")

    async def test_store_json_accepts_complete_large_provider_response(self):
        payload = {"data": {"value": "x" * (1024 * 1024)}}
        client = StremThru(
            _Session(_ResponseContext(payload)),
            None,
            None,
            "torbox:token",
            "",
        )

        _status, received = await client._request_store_json(
            "GET",
            "/magnets/check",
            action="check instant availability",
        )

        self.assertEqual(received, payload)

    async def test_premium_response_closes_on_auth_error(self):
        response = _ResponseContext({"error": "invalid"})
        client = StremThru(_Session(response), None, None, "realdebrid:token", "")

        with self.assertRaises(DebridAuthError):
            await client.check_premium()

        self.assertTrue(response.exited)

    async def test_account_check_accepts_extended_active_subscription_status(self):
        response = _ResponseContext({"data": {"subscription_status": "active"}})
        client = StremThru(_Session(response), None, None, "realdebrid:token", "")

        await client.check_premium()

        self.assertTrue(response.exited)

    async def test_account_check_does_not_hide_implementation_errors(self):
        client = StremThru(_Session(None), None, None, "realdebrid:token", "")
        error = TypeError("broken account decoder")
        client._request_store_json = AsyncMock(side_effect=error)

        with self.assertRaises(TypeError) as raised:
            await client.check_premium()

        self.assertIs(raised.exception, error)

    async def test_instant_response_closes_on_json_error(self):
        response = _ResponseContext(error=ValueError("invalid JSON"))
        client = StremThru(_Session(response), None, None, "realdebrid:token", "")

        with self.assertRaises(DebridLinkGenerationError):
            await client.get_instant(["a" * 40])
        self.assertTrue(response.exited)

    async def test_magnet_list_response_exposes_invalid_payload(self):
        response = _ResponseContext({"data": None})
        client = StremThru(_Session(response), None, None, "realdebrid:token", "")

        with self.assertRaises(TypeError):
            await client.list_magnets()
        self.assertTrue(response.exited)

    async def test_magnet_list_requires_a_complete_bounded_native_page(self):
        payload = {
            "data": {
                "items": [
                    {
                        "id": "remote-id",
                        "hash": "a" * 40,
                        "name": "Movie.mkv",
                        "size": 42,
                        "status": "cached",
                        "added_at": "2026-07-28T12:00:00+00:00",
                        "future": "ignored",
                    }
                ],
                "total_items": 1,
                "future": "ignored",
            },
            "future": "ignored",
        }
        session = _Session(_ResponseContext(payload))
        client = StremThru(session, None, None, "realdebrid:token", "")

        items, total = await client.list_magnets(limit=2, offset=0)

        self.assertEqual(total, 1)
        self.assertEqual(
            items,
            [
                {
                    "id": "remote-id",
                    "hash": "a" * 40,
                    "name": "Movie.mkv",
                    "size": 42,
                    "status": "cached",
                    "added_at": "2026-07-28T12:00:00+00:00",
                    "future": "ignored",
                }
            ],
        )
        _method, _args, kwargs = session.calls[0]
        self.assertEqual(
            kwargs["params"],
            {"limit": 2, "offset": 0, "client_ip": ""},
        )

    async def test_magnet_list_preserves_unknown_statuses(self):
        payload = {
            "data": {
                "items": [
                    {
                        "id": "remote-id",
                        "hash": "a" * 40,
                        "name": "Movie.mkv",
                        "size": 42,
                        "status": "Provider-New-State",
                        "added_at": "2026-07-28T12:00:00+00:00",
                    }
                ],
                "total_items": 1,
            }
        }
        client = StremThru(
            _Session(_ResponseContext(payload)),
            None,
            None,
            "realdebrid:token",
            "",
        )

        items, _total = await client.list_magnets()

        self.assertEqual(items[0]["status"], "Provider-New-State")

    async def test_instant_hash_is_forwarded_without_revalidation(self):
        session = _Session(_ResponseContext({}))
        client = StremThru(session, None, None, "realdebrid:token", "")

        self.assertEqual(await client.get_instant(["A" * 40]), {})

        self.assertEqual(len(session.calls), 1)

    async def test_store_error_keeps_only_bounded_codes(self):
        client = StremThru(
            _Session(
                _ResponseContext(
                    {
                        "error": {
                            "code": "STORE_MAGNET_INVALID",
                            "message": "secret-bearing remote text",
                            "__upstream_cause__": {
                                "code": "MAGNET_INVALID",
                                "detail": "secret-bearing detail",
                            },
                        }
                    },
                    status=400,
                )
            ),
            None,
            None,
            "realdebrid:token",
            "",
        )

        with self.assertRaises(DebridLinkGenerationError) as raised:
            await client._post_store_json("/endpoint", {}, "read store")

        self.assertEqual(
            raised.exception.status_keys,
            ["MAGNET_INVALID", "STORE_MAGNET_INVALID"],
        )
        self.assertNotIn("secret-bearing", str(raised.exception))
        self.assertFalse(hasattr(raised.exception, "payload"))

    def test_configuration_is_not_revalidated_at_runtime(self):
        with patch(
            "comet.debrid.stremthru.settings.STREMTHRU_URL",
            "https://user@stremthru.example",
        ):
            client = StremThru(None, None, None, "realdebrid:bad\nkey", "")

        self.assertEqual(client.base_url, "https://user@stremthru.example/v0/store")
        self.assertEqual(client.store_token, "bad\nkey")

    async def test_download_generation_accepts_native_sentinel_and_additive_fields(
        self,
    ):
        client = StremThru(None, None, None, "realdebrid:token", "")
        client._post_store_json = AsyncMock(
            side_effect=[
                {
                    "data": {
                        "status": "provider-new-state",
                        "files": [
                            {
                                "index": -1,
                                "name": "The.Sample.2026.mkv",
                                "size": 42,
                                "link": "provider-locked-link",
                                "future": "ignored",
                            }
                        ],
                        "future": "ignored",
                    },
                    "future": "ignored",
                },
                {
                    "data": {
                        "link": "https://download.test/video?token=short",
                        "future": "ignored",
                    },
                    "future": "ignored",
                },
            ]
        )

        result = await client.generate_download_link(
            "a" * 40,
            "n",
            "The Sample",
            "The.Sample.2026.mkv",
            None,
            None,
        )

        self.assertEqual(
            result,
            "https://download.test/video?token=short",
        )
        self.assertEqual(client._post_store_json.await_count, 2)
        self.assertEqual(
            client._post_store_json.await_args_list[0].args,
            (
                "/magnets",
                {
                    "magnet": (
                        "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=The.Sample.2026.mkv"
                    )
                },
                "add torrent to store",
            ),
        )
        self.assertEqual(
            client._post_store_json.await_args_list[1].args,
            (
                "/link/generate",
                {"link": "provider-locked-link"},
                "generate download link",
            ),
        )

    async def test_download_generation_rejects_sample_for_feature_release(self):
        client = StremThru(None, None, None, "torbox:token", "")
        client._post_store_json = AsyncMock(
            return_value={
                "data": {
                    "files": [
                        {
                            "index": 11,
                            "name": "Sample.mkv",
                            "size": 300,
                            "link": "provider-sample-link",
                        }
                    ]
                }
            }
        )

        with self.assertRaises(DebridLinkGenerationError) as raised:
            await client.generate_download_link(
                "a" * 40,
                "11",
                "Sample.mkv",
                "Movie.2026.2160p.WEB-DL-GROUP",
                None,
                None,
            )

        self.assertEqual(raised.exception.upstream_error_code, "MEDIA_NOT_CACHED_YET")
        client._post_store_json.assert_awaited_once()

    async def test_download_generation_honors_the_previously_selected_file(self):
        client = StremThru(None, None, None, "torbox:token", "")
        client._post_store_json = AsyncMock(
            side_effect=[
                {
                    "data": {
                        "files": [
                            {
                                "index": 1,
                                "name": "Movie.2026.1080p.mkv",
                                "size": 10_000,
                                "link": "selected-link",
                                "media_info": {
                                    "video": {"codec": "hevc"},
                                    "v": 1,
                                },
                            },
                            {
                                "index": 2,
                                "name": "Movie.2026.1080p.Alternate.mkv",
                                "size": 20_000,
                                "link": "larger-link",
                            },
                        ]
                    }
                },
                {"data": {"link": "https://download.test/selected"}},
            ]
        )

        with patch("comet.debrid.stremthru.schedule_cache_availability") as cache:
            result = await client.generate_download_link(
                "a" * 40,
                "1",
                "Movie",
                "Movie.2026.1080p.mkv",
                None,
                None,
            )

        self.assertEqual(result, "https://download.test/selected")
        self.assertEqual(
            client._post_store_json.await_args_list[1].args[1],
            {"link": "selected-link"},
        )
        cached = cache.call_args.args[1]
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["index"], 1)
        self.assertEqual(cached[0]["media_info"].video.codec, "hevc")

    async def test_unexpected_link_error_is_not_hidden(self):
        client = StremThru(None, None, None, "realdebrid:token", "")
        error = RuntimeError("transport failed")
        with (
            patch.object(
                client,
                "_post_store_json",
                new=AsyncMock(side_effect=error),
            ),
            self.assertRaises(RuntimeError) as raised,
        ):
            await client.generate_download_link(
                "a" * 40,
                "0",
                "Movie.mkv",
                "Movie",
                None,
                None,
            )

        self.assertIs(raised.exception, error)
