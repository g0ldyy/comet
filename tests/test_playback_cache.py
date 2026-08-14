import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from comet.api.endpoints.playback import (
    SeriousSportSyncResolverError,
    _build_playback_media_id,
    _cache_download_link_safely,
    _decode_sources,
    _parse_playback_path,
    _resolve_playback_metadata,
    _valid_download_url,
)


class PlaybackCacheTests(unittest.IsolatedAsyncioTestCase):
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

    def test_sources_require_current_string_list_schema(self):
        self.assertEqual(
            _decode_sources(b'["tracker:first", null, "", 42, "tracker:second"]'),
            ["tracker:first", "tracker:second"],
        )
        self.assertEqual(_decode_sources(b"not-json"), [])
        self.assertEqual(_decode_sources(b'{"tracker": "first"}'), [])

    async def test_serioussportsync_playback_uses_event_metadata(self):
        event = SimpleNamespace(
            search_titles=("SummerSlam Sunday", "WWE SummerSlam Sunday")
        )
        with (
            patch(
                "comet.api.endpoints.playback.resolve_serioussportsync_event",
                new=AsyncMock(return_value=event),
            ) as resolve,
            patch(
                "comet.api.endpoints.playback.MetadataScraper.fetch_metadata_and_aliases",
                new=AsyncMock(),
            ) as ordinary_metadata,
        ):
            video_id, media_only_id, aliases = await _resolve_playback_metadata(
                object(),
                {
                    "seriousSportsSyncManifestUrl": (
                        "https://sports.example/user-token/manifest.json"
                    )
                },
                "wwe:2185907",
                "movie",
                None,
                None,
            )

        self.assertEqual(video_id, "wwe:2185907")
        self.assertEqual(media_only_id, "wwe:2185907")
        self.assertEqual(
            aliases, {"ez": ["SummerSlam Sunday", "WWE SummerSlam Sunday"]}
        )
        resolve.assert_awaited_once()
        ordinary_metadata.assert_not_awaited()

    async def test_serioussportsync_playback_survives_alias_refresh_failure(self):
        with patch(
            "comet.api.endpoints.playback.resolve_serioussportsync_event",
            new=AsyncMock(
                side_effect=SeriousSportSyncResolverError("temporary failure")
            ),
        ):
            video_id, media_only_id, aliases = await _resolve_playback_metadata(
                object(),
                {
                    "seriousSportsSyncManifestUrl": (
                        "https://sports.example/user-token/manifest.json"
                    )
                },
                "wwe:2185907",
                "movie",
                None,
                None,
            )

        self.assertEqual(video_id, "wwe:2185907")
        self.assertEqual(media_only_id, "wwe:2185907")
        self.assertEqual(aliases, {})

    async def test_cache_write_failure_does_not_discard_generated_link(self):
        with (
            patch(
                "comet.api.endpoints.playback.cache_download_link",
                new=AsyncMock(
                    side_effect=RuntimeError(
                        "database rejected https://download.test/?token=secret"
                    )
                ),
            ) as cache,
            patch("comet.api.endpoints.playback.logger.warning") as warning,
        ):
            await _cache_download_link_safely(
                debrid_service="realdebrid",
                account_key_hash="account",
                info_hash="a" * 40,
                season=None,
                episode=None,
                download_url="https://download.test/video",
            )

        cache.assert_awaited_once()
        message = warning.call_args.args[0]
        self.assertIn("RuntimeError", message)
        self.assertNotIn("token=secret", message)

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
        ):
            with self.subTest(value=value):
                self.assertIsNone(_valid_download_url(value))
