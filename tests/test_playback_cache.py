import unittest
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from comet.api.endpoints.playback import (
    _build_playback_media_id,
    _parse_playback_path,
    _valid_download_url,
    playback,
)
from comet.debrid.link_cache import (
    cache_download_link_best_effort,
    get_cached_download_link,
)


class PlaybackCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_configuration_rejects_the_numeric_legacy_playback_route(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/playback/legacy",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 1234),
            }
        )
        with (
            patch(
                "comet.api.endpoints.playback.config_check",
                return_value={"schemaVersion": 2},
            ),
            patch(
                "comet.api.endpoints.playback.get_debrid_credentials",
            ) as credentials,
            patch(
                "comet.api.endpoints.playback.database.fetch_one",
                AsyncMock(),
            ) as database_read,
        ):
            response = await playback(
                request,
                "config",
                "a" * 40,
                "0",
                "n",
                "n",
                "n",
                torrent_name="Movie",
                name="Movie",
                media_id=None,
                media_type=None,
            )

        self.assertEqual(response.status_code, 200)
        credentials.assert_not_called()
        database_read.assert_not_awaited()

    async def test_v1_configuration_retains_one_release_numeric_route(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/playback/legacy",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 1234),
            }
        )
        config = {
            "schemaVersion": 1,
            "debridStreamProxyPassword": "",
        }
        with (
            patch(
                "comet.api.endpoints.playback.config_check",
                return_value=config,
            ),
            patch(
                "comet.api.endpoints.playback.get_debrid_credentials",
                return_value=("realdebrid", "legacy-key"),
            ) as credentials,
            patch(
                "comet.api.endpoints.playback.http_client_manager.get_session",
                AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.playback.get_cached_download_link",
                AsyncMock(return_value="https://download.example/video"),
            ),
            patch(
                "comet.api.endpoints.playback.settings.PROXY_DEBRID_STREAM",
                False,
            ),
        ):
            response = await playback(
                request,
                "config",
                "a" * 40,
                "0",
                "n",
                "n",
                "n",
                torrent_name="Movie",
                name="Movie",
                media_id=None,
                media_type=None,
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["location"],
            "https://download.example/video",
        )
        credentials.assert_called_once_with(config, 0)

    async def test_legacy_playback_rejects_oversized_query_state_before_io(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "https",
                "server": ("comet.example", 443),
                "path": "/config/playback/legacy",
                "query_string": b"",
                "headers": (),
                "client": ("127.0.0.1", 1234),
            }
        )
        with (
            patch(
                "comet.api.endpoints.playback.config_check",
                return_value={"schemaVersion": 1},
            ),
            patch(
                "comet.api.endpoints.playback.get_debrid_credentials",
            ) as credentials,
            patch(
                "comet.api.endpoints.playback.database.fetch_one",
                AsyncMock(),
            ) as database_read,
        ):
            response = await playback(
                request,
                "config",
                "a" * 40,
                "0",
                "n",
                "n",
                "n",
                torrent_name="x" * 2_049,
                name="Movie",
                media_id=None,
                media_type=None,
            )

        self.assertEqual(response.status_code, 200)
        credentials.assert_not_called()
        database_read.assert_not_awaited()

    def test_playback_media_id_preserves_aggregate_series_scopes(self):
        self.assertEqual(
            _build_playback_media_id("tt1234567", "series", None, None),
            "tt1234567",
        )
        self.assertEqual(
            _build_playback_media_id("tt1234567", "series", 2, None),
            "tt1234567:2",
        )
        self.assertEqual(
            _build_playback_media_id("tt1234567", "series", 2, 3),
            "tt1234567:2:3",
        )
        self.assertEqual(
            _build_playback_media_id("tt1234567", "movie", None, None),
            "tt1234567",
        )

    async def test_cache_write_failure_is_observable_without_discarding_the_link(self):
        database = type(
            "Database",
            (),
            {
                "execute": AsyncMock(
                    side_effect=RuntimeError(
                        "database rejected https://download.test/?token=secret"
                    )
                )
            },
        )()
        with patch("comet.debrid.link_cache.log.warning") as warning:
            await cache_download_link_best_effort(
                database,
                debrid_service="realdebrid",
                account_key_hash="account",
                info_hash="a" * 40,
                season=None,
                episode=None,
                selection_key="7",
                client_ip="203.0.113.7",
                download_url="https://download.test/video",
            )

        database.execute.assert_awaited_once()
        warning.assert_called_once()

    def test_playback_path_requires_current_canonical_scope(self):
        self.assertEqual(
            _parse_playback_path("a" * 40, "2", "n", "1", "0"),
            ("a" * 40, 2, "n", 1, 0),
        )

        invalid_paths = (
            ("A" * 40, "2", "n", "1", "0"),
            ("a" * 39, "2", "n", "1", "0"),
            ("a" * 40, "n", "n", "1", "0"),
            ("a" * 40, "02", "n", "1", "0"),
            ("a" * 40, "2", "-1", "1", "0"),
            ("a" * 40, "2", "n", "bad", "0"),
            ("a" * 40, "2", "n", "1", "+1"),
            ("a" * 40, "2", "n", "1", str(2**63)),
            ("a" * 40, "2", "n", "1", "1" * 20),
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                _parse_playback_path(*path)

    def test_download_urls_require_absolute_http_current_shape(self):
        valid = "https://download.test/video?token=secret"
        self.assertEqual(_valid_download_url(valid), valid)

        for value in (
            None,
            42,
            "",
            "/relative/video",
            "javascript:alert(1)",
            "https://",
            "https://download.test:invalid/video",
            "https://download.test/video\r\nX-Injected: yes",
            "https://user@download.test/video",
            "https://download.test/video#token",
            "https://download.test/video%ZZ",
            "https://download.test\\@other.test/video",
            "https://download.test/video path",
            "https://download.test/video\x7f",
            "https://download.test/" + "x" * 8192,
        ):
            with self.subTest(value=value):
                self.assertIsNone(_valid_download_url(value))

    async def test_link_cache_keys_file_selection_and_client_scope(self):
        database = type(
            "Database",
            (),
            {
                "fetch_one": AsyncMock(
                    return_value={
                        "download_url": "https://download.test/video?token=short"
                    }
                ),
                "execute": AsyncMock(),
            },
        )()
        arguments = {
            "debrid_service": "realdebrid",
            "account_key_hash": "b" * 64,
            "info_hash": "a" * 40,
            "season": None,
            "episode": None,
            "selection_key": "7",
            "client_ip": "203.0.113.7",
        }

        cached = await get_cached_download_link(database, **arguments)
        await cache_download_link_best_effort(
            database,
            **arguments,
            download_url="https://download.test/video?token=short",
        )

        self.assertEqual(cached, "https://download.test/video?token=short")
        read_values = database.fetch_one.await_args.args[1]
        write_values = database.execute.await_args.args[1]
        self.assertEqual(read_values["selection_key"], "7")
        self.assertEqual(read_values["client_scope"], write_values["client_scope"])
        self.assertNotEqual(read_values["client_scope"], arguments["client_ip"])
