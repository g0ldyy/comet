import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from RTN import ParsedData

from comet.api.endpoints.stream import _build_folder_hints
from comet.api.endpoints.torznab import serialize_feed
from comet.services.debrid import DebridService
from comet.services.media_search import MediaSearchResult, MediaSearchStatus
from comet.services.orchestration import TorrentManager


def _match_file(torrent: dict, title: str, size: int, file_index: int = 1) -> dict:
    torrent.update(
        DebridService._build_torrent_update(
            torrent, file_index=file_index, title=title, size=size, parsed=None
        )
    )
    return torrent


class FolderHintsTest(unittest.TestCase):
    def test_pack_name_and_size_survive_file_match(self):
        torrent = {"title": "Show.S01.2160p-GRP", "size": 50_000, "parsed": None}
        _match_file(torrent, "Show.S01E06.2160p-GRP.mkv", 6_000)

        self.assertEqual(torrent["title"], "Show.S01E06.2160p-GRP.mkv")
        self.assertEqual(
            _build_folder_hints(torrent),
            {"folderName": "Show.S01.2160p-GRP", "folderSize": 50_000},
        )

    def test_single_file_torrent_gets_no_hints(self):
        torrent = {"title": "Movie.2024.2160p.mkv", "size": 20_000, "parsed": None}
        _match_file(torrent, "Movie.2024.2160p.mkv", 20_000, file_index=0)

        self.assertEqual(_build_folder_hints(torrent), {})

    def test_single_release_with_extra_files_gets_no_hints(self):
        torrent = {"title": "Movie.2024.2160p-GRP", "size": 20_100, "parsed": None}
        _match_file(torrent, "Movie.2024.2160p-GRP.mkv", 20_000, file_index=0)

        self.assertEqual(_build_folder_hints(torrent), {})

    def test_later_file_match_keeps_the_pack_name(self):
        torrent = {"title": "Show.S01.1080p-GRP", "size": 10_000, "parsed": None}
        _match_file(torrent, "Show.S01E01.mkv", 1_000)
        _match_file(torrent, "Show.S01E02.mkv", 1_200, file_index=2)

        self.assertEqual(
            _build_folder_hints(torrent),
            {"folderName": "Show.S01.1080p-GRP", "folderSize": 10_000},
        )


class CachedPackNameTest(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        return TorrentManager(
            media_type="series",
            media_full_id="tt123:1:1",
            media_only_id="tt123",
            title="Show",
            year=2024,
            year_end=None,
            season=1,
            episode=1,
            aliases={},
            remove_adult_content=False,
        )

    def _episode_row(self, info_hash: str) -> dict:
        return {
            "info_hash": info_hash,
            "seeders": 1,
            "tracker": "cache",
            "sources_json": "[]",
            "updated_at": 1,
            "file_index": 1,
            "title": "Show.S01E01.1080p-GRP.mkv",
            "size": 100,
            "episode": 1,
            "parsed_json": (
                '{"raw_title":"Show.S01E01.1080p-GRP.mkv","seasons":[1],"episodes":[1]}'
            ),
        }

    async def test_episode_request_keeps_pack_name_from_cache(self):
        manager = self._manager()
        info_hash = "a" * 40
        pack_row = {
            **self._episode_row(info_hash),
            "file_index": None,
            "title": "Show.S01.1080p-GRP",
            "size": 800,
            "episode": None,
            "parsed_json": '{"raw_title":"Show.S01.1080p-GRP","seasons":[1],"episodes":[]}',
        }
        rows = [pack_row, self._episode_row(info_hash)]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        torrent = manager.torrents[info_hash]
        self.assertEqual(torrent["title"], "Show.S01E01.1080p-GRP.mkv")
        self.assertEqual(
            _build_folder_hints(torrent),
            {"folderName": "Show.S01.1080p-GRP", "folderSize": 800},
        )

    async def test_multi_episode_file_row_is_not_taken_for_a_pack(self):
        manager = self._manager()
        info_hash = "b" * 40
        multi_episode_row = {
            **self._episode_row(info_hash),
            "file_index": 2,
            "title": "Show.S01E01E02.1080p-GRP.mkv",
            "size": 800,
            "episode": None,
            "parsed_json": (
                '{"raw_title":"Show.S01E01E02.1080p-GRP.mkv",'
                '"seasons":[1],"episodes":[1,2]}'
            ),
        }
        rows = [multi_episode_row, self._episode_row(info_hash)]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        torrent = manager.torrents[info_hash]
        self.assertNotIn("torrentTitle", torrent)
        self.assertEqual(_build_folder_hints(torrent), {})

    async def test_file_row_without_extension_is_not_taken_for_a_pack(self):
        manager = self._manager()
        info_hash = "f" * 40
        file_row = {
            **self._episode_row(info_hash),
            "file_index": 3,
            "title": "Show.S01E01-E02.1080p-GRP",
            "size": 800,
            "episode": None,
            "parsed_json": (
                '{"raw_title":"Show.S01E01-E02.1080p-GRP",'
                '"seasons":[1],"episodes":[1,2]}'
            ),
        }
        rows = [file_row, self._episode_row(info_hash)]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        self.assertNotIn("torrentTitle", manager.torrents[info_hash])

    async def test_anime_batch_row_is_taken_for_a_pack(self):
        manager = self._manager()
        info_hash = "c" * 40
        batch_row = {
            **self._episode_row(info_hash),
            "file_index": None,
            "title": "[GRP] Show (01-12) [1080p]",
            "size": 1_200,
            "episode": None,
            "parsed_json": (
                '{"raw_title":"[GRP] Show (01-12) [1080p]","seasons":[],'
                '"episodes":[1,2,3,4,5,6,7,8,9,10,11,12]}'
            ),
        }
        rows = [batch_row, self._episode_row(info_hash)]

        with patch.object(manager, "_fetch_cached_rows", return_value=rows):
            await manager.get_cached_torrents()

        self.assertEqual(
            _build_folder_hints(manager.torrents[info_hash]),
            {"folderName": "[GRP] Show (01-12) [1080p]", "folderSize": 1_200},
        )


class TorznabPackTest(unittest.TestCase):
    def _feed(self, torrent: dict) -> tuple[str, int]:
        info_hash = "d" * 40
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            metadata={"title": "Show", "year": 2024},
            torrents={info_hash: torrent},
            ranked_info_hashes=[info_hash],
            media_only_id="tt1234567",
            search_season=1,
            search_episode=1,
        )
        content, total = serialize_feed(
            result, "series", "https://example.test/torznab/api"
        )
        self.assertEqual(total, 1)
        item = ET.fromstring(content).find("channel/item")
        return item.findtext("title"), int(item.findtext("size"))

    def _torrent(self, **fields) -> dict:
        return {
            "fileIndex": 1,
            "seeders": 1,
            "tracker": "cache",
            "sources": [],
            "updatedAt": 1_700_000_000,
            "parsed": ParsedData(raw_title=fields["title"], resolution="1080p"),
            **fields,
        }

    def test_file_in_a_pack_is_listed_as_the_pack(self):
        torrent = self._torrent(
            title="Show.S01E01.1080p-GRP.mkv",
            size=100,
            torrentTitle="Show.S01.1080p-GRP",
            torrentSize=800,
        )

        self.assertEqual(self._feed(torrent), ("Show.S01.1080p-GRP", 800))

    def test_single_file_release_is_unchanged(self):
        torrent = self._torrent(title="Show.S01E01.1080p-GRP.mkv", size=100)

        self.assertEqual(self._feed(torrent), ("Show.S01E01.1080p-GRP.mkv", 100))

    def test_movie_is_listed_as_its_file(self):
        torrent = self._torrent(
            title="Movie.2024.Part.1.1080p-GRP.mkv",
            size=100,
            torrentTitle="Movie.Collection.1080p-GRP",
            torrentSize=300,
        )
        info_hash = "e" * 40
        result = MediaSearchResult(
            MediaSearchStatus.OK,
            metadata={"title": "Movie", "year": 2024},
            torrents={info_hash: torrent},
            ranked_info_hashes=[info_hash],
            media_only_id="tt1234567",
        )
        content, _ = serialize_feed(result, "movie", "https://example.test/torznab/api")
        item = ET.fromstring(content).find("channel/item")

        self.assertEqual(
            (item.findtext("title"), int(item.findtext("size"))),
            ("Movie.2024.Part.1.1080p-GRP.mkv", 100),
        )
