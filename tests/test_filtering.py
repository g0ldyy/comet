import unittest
from unittest.mock import patch

from RTN import parse

from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.services.filtering import (
    _clone_parsed,
    exact_alias_match,
    filter_release_candidates,
    filter_release_records,
    settings,
)


class AliasFilteringTests(unittest.TestCase):
    def test_usenet_candidate_receives_the_shared_rtn_parse(self):
        candidate = ReleaseCandidate(
            candidate_id="candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="The.Matrix.1999.1080p.WEB-DL.x264",
            locators=(
                NzbArtifactRef(
                    locator_id="locator",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                    artifact_sha256="a" * 64,
                    manifest_identity="nm1:" + "b" * 64,
                ),
            ),
        )

        filtered = filter_release_candidates(
            (candidate,), "The Matrix", 1999, None, "movie", {}, False
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].candidate_id, candidate.candidate_id)
        self.assertEqual(filtered[0].parsed.resolution, "1080p")

    def test_torrent_candidate_carries_its_shared_parse_into_the_locator(self):
        candidate = ReleaseCandidate(
            candidate_id="candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.BITTORRENT,
            title="The.Matrix.1999.1080p.WEB-DL.x264",
            locators=(
                TorrentLocator(
                    locator_id="locator",
                    kind=LocatorKind.TORRENT,
                    policy=LocatorPolicy(frozenset({"direct_torrent"})),
                    info_hash="a" * 40,
                ),
            ),
        )

        filtered = filter_release_candidates(
            (candidate,), "The Matrix", 1999, None, "movie", {}, False
        )

        self.assertEqual(filtered[0].parsed.resolution, "1080p")
        self.assertIn(
            '"raw_title":"The.Matrix.1999.1080p.WEB-DL.x264"',
            filtered[0].locators[0].selection_parsed_json,
        )

    def test_cached_parse_clone_detaches_mutated_languages(self):
        cached = parse("Movie.2024.MULTI.FRENCH.1080p.WEB-DL")
        clone = _clone_parsed(cached)

        clone.languages.append("de")

        self.assertNotIn("de", cached.languages)
        self.assertIn("de", clone.languages)

    def test_empty_alias_does_not_match_every_title(self):
        self.assertFalse(exact_alias_match("unrelated title", [""]))

    def test_short_or_partial_alias_does_not_bypass_title_matching(self):
        self.assertFalse(exact_alias_match("quality release", ["it"]))
        self.assertFalse(exact_alias_match("friends swapped places", ["swap"]))
        self.assertFalse(exact_alias_match("friends swapped places", ["swapped"]))
        self.assertTrue(exact_alias_match("swapped", ["swapped"]))

    def test_invalid_internal_alias_contract_is_not_normalized(self):
        with self.assertRaises(AttributeError):
            filter_release_records([], "Movie", 2026, None, "movie", [], False)
        with self.assertRaises(TypeError):
            filter_release_records(
                [{"title": "Different.2026.1080p.WEB-DL"}],
                "Movie",
                2026,
                None,
                "movie",
                {"ez": [""]},
                False,
            )

    def test_language_scoped_alias_sets_the_exact_language(self):
        torrent = {
            "title": "Il.Postino.2020.1080p.WEB-DL",
            "infoHash": "1" * 40,
        }

        with patch.object(settings, "SMART_LANGUAGE_DETECTION", True):
            actual = filter_release_records(
                [torrent],
                "The Postman",
                2020,
                None,
                "movie",
                {"lang:it": ["Il Postino"]},
                False,
            )

        self.assertEqual(actual[0]["parsed"].languages, ["it"])

    def test_movie_named_sample_is_not_rejected_by_filename_heuristic(self):
        actual = filter_release_records(
            [{"title": "The.Sample.2026.1080p.WEB-DL", "infoHash": "1" * 40}],
            "The Sample",
            2026,
            None,
            "movie",
            {},
            False,
        )

        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]["parsed"].parsed_title, "The Sample")


if __name__ == "__main__":
    unittest.main()
