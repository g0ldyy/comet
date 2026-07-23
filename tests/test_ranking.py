import unittest
from unittest.mock import patch

from RTN import (
    DefaultRanking,
    SettingsModel,
    Torrent,
    check_fetch,
    get_rank,
    parse,
    sort_torrents,
)

from comet.core.models import rtn_settings_default
from comet.services.ranking import rank_worker


class RankWorkerTests(unittest.TestCase):
    def test_comet_fetches_reported_size_but_still_rejects_cam(self):
        size_fetchable, size_reasons = check_fetch(
            parse("Obsession.2026.1080p.WEB-DL.4.7GB.x264"), rtn_settings_default
        )
        cam_fetchable, cam_reasons = check_fetch(
            parse("Obsession.2026.1080p.CAM.x264"), rtn_settings_default
        )

        self.assertTrue(size_fetchable)
        self.assertEqual(size_reasons, [])
        self.assertFalse(cam_fetchable)
        self.assertIn("trash_quality", cam_reasons)

    def test_combined_worker_matches_individual_rtn_calls(self):
        titles = [
            "Oppenheimer.2023.2160p.REMUX.DV.HDR10Plus.TrueHD.7.1.HEVC",
            "The.Walking.Dead.S05E03.720p.WEB-DL.x264-ASAP",
            "Some.Movie.2020.CAM.XVID.MP3",
        ]
        torrents = {
            f"{index:040x}": {
                "title": title,
                "parsed": parse(title),
                "size": index * 1_000_000,
            }
            for index, title in enumerate(titles, 1)
        }
        settings = SettingsModel()
        ranking = DefaultRanking()

        expected = set()
        for info_hash, torrent in torrents.items():
            fetchable, _ = check_fetch(torrent["parsed"], settings)
            rank = get_rank(torrent["parsed"], settings, ranking)
            if not fetchable or rank < settings.options["remove_ranks_under"]:
                continue
            expected.add(
                Torrent(
                    infohash=info_hash,
                    raw_title=torrent["title"],
                    data=torrent["parsed"],
                    fetch=fetchable,
                    rank=rank,
                    lev_ratio=0.0,
                )
            )

        actual = rank_worker(torrents, settings, ranking, 50, 0, True)

        self.assertEqual(actual, sort_torrents(expected, 50))

    def test_invalid_infohash_is_ignored(self):
        title = "The.Matrix.1999.1080p.BluRay.x264"
        torrents = {
            "invalid": {
                "title": title,
                "parsed": parse(title),
                "size": 1_000_000,
            }
        }

        actual = rank_worker(torrents, SettingsModel(), DefaultRanking(), 50, 0, False)

        self.assertEqual(actual, {})

    def test_unexpected_torrent_error_is_not_masked(self):
        title = "The.Matrix.1999.1080p.BluRay.x264"
        torrents = {
            "1" * 40: {
                "title": title,
                "parsed": parse(title),
                "size": 1_000_000,
            }
        }

        with patch("comet.services.ranking.Torrent", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                rank_worker(torrents, SettingsModel(), DefaultRanking(), 50, 0, False)

    def test_size_filter_is_applied_before_batch_ranking(self):
        title = "The.Matrix.1999.1080p.BluRay.x264"
        torrents = {
            "1" * 40: {"title": title, "parsed": parse(title), "size": 1_000_000},
            "2" * 40: {"title": title, "parsed": parse(title), "size": 2_000_000},
        }

        actual = rank_worker(
            torrents, SettingsModel(), DefaultRanking(), 50, 1_500_000, False
        )

        self.assertEqual(set(actual), {"1" * 40})


if __name__ == "__main__":
    unittest.main()
