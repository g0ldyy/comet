import asyncio
import re

import pytest

from comet.playback.providers.stremio_nntp import (
    StremioNntpHandoff,
    StremioNntpProvider,
    handoff_selector,
    validate_handoff_manifest,
)
from comet.usenet.limits import MAX_NZB_FILES
from comet.usenet.stremio_nntp_config import (
    serialize_servers,
    validate_serialized_servers,
)


def _handoff_options(*, tls_mode="implicit_tls"):
    return {
        "servers": [
            {
                "host": "news.test",
                "port": 563,
                "tls_mode": tls_mode,
                "username": "user",
                "password": "password",
                "connections": 1,
            }
        ],
    }


def test_stremio_nntp_handoff_renders_one_valid_selector():
    handoff = StremioNntpHandoff(
        "https://comet.test/nzb", ("nntps://user:password@news.test:563/4",), file_idx=2
    )
    assert handoff.render() == {
        "nzbUrl": "https://comet.test/nzb",
        "servers": ["nntps://user:password@news.test:563/4"],
        "fileIdx": 2,
    }


def test_stremio_nntp_handoff_rejects_ambiguous_selectors():
    with pytest.raises(ValueError):
        StremioNntpHandoff(
            "https://comet.test/nzb",
            ("nntps://user:password@news.test:563/4",),
            file_idx=2,
            file_must_include="episode",
        )


@pytest.mark.parametrize(
    ("nzb_url", "servers", "file_idx", "file_must_include"),
    [
        (
            "https://user:secret@comet.test/nzb",
            ("nntps://user:password@news.test:563/4",),
            2,
            None,
        ),
        (
            "https://comet.test/nzb#fragment",
            ("nntps://user:password@news.test:563/4",),
            2,
            None,
        ),
        (
            "https://comet.test/nzb",
            ("nntps://user:password@news.test:563/4?extra=true",),
            2,
            None,
        ),
        (
            "https://comet.test/nzb",
            ("nntps://user:password@news.test:563/04",),
            2,
            None,
        ),
        (
            "https://comet.test/nzb",
            ("nntps://user:password@news.test:563/4",),
            True,
            None,
        ),
        (
            "https://comet.test/nzb",
            ("nntps://user:password@news.test:563/4",),
            MAX_NZB_FILES,
            None,
        ),
        (
            "https://comet.test/nzb",
            ("nntps://user:password@news.test:563/4",),
            None,
            "/(/i",
        ),
    ],
)
def test_stremio_nntp_handoff_rejects_noncanonical_wire_values(
    nzb_url,
    servers,
    file_idx,
    file_must_include,
):
    with pytest.raises(ValueError):
        StremioNntpHandoff(
            nzb_url,
            servers,
            file_idx=file_idx,
            file_must_include=file_must_include,
        )


def test_stremio_nntp_handoff_accepts_signed_query_urls():
    handoff = StremioNntpHandoff(
        "https://comet.test/nzb?token=secret",
        ("nntps://user:password@news.test:563/4",),
        file_idx=2,
    )

    assert handoff.nzb_url == "https://comet.test/nzb?token=secret"


def test_lazy_handoff_selector_is_deterministic_and_manifest_checked():
    movie_selector = handoff_selector(
        "Movie.2024.1080p.nzb",
        (0,),
    )
    assert movie_selector == (
        r"/Movie\.2024\.1080p.*?\.(?:mkv|mp4|m4v|mov|webm|avi|ts|m2ts|mpg|mpeg|wmv|flv)"
        r"(?:[^A-Z0-9]|$)/i"
    )
    episode_selector = handoff_selector("ignored", (1, 2, 3))
    assert episode_selector is not None
    assert re.search(episode_selector[1:-2], "Show.2x03.1080p.mkv", re.IGNORECASE)
    assert handoff_selector("ignored", (2, b"a" * 32)) is None

    validate_handoff_manifest(
        [
            {
                "subject": '"Movie.2024.1080p.mkv" yEnc (1/10)',
                "postings": [],
            },
            {
                "subject": '"Movie.2024.1080p.par2" yEnc (1/1)',
                "postings": [],
            },
        ],
        movie_selector,
    )
    with pytest.raises(ValueError, match="ambiguous"):
        validate_handoff_manifest(
            [
                {"subject": '"Show.S02E03.1080p.mkv" yEnc', "postings": []},
                {"subject": '"Show.S02E03.720p.mkv" yEnc', "postings": []},
            ],
            episode_selector,
        )


def test_lazy_handoff_preserves_a_deterministic_archive_member_selector():
    selector = handoff_selector("ignored", (1, 2, 3))
    assert selector is not None

    validate_handoff_manifest(
        [
            {"subject": '"Show.S02E03.part01.rar" yEnc', "postings": []},
            {"subject": '"Show.S02E03.part02.rar" yEnc', "postings": []},
            {"subject": '"Show.S02E03.par2" yEnc', "postings": []},
        ],
        selector,
    )


def test_lazy_handoff_accepts_only_an_exact_single_file_easynews_hint():
    selector = handoff_selector("Movie.2026", (0,))
    assert selector is not None
    manifest = [
        {
            "subject": "generic",
            "postings": [
                {"number": 1, "bytes": 100, "message_id": "first"},
                {"number": 2, "bytes": 200, "message_id": "second"},
            ],
        }
    ]

    validate_handoff_manifest(
        manifest,
        selector,
        ("Movie.2026.mkv", 300),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        validate_handoff_manifest(
            manifest,
            selector,
            ("Movie.2026.mkv", 301),
        )
    with pytest.raises(ValueError, match="ambiguous"):
        validate_handoff_manifest(
            [
                *manifest,
                {
                    "subject": "other",
                    "postings": [{"number": 1, "bytes": 1, "message_id": "third"}],
                },
            ],
            selector,
            ("Movie.2026.mkv", 300),
        )


def test_lazy_handoff_does_not_depend_on_release_name_heuristics():
    selector = handoff_selector("Movie.2026", (0,))
    assert selector is not None
    manifest = [{"subject": '"Movie.2026.mkv" \x7f yEnc', "postings": []}]

    validate_handoff_manifest(manifest, selector)
    validate_handoff_manifest(
        [{"subject": "generic", "postings": [{"number": 1, "bytes": 1}]}],
        selector,
        ("Movie.2026.mkv", 1),
    )


def test_resolved_handoff_selects_the_only_direct_video_not_its_sidecars():
    handoff = StremioNntpProvider().render_client_delegated(
        _handoff_options(),
        "https://comet.test/nzb",
        [
            {"subject": '"Movie.2024.par2" yEnc', "postings": []},
            {"subject": '"Movie.2024.mkv" yEnc', "postings": []},
        ],
    )

    assert handoff["fileIdx"] == 1


def test_resolved_handoff_projects_the_requested_episode_inside_a_pack():
    config = _handoff_options()
    selector = handoff_selector("ignored", (1, 2, 3))

    handoff = StremioNntpProvider().render_resolved(
        config,
        "https://comet.test/nzb",
        [
            {"subject": '"Show.S02E02.mkv" yEnc', "postings": []},
            {"subject": '"Show.S02E03.mkv" yEnc', "postings": []},
        ],
        selector,
    )

    assert handoff["fileIdx"] == 1
    assert "fileMustInclude" not in handoff


def test_stremio_nntp_server_serialization_escapes_credentials_and_ipv6():
    assert serialize_servers(
        [
            {
                "host": "2001:db8::1",
                "port": 563,
                "tls_mode": "implicit_tls",
                "username": "user@example",
                "password": "pa:ss/word",
                "connections": 4,
            }
        ]
    ) == ("nntps://user%40example:pa%3Ass%2Fword@[2001:db8::1]:563/4",)


def test_stremio_nntp_collapses_duplicate_servers():
    server = _handoff_options()["servers"][0]
    serialized = serialize_servers([server, server])

    assert len(serialized) == 1
    assert validate_serialized_servers(serialized * 2) == serialized


def test_stremio_nntp_rejects_starttls_and_ambiguous_hosts():
    with pytest.raises(ValueError):
        serialize_servers(
            [
                {
                    "host": "news.test/path",
                    "port": 563,
                    "tls_mode": "starttls",
                    "username": "user",
                    "password": "password",
                    "connections": 1,
                }
            ]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "news..test"),
        ("host", "news.test."),
        ("host", "fe80::1%eth0"),
        ("username", "member\x7f"),
        ("username", "member name"),
        ("password", "é" * 257),
    ],
)
def test_stremio_nntp_reuses_bounded_canonical_server_validation(field, value):
    server = _handoff_options()["servers"][0]
    server[field] = value

    with pytest.raises(ValueError):
        serialize_servers([server])


def test_stremio_nntp_accepts_tls_and_plaintext_servers_without_redundant_consent():
    async def run():
        provider = StremioNntpProvider()
        status = await provider.validate_config(_handoff_options())
        assert status.code is None
        status = await provider.validate_config(_handoff_options(tls_mode="plaintext"))
        assert status.code is None

        status = await provider.validate_config({"servers": []})
        assert status.code == "nntp_servers_required"

    asyncio.run(run())
