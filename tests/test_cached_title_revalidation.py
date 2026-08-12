import unittest
from unittest.mock import patch

from RTN import parse

from comet.services.orchestration import TorrentManager


class CachedTitleRevalidationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _row(title: str, info_hash: str) -> dict:
        return {
            "info_hash": info_hash,
            "file_index": 0,
            "title": title,
            "seeders": 1,
            "size": 1_000,
            "tracker": "cache",
            "sources_json": "[]",
            "parsed_json": parse(title).model_dump_json(),
            "episode": None,
            "updated_at": 1,
        }

    @staticmethod
    def _manager() -> TorrentManager:
        return TorrentManager(
            media_type="movie",
            media_full_id="tt2250912",
            media_only_id="tt2250912",
            title="Spider-Man: Homecoming",
            year=2017,
            year_end=None,
            season=None,
            episode=None,
            aliases={},
            remove_adult_content=False,
        )

    async def test_mismatched_cached_title_is_rejected(self):
        manager = self._manager()
        wrong_hash = "a" * 40
        wrong = self._row(
            "Spider-Man.Into.the.Spider-Verse.2018.2160p.REMUX.HEVC.DV.mkv",
            wrong_hash,
        )

        with patch.object(manager, "_fetch_cached_rows", return_value=[wrong]):
            await manager.get_cached_torrents()

        self.assertNotIn(wrong_hash, manager.torrents)
        self.assertFalse(manager.primary_cached)

    async def test_cached_row_with_wrong_year_is_rejected(self):
        manager = self._manager()
        wrong_year_hash = "e" * 40
        wrong_year = self._row(
            "Spider-Man.Homecoming.2019.2160p.BluRay.REMUX.HEVC.mkv",
            wrong_year_hash,
        )

        with patch.object(manager, "_fetch_cached_rows", return_value=[wrong_year]):
            await manager.get_cached_torrents()

        self.assertNotIn(wrong_year_hash, manager.torrents)
        self.assertFalse(manager.primary_cached)

    async def test_cached_row_revalidates_legacy_parsed_data_from_raw_title(self):
        manager = self._manager()
        missing_title_hash = "c" * 40
        row = self._row(
            "Spider-Man.Homecoming.2017.2160p.BluRay.REMUX.HEVC.mkv",
            missing_title_hash,
        )
        row["parsed_json"] = '{"raw_title":"Spider-Man.Homecoming.2017.mkv"}'

        with patch.object(manager, "_fetch_cached_rows", return_value=[row]):
            await manager.get_cached_torrents()

        self.assertIn(missing_title_hash, manager.torrents)
        self.assertTrue(manager.primary_cached)

    async def test_cached_row_does_not_trust_persisted_parsed_title(self):
        manager = self._manager()
        inconsistent_hash = "d" * 40
        row = self._row(
            "Spider-Man.Into.the.Spider-Verse.2018.2160p.REMUX.HEVC.DV.mkv",
            inconsistent_hash,
        )
        row["parsed_json"] = parse(
            "Spider-Man.Homecoming.2017.2160p.BluRay.REMUX.HEVC.mkv"
        ).model_dump_json()

        with patch.object(manager, "_fetch_cached_rows", return_value=[row]):
            await manager.get_cached_torrents()

        self.assertNotIn(inconsistent_hash, manager.torrents)
        self.assertFalse(manager.primary_cached)

    async def test_matching_cached_title_still_counts_as_primary_cache(self):
        manager = self._manager()
        right_hash = "b" * 40
        right = self._row(
            "Spider-Man.Homecoming.2017.2160p.BluRay.REMUX.HEVC.mkv",
            right_hash,
        )

        with patch.object(manager, "_fetch_cached_rows", return_value=[right]):
            await manager.get_cached_torrents()

        self.assertIn(right_hash, manager.torrents)
        self.assertTrue(manager.primary_cached)

    async def test_secondary_duplicate_does_not_count_as_primary_cache(self):
        manager = self._manager()
        manager.cache_media_ids = [manager.media_only_id, "kitsu:456"]
        duplicate_hash = "f" * 40
        primary = self._row(
            "Spider-Man.Into.the.Spider-Verse.2018.2160p.REMUX.HEVC.DV.mkv",
            duplicate_hash,
        )
        primary["updated_at"] = 1
        secondary = self._row(
            "Spider-Man.Homecoming.2017.2160p.BluRay.REMUX.HEVC.mkv",
            duplicate_hash,
        )
        secondary["updated_at"] = 2

        async def fetch_rows(media_id):
            return [primary] if media_id == manager.media_only_id else [secondary]

        with patch.object(manager, "_fetch_cached_rows", side_effect=fetch_rows):
            await manager.get_cached_torrents()

        self.assertIn(duplicate_hash, manager.torrents)
        self.assertEqual(
            manager.torrents[duplicate_hash]["updatedAt"],
            secondary["updated_at"],
        )
        self.assertFalse(manager.primary_cached)


if __name__ == "__main__":
    unittest.main()
