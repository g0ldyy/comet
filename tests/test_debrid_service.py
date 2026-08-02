import unittest
from unittest.mock import AsyncMock, patch

from comet.services.debrid import DebridService
from comet.utils.parsing import MediaScope


class DebridServiceCacheTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_cached_file_index_is_not_converted_to_absence(self):
        with self.assertRaisesRegex(ValueError, "file index"):
            DebridService._build_torrent_update(
                {},
                file_index="not-an-index",
                title=None,
                size=None,
                parsed=None,
            )

    async def test_live_season_scope_uses_episode_files_only_as_cache_evidence(self):
        info_hash = "a" * 40
        torrents = {
            info_hash: {
                "title": "Show.S02.COMPLETE.1080p.mkv",
                "size": 1_000,
                "fileIndex": None,
                "parsed": None,
            }
        }
        availability = [
            {
                "info_hash": info_hash,
                "index": 1,
                "title": "Show.S02E01.1080p.mkv",
                "size": 100,
                "season": 2,
                "episode": 1,
                "parsed": None,
            },
            {
                "info_hash": info_hash,
                "index": 2,
                "title": "Show.S02E02.1080p.mkv",
                "size": 200,
                "season": 2,
                "episode": 2,
                "parsed": None,
            },
        ]

        for media_scope, media_id, season in (
            (MediaScope.SEASON, "tt1234567:2", 2),
            (MediaScope.SERIES, "tt1234567", None),
        ):
            with self.subTest(media_scope=media_scope):
                with (
                    patch(
                        "comet.services.debrid.retrieve_debrid_availability",
                        new=AsyncMock(return_value=availability),
                    ),
                    patch(
                        "comet.services.debrid.schedule_cache_availability"
                    ) as schedule,
                ):
                    cached, updates = await DebridService(
                        "torbox", "token", ""
                    ).get_and_cache_availability(
                        session=None,
                        info_hashes=[info_hash],
                        seeders_map={},
                        tracker_map={},
                        sources_map={},
                        torrents=torrents,
                        media_id=media_id,
                        media_only_id="tt1234567",
                        season=season,
                        episode=None,
                        media_scope=media_scope,
                    )

                self.assertEqual(cached, {info_hash})
                self.assertEqual(updates, {})
                self.assertEqual(
                    torrents[info_hash]["title"], "Show.S02.COMPLETE.1080p.mkv"
                )
                self.assertEqual(torrents[info_hash]["size"], 1_000)
                self.assertIsNone(torrents[info_hash]["fileIndex"])
                schedule.assert_called_once_with("torbox", availability)

    async def test_cached_season_scope_never_replaces_pack_metadata(self):
        info_hash = "a" * 40
        torrent = {
            "title": "Show.S02.COMPLETE.1080p.mkv",
            "size": 1_000,
            "fileIndex": None,
            "parsed": None,
        }
        rows = [
            {
                "info_hash": info_hash,
                "file_index": "1",
                "title": "Show.S02E01.1080p.mkv",
                "size": 100,
                "parsed": '{"raw_title":"Show.S02E01.1080p.mkv"}',
            }
        ]

        for media_scope, season in (
            (MediaScope.SEASON, 2),
            (MediaScope.SERIES, None),
        ):
            with self.subTest(media_scope=media_scope):
                with patch(
                    "comet.services.debrid.get_cached_availability",
                    return_value=rows,
                ):
                    cached, updates = await DebridService(
                        "torbox", "token", ""
                    ).check_existing_availability(
                        [info_hash],
                        season=season,
                        episode=None,
                        media_scope=media_scope,
                        torrents={info_hash: torrent},
                    )

                self.assertEqual(cached, {info_hash})
                self.assertEqual(updates, {})
                self.assertEqual(torrent["title"], "Show.S02.COMPLETE.1080p.mkv")
                self.assertEqual(torrent["size"], 1_000)
                self.assertIsNone(torrent["fileIndex"])
                self.assertIsNone(torrent["parsed"])

    async def test_season_scope_skips_unused_cross_service_enrichment_lookup(self):
        info_hash = "a" * 40
        torrents = {info_hash: {"title": "Show.S02.COMPLETE.1080p.mkv"}}

        for media_scope, season in (
            (MediaScope.SEASON, 2),
            (MediaScope.SERIES, None),
        ):
            with patch(
                "comet.services.debrid.get_cached_availability_any_service"
            ) as lookup:
                await DebridService.apply_cached_availability_any_service(
                    [info_hash],
                    season=season,
                    episode=None,
                    media_scope=media_scope,
                    torrents=torrents,
                )
            lookup.assert_not_called()
        self.assertEqual(torrents[info_hash]["title"], "Show.S02.COMPLETE.1080p.mkv")

    async def test_corrupt_cached_parse_fails_the_cache_read(self):
        service = DebridService("realdebrid", "token", "")
        torrents = {
            "a" * 40: {"parsed": None},
            "b" * 40: {"parsed": None},
        }
        rows = [
            {
                "info_hash": "a" * 40,
                "file_index": "1",
                "title": "Corrupt.mkv",
                "size": 100,
                "parsed": "not-json",
            },
            {
                "info_hash": "b" * 40,
                "file_index": "2",
                "title": "Valid.mkv",
                "size": 200,
                "parsed": '{"raw_title":"Valid.mkv"}',
            },
        ]

        with (
            patch(
                "comet.services.debrid.get_cached_availability",
                return_value=rows,
            ),
            self.assertRaises(ValueError),
        ):
            await service.check_existing_availability(
                list(torrents), None, None, MediaScope.MOVIE, torrents
            )
