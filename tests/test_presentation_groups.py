import unittest
import uuid
from dataclasses import replace

from RTN import parse

from comet.core.capabilities import EligibleProvider
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TorrentLocator,
    TransportKind,
)
from comet.playback.groups import build_presentation_groups
from comet.playback.presentation import ProviderOption, select_presentation


def _candidate(
    candidate_id: str,
    transport: TransportKind,
    title: str,
    *,
    size: int = 1_000_000_000,
) -> ReleaseCandidate:
    if transport is TransportKind.BITTORRENT:
        locator = TorrentLocator(
            locator_id=f"{candidate_id}:torrent",
            kind=LocatorKind.TORRENT,
            policy=LocatorPolicy(frozenset({"direct_torrent"})),
            info_hash="a" * 40,
        )
    else:
        locator = NzbArtifactRef(
            locator_id=f"{candidate_id}:nzb",
            kind=LocatorKind.NZB_ARTIFACT,
            policy=LocatorPolicy(frozenset({"stremio_nntp"})),
            artifact_sha256="b" * 64,
            manifest_identity="nm1:" + "c" * 64,
        )
    return ReleaseCandidate(
        candidate_id=candidate_id,
        media_id="tt1234567",
        scope=ReleaseScope.MOVIE,
        transport=transport,
        title=title,
        locators=(locator,),
        size=size,
        parsed=parse(title),
    )


class PresentationGroupingTests(unittest.TestCase):
    def test_exact_cross_family_alternatives_share_one_group(self):
        torrent = _candidate(
            "torrent",
            TransportKind.BITTORRENT,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        usenet = _candidate(
            "usenet",
            TransportKind.USENET,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )

        groups = build_presentation_groups((torrent, usenet))

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].resolution, "1080p")
        self.assertEqual(groups[0].candidates, (torrent, usenet))

    def test_identical_releases_in_different_scopes_never_share_a_group(self):
        movie = _candidate(
            "movie",
            TransportKind.BITTORRENT,
            "Example.2026.1080p.WEB-DL-GROUP",
        )
        episode = replace(
            _candidate(
                "episode",
                TransportKind.USENET,
                "Example.2026.1080p.WEB-DL-GROUP",
            ),
            scope=ReleaseScope.EPISODE,
        )

        groups = build_presentation_groups((movie, episode))

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {group.candidates[0].scope.value for group in groups},
            {"movie", "episode"},
        )

    def test_conflicting_known_resolutions_never_share_a_group(self):
        first = _candidate(
            "1080",
            TransportKind.BITTORRENT,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        second = _candidate(
            "2160",
            TransportKind.USENET,
            "Movie.2026.2160p.WEB-DL-GROUP",
        )

        groups = build_presentation_groups((first, second))

        self.assertEqual(len(groups), 2)
        self.assertEqual({group.resolution for group in groups}, {"1080p", "2160p"})

    def test_unknown_resolution_is_not_inferred_from_another_candidate(self):
        known = _candidate(
            "known",
            TransportKind.BITTORRENT,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        unknown = _candidate(
            "unknown",
            TransportKind.USENET,
            "Movie.2026.WEB-DL-GROUP",
        )

        groups = build_presentation_groups((known, unknown))

        self.assertEqual(len(groups), 2)
        self.assertEqual(
            {group.resolution for group in groups},
            {"1080p", "unknown"},
        )

    def test_duplicate_candidate_ids_are_not_silently_collapsed(self):
        candidate = _candidate(
            "same",
            TransportKind.BITTORRENT,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )

        with self.assertRaisesRegex(ValueError, "unique"):
            build_presentation_groups((candidate, candidate))

    def test_group_limit_keeps_all_cross_family_members(self):
        torrent = _candidate(
            "torrent",
            TransportKind.BITTORRENT,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        usenet = _candidate(
            "usenet",
            TransportKind.USENET,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        later = _candidate(
            "later",
            TransportKind.USENET,
            "Movie.2026.1080p.WEB-DL-OTHER",
        )
        torrent_provider = EligibleProvider(str(uuid.uuid4()), "direct_torrent", 0)
        usenet_provider = EligibleProvider(str(uuid.uuid4()), "stremio_nntp", 1)
        options = (
            ProviderOption(
                torrent.candidate_id,
                torrent_provider,
                torrent.locators,
            ),
            ProviderOption(
                usenet.candidate_id,
                usenet_provider,
                usenet.locators,
            ),
            ProviderOption(
                later.candidate_id,
                usenet_provider,
                later.locators,
            ),
        )

        candidates, retained = select_presentation(
            (torrent, usenet, later),
            options,
            cached_only=False,
            max_releases_per_resolution=1,
        )

        self.assertEqual(candidates, (torrent, usenet))
        self.assertEqual(
            [option.candidate_id for option in retained],
            ["torrent", "usenet"],
        )

    def test_confirmed_debrid_cache_is_preferred_only_inside_same_group(self):
        torrent = _candidate(
            "torrent",
            TransportKind.BITTORRENT,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        usenet = _candidate(
            "usenet",
            TransportKind.USENET,
            "Movie.2026.1080p.WEB-DL-GROUP",
        )
        torrent_provider = EligibleProvider(str(uuid.uuid4()), "realdebrid", 1)
        usenet_provider = EligibleProvider(str(uuid.uuid4()), "stremio_nntp", 0)
        options = (
            ProviderOption(
                torrent.candidate_id,
                torrent_provider,
                torrent.locators,
                cached=True,
            ),
            ProviderOption(
                usenet.candidate_id,
                usenet_provider,
                usenet.locators,
            ),
        )

        candidates, retained = select_presentation(
            (torrent, usenet),
            options,
            cached_only=False,
            max_releases_per_resolution=0,
        )

        self.assertEqual(candidates, (torrent, usenet))
        self.assertEqual(
            [option.candidate_id for option in retained],
            ["torrent", "usenet"],
        )

    def test_cached_lower_ranked_release_never_overtakes_content_rank(self):
        ranked_first = _candidate(
            "ranked-first",
            TransportKind.USENET,
            "First.2026.1080p.WEB-DL-GROUP",
        )
        cached_later = _candidate(
            "cached-later",
            TransportKind.BITTORRENT,
            "Later.2026.1080p.WEB-DL-OTHER",
        )
        options = (
            ProviderOption(
                ranked_first.candidate_id,
                EligibleProvider(str(uuid.uuid4()), "stremio_nntp", 0),
                ranked_first.locators,
            ),
            ProviderOption(
                cached_later.candidate_id,
                EligibleProvider(str(uuid.uuid4()), "realdebrid", 1),
                cached_later.locators,
                cached=True,
            ),
        )

        _candidates, retained = select_presentation(
            (ranked_first, cached_later),
            options,
            cached_only=False,
            max_releases_per_resolution=0,
        )

        self.assertEqual(
            [option.candidate_id for option in retained],
            ["ranked-first", "cached-later"],
        )

    def test_cached_only_is_scoped_to_debrid_paths(self):
        torrent = _candidate(
            "torrent",
            TransportKind.BITTORRENT,
            "Torrent.2026.1080p.WEB-DL-GROUP",
        )
        usenet = _candidate(
            "usenet",
            TransportKind.USENET,
            "Usenet.2026.1080p.WEB-DL-GROUP",
        )
        options = (
            ProviderOption(
                torrent.candidate_id,
                EligibleProvider(str(uuid.uuid4()), "realdebrid", 0),
                torrent.locators,
            ),
            ProviderOption(
                torrent.candidate_id,
                EligibleProvider(str(uuid.uuid4()), "direct_torrent", 1),
                torrent.locators,
            ),
            ProviderOption(
                usenet.candidate_id,
                EligibleProvider(str(uuid.uuid4()), "stremio_nntp", 2),
                usenet.locators,
            ),
        )

        candidates, retained = select_presentation(
            (torrent, usenet),
            options,
            cached_only=True,
            max_releases_per_resolution=0,
        )

        self.assertEqual(candidates, (torrent, usenet))
        self.assertEqual(
            [option.provider.kind for option in retained],
            ["direct_torrent", "stremio_nntp"],
        )
