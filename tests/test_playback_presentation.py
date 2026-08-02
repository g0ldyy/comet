import base64
import unittest
import uuid

from comet.core.capabilities import CapabilityPlanner
from comet.core.sources import (
    EasynewsHttpRef,
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TransportKind,
)
from comet.playback.presentation import (
    build_provider_options,
    issue_nzb_handoff_capability,
    issue_provider_option_capability,
    select_presentation,
)
from comet.playback.repository import RenderedCandidateIds
from comet.playback.tokens import CapabilityCodec
from comet.usenet.access import NativeAccessAuthorizer


class ProviderPresentationTests(unittest.TestCase):
    @staticmethod
    def _mixed_candidate(candidate_id: str) -> ReleaseCandidate:
        return ReleaseCandidate(
            candidate_id=candidate_id,
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title=f"Example.{candidate_id}.2024.1080p",
            locators=(
                NzbArtifactRef(
                    locator_id=f"{candidate_id}:cloud",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                    artifact_sha256="c" * 64,
                    manifest_identity="nm1:" + "d" * 64,
                ),
                NzbArtifactRef(
                    locator_id=f"{candidate_id}:artifact",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"stremio_nntp"})),
                    artifact_sha256="a" * 64,
                    manifest_identity="nm1:" + "b" * 64,
                ),
            ),
        )

    @staticmethod
    def _mixed_plan():
        return CapabilityPlanner(
            usenet_offered=True,
            native_authorizer=NativeAccessAuthorizer(None),
        ).build(
            {
                "schemaVersion": 2,
                "enabledTransports": ["usenet"],
                "playbackProviders": [
                    {
                        "configurationId": ("11111111-1111-4111-8111-111111111111"),
                        "displayName": "Cloud",
                        "kind": "torbox_usenet",
                        "enabled": True,
                    },
                    {
                        "configurationId": ("22222222-2222-4222-8222-222222222222"),
                        "displayName": "Client",
                        "kind": "stremio_nntp",
                        "enabled": True,
                    },
                ],
            }
        )

    def test_provider_build_preserves_ranked_candidate_order(self):
        first = self._mixed_candidate("z-ranked-first")
        second = self._mixed_candidate("a-ranked-second")

        options = build_provider_options((first, second), self._mixed_plan())

        self.assertEqual(
            [option.candidate_id for option in options],
            [
                first.candidate_id,
                first.candidate_id,
                second.candidate_id,
                second.candidate_id,
            ],
        )

    def test_presentation_keeps_every_compatible_never_prepared_path(self):
        first = self._mixed_candidate("first")
        second = ReleaseCandidate(
            candidate_id="unknown-only",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Unknown.2024.1080p",
            locators=(first.locators[0],),
        )
        candidates, options = select_presentation(
            (first, second),
            build_provider_options((first, second), self._mixed_plan()),
            cached_only=False,
            max_releases_per_resolution=0,
        )

        self.assertEqual(candidates, (first, second))
        self.assertEqual(
            [option.provider.kind for option in options],
            ["torbox_usenet", "stremio_nntp", "torbox_usenet"],
        )

    def test_presentation_keeps_configured_provider_alternatives(self):
        candidate = self._mixed_candidate("candidate")

        candidates, options = select_presentation(
            (candidate,),
            build_provider_options((candidate,), self._mixed_plan()),
            cached_only=False,
            max_releases_per_resolution=0,
        )

        self.assertEqual(candidates, (candidate,))
        self.assertEqual(len(options), 2)
        self.assertEqual(
            [option.provider.kind for option in options],
            ["torbox_usenet", "stremio_nntp"],
        )

    def test_presentation_never_reorders_releases_from_playback_history(self):
        mixed = self._mixed_candidate("source")
        unknown = ReleaseCandidate(
            candidate_id="higher-ranked-unknown",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Unknown.2024.1080p",
            locators=(mixed.locators[0],),
        )
        ready = ReleaseCandidate(
            candidate_id="lower-ranked-ready",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Ready.2024.1080p",
            locators=(mixed.locators[1],),
        )
        _candidates, options = select_presentation(
            (unknown, ready),
            build_provider_options((unknown, ready), self._mixed_plan()),
            cached_only=False,
            max_releases_per_resolution=0,
        )

        self.assertEqual(
            [option.candidate_id for option in options],
            [unknown.candidate_id, ready.candidate_id],
        )

    def test_issues_lazy_handoff_for_an_exact_easynews_generated_nzb_target(self):
        provider_id = "11111111-1111-4111-8111-111111111111"
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [
                {
                    "configurationId": provider_id,
                    "displayName": "Client",
                    "kind": "stremio_nntp",
                    "enabled": True,
                }
            ],
        }
        plan = CapabilityPlanner(
            usenet_offered=True,
            native_authorizer=NativeAccessAuthorizer(None),
        ).build(config)
        candidate = ReleaseCandidate(
            candidate_id="candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Example.2024.1080p",
            locators=(
                EasynewsHttpRef(
                    locator_id="easynews-source",
                    kind=LocatorKind.EASYNEWS_HTTP,
                    policy=LocatorPolicy(
                        frozenset({"stremio_nntp"}),
                        exact_provider_configuration_id=provider_id,
                    ),
                    account_configuration_id=provider_id,
                    file_identifier="file",
                    download_farm="farm",
                    download_port="443",
                    content_hash="hash",
                    item_identifier="item",
                    filename="Movie",
                    extension="mkv",
                ),
            ),
        )
        option = build_provider_options((candidate,), plan)[0]
        codec = CapabilityCodec(
            base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
        )
        partition = codec.configuration_partition(b"config")
        persisted = RenderedCandidateIds(
            str(uuid.uuid4()),
            {"easynews-source": str(uuid.uuid4())},
        )

        token = issue_nzb_handoff_capability(
            codec,
            partition=partition,
            option=option,
            persisted=persisted,
            selection_intent=[0],
            ttl=60,
        )
        intent = codec.decode_nzb_handoff_intent(token, partition=partition)

        self.assertEqual(intent.candidate_id, persisted.candidate_id)
        self.assertEqual(intent.provider_configuration_id, provider_id)
        self.assertEqual(
            intent.locator_ids,
            (persisted.locator_ids["easynews-source"],),
        )

    def test_aggregates_only_compatible_locators_per_configured_provider(self):
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [
                {
                    "configurationId": "11111111-1111-4111-8111-111111111111",
                    "displayName": "Cloud",
                    "kind": "torbox_usenet",
                    "enabled": True,
                },
                {
                    "configurationId": "22222222-2222-4222-8222-222222222222",
                    "displayName": "Client",
                    "kind": "stremio_nntp",
                    "enabled": True,
                },
            ],
        }
        plan = CapabilityPlanner(
            usenet_offered=True, native_authorizer=NativeAccessAuthorizer(None)
        ).build(config)
        candidate = ReleaseCandidate(
            candidate_id="candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Example.2024.1080p",
            locators=(
                NzbArtifactRef(
                    locator_id="cloud-artifact",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                    artifact_sha256="c" * 64,
                    manifest_identity="nm1:" + "d" * 64,
                ),
                NzbArtifactRef(
                    locator_id="artifact-b",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"stremio_nntp"})),
                    artifact_sha256="b" * 64,
                    manifest_identity="nm1:" + "b" * 64,
                ),
                NzbArtifactRef(
                    locator_id="artifact-a",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"stremio_nntp"})),
                    artifact_sha256="a" * 64,
                    manifest_identity="nm1:" + "a" * 64,
                ),
            ),
        )

        options = build_provider_options((candidate,), plan)

        self.assertEqual(
            [option.provider.kind for option in options],
            ["torbox_usenet", "stremio_nntp"],
        )
        self.assertEqual(
            [locator.locator_id for locator in options[1].locators],
            ["artifact-a", "artifact-b"],
        )

    def test_issues_pi2_from_only_committed_internal_ids(self):
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["usenet"],
            "playbackProviders": [
                {
                    "configurationId": "11111111-1111-4111-8111-111111111111",
                    "displayName": "Cloud",
                    "kind": "torbox_usenet",
                    "enabled": True,
                }
            ],
        }
        plan = CapabilityPlanner(
            usenet_offered=True, native_authorizer=NativeAccessAuthorizer(None)
        ).build(config)
        candidate = ReleaseCandidate(
            candidate_id="candidate",
            media_id="tt123",
            scope=ReleaseScope.MOVIE,
            transport=TransportKind.USENET,
            title="Example.2024.1080p",
            locators=(
                NzbArtifactRef(
                    locator_id="artifact",
                    kind=LocatorKind.NZB_ARTIFACT,
                    policy=LocatorPolicy(frozenset({"torbox_usenet"})),
                    artifact_sha256="c" * 64,
                    manifest_identity="nm1:" + "d" * 64,
                ),
            ),
        )
        option = build_provider_options((candidate,), plan)[0]
        root = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
        codec = CapabilityCodec(root)
        partition = codec.configuration_partition(b"config")
        persisted = RenderedCandidateIds(
            str(uuid.uuid4()), {"artifact": str(uuid.uuid4())}
        )

        token = issue_provider_option_capability(
            codec,
            partition=partition,
            option=option,
            persisted=persisted,
            selection_intent=[0],
            client="stremio",
        )

        self.assertEqual(
            codec.decode(token, partition=partition)[5][0:1],
            uuid.UUID(persisted.candidate_id).bytes[0:1],
        )
