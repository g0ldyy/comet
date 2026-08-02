import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks
from RTN import ParsedData, parse
from starlette.requests import Request

from comet.api.endpoints.chilllink import chilllink_streams
from comet.api.endpoints.stream import _render_server_usenet_options
from comet.core.capabilities import EligibleProvider
from comet.core.sources import (
    LocatorKind,
    LocatorPolicy,
    NzbArtifactRef,
    ReleaseCandidate,
    ReleaseScope,
    TransportKind,
)
from comet.playback.presentation import ProviderOption
from comet.services.media_search import MediaSearchResult, MediaSearchStatus


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/chilllink/streams",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "scheme": "http",
        }
    )


def _server_result(
    provider_kind: str,
    title: str,
    *,
    size: int,
    parsed=None,
) -> tuple[str, MediaSearchResult]:
    provider_id = "11111111-1111-4111-8111-111111111111"
    locator = NzbArtifactRef(
        locator_id="artifact",
        kind=LocatorKind.NZB_ARTIFACT,
        policy=LocatorPolicy(frozenset({provider_kind})),
        artifact_sha256="a" * 64,
        manifest_identity="nm1:" + "b" * 64,
    )
    candidate = ReleaseCandidate(
        candidate_id="candidate",
        media_id="tt1234567",
        scope=ReleaseScope.MOVIE,
        transport=TransportKind.USENET,
        title=title,
        locators=(locator,),
        size=size,
        source="Usenet",
        parsed=parse(title) if parsed is None else parsed,
    )
    option = ProviderOption(
        candidate.candidate_id,
        EligibleProvider(provider_id, provider_kind, 0),
        candidate.locators,
    )
    return provider_id, MediaSearchResult(
        MediaSearchStatus.OK,
        candidates=(candidate,),
        provider_options=(option,),
        provider_capabilities={(candidate.candidate_id, provider_id): "pi2.capability"},
    )


def test_server_usenet_rendering_carries_truthful_chilllink_metadata():
    provider_id, search_result = _server_result(
        "nzbdav",
        "Movie.2026.1080p",
        size=2 * 1024**3,
    )
    config = {
        "resultFormat": ["all"],
        "playbackProviders": [
            {
                "configurationId": provider_id,
                "displayName": "My NzbDAV",
            }
        ],
    }

    streams = _render_server_usenet_options(
        search_result,
        config,
        "https://comet.test",
        chilllink=True,
    )

    rendered = next(iter(streams.values()))
    assert rendered["_chilllink"] == [
        "📰 My NzbDAV",
        "Usenet",
        "💾 2.0 GB",
        "🔎 Usenet",
    ]
    assert "cached" not in " ".join(rendered["_chilllink"]).lower()


def test_server_usenet_rendering_carries_kodi_metadata_without_fake_availability():
    provider_id, search_result = _server_result(
        "easynews",
        "Movie.2026.1080p.WEB-DL",
        size=2 * 1024**3,
        parsed=ParsedData(
            raw_title="Movie.2026.1080p.WEB-DL",
            resolution="1080p",
            codec="H.265",
            hdr=[],
            audio=["DDP"],
            channels=["5.1"],
            languages=["en"],
        ),
    )
    config = {
        "resultFormat": ["all"],
        "playbackProviders": [
            {
                "configurationId": provider_id,
                "displayName": "Easynews",
            }
        ],
    }

    stream = next(
        iter(
            _render_server_usenet_options(
                search_result,
                config,
                "https://comet.test",
                kodi=True,
            ).values()
        )
    )

    kodi_meta = stream["behaviorHints"]["cometKodiMetaV1"]
    assert kodi_meta["width"] == 1920
    assert kodi_meta["height"] == 1080
    assert kodi_meta["codec"] == "H.265"
    assert kodi_meta["languages"] == ["en"]
    assert kodi_meta["sizeInfo"] == "Size: 2.0 GB"
    assert kodi_meta["videoInfo"] == "H.265"
    assert kodi_meta["audioInfo"] == "DDP • 5.1"
    assert stream["name"] == "[Easynews NZB] 1080p | 2.0 GB | H.265 | DDP • 5.1"
    assert stream["description"] == (
        "Movie.2026.1080p.WEB-DL\n"
        "H.265 | DDP • 5.1\n"
        "Size: 2.0 GB Source: Usenet\n"
        "Languages: en"
    )
    assert "seeders" not in stream["description"].lower()
    assert "cached" not in stream["description"].lower()


def test_usenet_only_chilllink_keeps_server_urls_and_skips_client_handoffs():
    config = {
        "schemaVersion": 2,
        "enabledTransports": ["usenet"],
        "_debridEntries": [],
    }
    response = {
        "streams": [
            {
                "behaviorHints": {
                    "bingeGroup": "comet|nzbdav|candidate",
                    "filename": "Movie.2026.mkv",
                },
                "url": "https://comet.test/playback/v2/pi2.capability",
                "_chilllink": ["📰 NzbDAV", "Usenet"],
            },
            {
                "behaviorHints": {
                    "bingeGroup": "comet|stremio-nntp|candidate",
                    "filename": "Movie.2026.mkv",
                },
                "nzbUrl": "https://comet.test/nzb/intent.nzb",
                "servers": ["nntps://member:secret@news.test:563/4"],
            },
        ]
    }
    with (
        patch(
            "comet.api.endpoints.chilllink.config_check",
            return_value=config,
        ),
        patch(
            "comet.api.endpoints.chilllink.get_streams",
            new=AsyncMock(return_value=response),
        ) as get_streams,
    ):
        result = asyncio.run(
            chilllink_streams(
                _request(),
                BackgroundTasks(),
                imdbID="tt1234567",
                type="movie",
            )
        )

    assert result == {
        "sources": [
            {
                "id": "comet|nzbdav|candidate",
                "title": "Movie.2026.mkv",
                "url": "https://comet.test/playback/v2/pi2.capability",
                "metadata": ["📰 NzbDAV", "Usenet"],
            }
        ]
    }
    assert get_streams.await_args.kwargs["chilllink"] is True


def test_chilllink_rejects_noncanonical_or_over_domain_media_coordinates():
    with (
        patch(
            "comet.api.endpoints.chilllink.config_check",
        ) as config_check,
        patch(
            "comet.api.endpoints.chilllink.get_streams",
            new=AsyncMock(),
        ) as get_streams,
    ):
        for kwargs in (
            {"imdbID": "not-imdb", "type": "movie"},
            {"imdbID": "tt1234567", "type": "movie", "season": 1},
            {"imdbID": "tt1234567", "type": "series", "episode": 1},
            {"imdbID": "tt1234567", "type": "series", "season": 65_536},
        ):
            result = asyncio.run(
                chilllink_streams(
                    _request(),
                    BackgroundTasks(),
                    **kwargs,
                )
            )
            assert result == {"sources": []}

    config_check.assert_not_called()
    get_streams.assert_not_awaited()
