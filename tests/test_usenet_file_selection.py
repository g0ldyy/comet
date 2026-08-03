import hashlib

import pytest

from comet.usenet.file_selection import (
    FileSelectionError,
    catalog_archive_members,
    catalog_archive_volume_groups,
    catalog_engine_source_assets,
    eligible_video_assets,
    catalog_nested_archive_members,
    catalog_par2_assets,
    catalog_par2_source_assets,
    select_archive_volume_group,
    select_archive_volume_groups,
    select_asset,
)
from comet.usenet.limits import MAX_USENET_LOGICAL_BYTES

ARTIFACT = "a" * 64


def _asset(name: str, size=100, *, index=0, kind="video"):
    digest = hashlib.sha256()
    digest.update(b"comet-nzb-asset-v1\0")
    digest.update(bytes.fromhex(ARTIFACT))
    digest.update(index.to_bytes(4, "big"))
    path = name.encode()
    digest.update(len(path).to_bytes(4, "big"))
    digest.update(path)
    return {
        "asset_id": digest.hexdigest(),
        "file_index": index,
        "relative_path": name,
        "declared_bytes": size,
        "kind": kind,
    }


def test_catalog_accepts_only_exact_engine_ids_and_video_assets():
    engine_assets = [
        _asset("release.par2", 50, index=0, kind="par2"),
        _asset("Movie.mkv", 300, index=1),
    ]

    assets = tuple(
        asset
        for asset in catalog_engine_source_assets(ARTIFACT, engine_assets)
        if asset.kind == "video"
    )

    assert len(assets) == 1
    assert assets[0].relative_path == "Movie.mkv"
    assert assets[0].declared_bytes == 300
    assert len(assets[0].asset_id) == 32
    assert [
        asset.kind for asset in catalog_engine_source_assets(ARTIFACT, engine_assets)
    ] == [
        "par2",
        "video",
    ]


def test_catalogs_trust_native_video_classification_for_obfuscated_names():
    source = catalog_engine_source_assets(
        ARTIFACT,
        [_asset("4f6a9d2c", 300, index=0)],
    )
    assert source[0].relative_path == "4f6a9d2c"

    set_identity = "b" * 64
    archive = catalog_archive_members(
        set_identity,
        [_archive_member(set_identity, "8e12cfa4", 300)],
    )
    assert archive[0].relative_path == "8e12cfa4"

    nested_member = _archive_member(
        set_identity,
        "payload.bin!/9ab372e1",
        300,
    )
    nested_member["selected_paths"] = ["payload.bin", "9ab372e1"]
    nested = catalog_nested_archive_members(set_identity, [nested_member])
    assert nested[0].relative_path == "payload.bin!/9ab372e1"


def test_movie_selection_prefers_largest_then_uses_a_stable_path_tie_break():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("zeta.mkv", 200, index=0),
            _asset("alpha.mkv", 200, index=1),
            _asset("small.mkv", 100, index=2),
        ],
    )

    selected = select_asset(assets, (0,))

    assert selected.relative_path == "alpha.mkv"


def test_episode_selection_handles_packs_multi_episode_and_ambiguity():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("Show.S01E01.mkv", index=0),
            _asset("Show.S01E02E03.mkv", index=1),
            _asset("Show.S02E02.mkv", index=2),
        ],
    )

    assert select_asset(assets, (1, 1, 2)).relative_path == "Show.S01E02E03.mkv"
    assert select_asset(assets, (1, 1, 3)).relative_path == "Show.S01E02E03.mkv"
    with pytest.raises(FileSelectionError, match="file_selection_ambiguous"):
        select_asset(assets + (assets[1],), (1, 1, 2))
    with pytest.raises(FileSelectionError, match="file_selection_ambiguous"):
        select_asset(assets, (1, 3, 9))


def test_anime_absolute_and_explicit_asset_selection_are_exact():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("Anime - 123 [1080p].mkv", index=0),
            _asset("Anime.2026.1080p.mkv", index=1),
        ],
    )

    anime = select_asset(assets, (1, 0, 123))
    assert anime.relative_path == "Anime - 123 [1080p].mkv"
    assert select_asset(assets, (2, anime.asset_id)) == anime
    with pytest.raises(FileSelectionError, match="file_selection_ambiguous"):
        select_asset(assets, (1, 0, 2026))
    with pytest.raises(FileSelectionError, match="file_selection_ambiguous"):
        select_asset(assets, (2, b"x" * 32))


@pytest.mark.parametrize(
    "invalid",
    [
        {**_asset("../Movie.mkv"), "asset_id": "0" * 64},
        {**_asset("Movie.Sample.mkv"), "asset_id": "0" * 64},
        {**_asset("Movie.mkv"), "asset_id": "f" * 64},
        {**_asset("Movie.mkv"), "declared_bytes": 0},
        {
            **_asset("Movie.mkv"),
            "declared_bytes": MAX_USENET_LOGICAL_BYTES + 1,
        },
        {**_asset("Movie.mkv"), "kind": "unknown"},
    ],
)
def test_catalog_rejects_malformed_or_untrusted_engine_assets(invalid):
    with pytest.raises(FileSelectionError, match="file_selection_invalid"):
        catalog_engine_source_assets(ARTIFACT, [invalid])


def test_archive_volume_group_selection_is_target_scoped_and_only_a_hint():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("Show.S01E01.part02.rar", 20, index=0, kind="archive"),
            _asset("Show.S01E01.part01.rar", 10, index=1, kind="archive"),
            _asset("Show.S01E02.001", 30, index=2, kind="split"),
            _asset("Show.S01E02.002", 40, index=3, kind="split"),
            _asset("release.par2", 50, index=4, kind="par2"),
        ],
    )

    selected = select_archive_volume_group(assets, (1, 1, 2))

    assert selected.selection_path == "show.s01e02"
    assert [asset.relative_path for asset in selected.volumes] == [
        "Show.S01E02.001",
        "Show.S01E02.002",
    ]


def test_archive_volume_group_supports_large_real_world_rar_sets():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset(
                f"Movie.2024.part{number:03}.rar",
                index=number - 1,
                kind="archive",
            )
            for number in range(1, 100)
        ],
    )

    selected = select_archive_volume_group(assets, (0,))

    assert len(selected.volumes) == 99
    assert selected.volumes[0].relative_path == "Movie.2024.part001.rar"
    assert selected.volumes[-1].relative_path == "Movie.2024.part099.rar"


def test_small_sample_does_not_hide_archive_feature():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("Movie.2024-sample.mkv", 250, index=0, kind="video"),
            _asset("Movie.2024.rar", 10_000, index=1, kind="archive"),
            _asset("Movie.2024.r00", 10_000, index=2, kind="archive"),
        ],
    )

    assert eligible_video_assets(assets) == ()


def test_feature_videos_take_priority_over_samples():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("Movie.2024-proof.mkv", 500, index=0, kind="video"),
            _asset("Movie.2024.mkv", 100, index=1, kind="video"),
        ],
    )

    assert eligible_video_assets(assets) == (assets[1],)


def test_plausible_standalone_sample_remains_playable():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("Movie.2024.sample.mkv", 100, index=0, kind="video"),
            _asset("Movie.2024.par2", 1, index=1, kind="par2"),
        ],
    )

    assert eligible_video_assets(assets) == (assets[0],)


def test_archive_volume_group_accepts_non_padded_numeric_parts():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset(
                f"obfuscated.{number}",
                index=number - 10,
                kind="split",
            )
            for number in range(10, 94)
        ],
    )

    selected = select_archive_volume_group(assets, (0,))

    assert selected.selection_path == "obfuscated"
    assert len(selected.volumes) == 84
    assert selected.volumes[0].relative_path == "obfuscated.10"
    assert selected.volumes[-1].relative_path == "obfuscated.93"


def test_logical_split_group_preserves_the_owned_video_path_and_manifest_order():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset(
                f"Movie.2026.MKV/part.{number:03}",
                index=number - 1,
                kind="logical_split",
            )
            for number in range(1, 4)
        ],
    )

    selected = select_archive_volume_group(assets, (0,))

    assert selected.selection_path == "Movie.2026.MKV"
    assert [asset.file_index for asset in selected.volumes] == [0, 1, 2]


def test_logical_archive_group_ignores_obfuscated_volume_names():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset(
                f"Movie.2026.mkv/archive/{name}.rar",
                index=index,
                kind="logical_archive",
            )
            for index, name in enumerate(("random-z", "random-a", "random-q"))
        ],
    )

    selected = select_archive_volume_group(assets, (0,))

    assert selected.selection_path == "Movie.2026.mkv"
    assert [asset.file_index for asset in selected.volumes] == [0, 1, 2]


def test_archive_volume_group_rejects_ambiguous_or_conflicting_hints():
    assets = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("opaque-a.rar", index=0, kind="archive"),
            _asset("opaque-b.rar", index=1, kind="archive"),
        ],
    )
    with pytest.raises(FileSelectionError, match="file_selection_ambiguous"):
        select_archive_volume_group(assets, (1, 1, 2))

    duplicate = catalog_engine_source_assets(
        ARTIFACT,
        [
            _asset("release.part01.rar", index=0, kind="archive"),
            _asset("release.part1.rar", index=1, kind="archive"),
        ],
    )
    with pytest.raises(FileSelectionError, match="file_selection_invalid"):
        select_archive_volume_group(duplicate, (0,))


def test_archive_groups_from_independent_par2_sets_do_not_merge_by_path():
    def source(set_id, file_id, size):
        return catalog_par2_source_assets(
            set_id,
            1024,
            [
                {
                    "file_id": file_id,
                    "relative_path": "release.rar",
                    "exact_size": size,
                    "full_md5": "3" * 32,
                    "first_16k_md5": "4" * 32,
                    "slice_count": 1,
                }
            ],
        )

    first = catalog_archive_volume_groups(source("1" * 32, "2" * 32, 100))
    second = catalog_archive_volume_groups(source("5" * 32, "6" * 32, 200))

    selected = select_archive_volume_groups((*first, *second), (0,))

    assert selected == second[0]


def test_par2_source_catalog_uses_file_ids_and_ignores_non_archive_descriptions():
    files = [
        {
            "file_id": "1" * 32,
            "relative_path": "Movie.mkv",
            "exact_size": 1_000,
            "full_md5": "2" * 32,
            "first_16k_md5": "3" * 32,
            "slice_count": 1,
        },
        {
            "file_id": "3" * 32,
            "relative_path": "The.Sample.2026.mkv",
            "exact_size": 900,
            "full_md5": "4" * 32,
            "first_16k_md5": "5" * 32,
            "slice_count": 1,
        },
        {
            "file_id": "4" * 32,
            "relative_path": "release.part01.rar",
            "exact_size": 100,
            "full_md5": "6" * 32,
            "first_16k_md5": "7" * 32,
            "slice_count": 1,
        },
    ]

    assets = catalog_par2_source_assets("a" * 32, 1024, files)
    all_assets = catalog_par2_assets("a" * 32, 1024, files)

    assert len(assets) == 1
    assert [asset.kind for asset in all_assets] == ["video", "video", "archive"]
    assert assets[0].relative_path == "release.part01.rar"
    assert assets[0].source_file_id == "4" * 32
    assert len(assets[0].asset_id) == 32
    assert assets == catalog_par2_source_assets("a" * 32, 1024, files)
    assert catalog_par2_assets("a" * 32, 1024, list(reversed(files)))
    with pytest.raises(FileSelectionError, match="file_selection_invalid"):
        catalog_par2_assets(
            "a" * 32,
            1024,
            [files[0], {**files[1], "file_id": files[0]["file_id"]}],
        )
    with pytest.raises(FileSelectionError, match="file_selection_invalid"):
        catalog_par2_source_assets(
            "a" * 32,
            1024,
            [{**files[0], "relative_path": "../Movie.mkv"}],
        )


def test_par2_catalog_binds_an_opaque_video_only_to_the_exact_known_source():
    opaque = {
        "file_id": "1" * 32,
        "relative_path": "4f6a9d2c",
        "exact_size": 1_000,
        "full_md5": "2" * 32,
        "first_16k_md5": "3" * 32,
        "slice_count": 1,
    }

    assert not catalog_par2_assets("a" * 32, 1024, [opaque])
    assets = catalog_par2_assets(
        "a" * 32,
        1024,
        [opaque],
        known_video=("4f6a9d2c", 1_000),
    )

    assert len(assets) == 1
    assert assets[0].kind == "video"
    assert assets[0].relative_path == "4f6a9d2c"
    assert not catalog_par2_assets(
        "a" * 32,
        1024,
        [opaque],
        known_video=("4f6a9d2c", 999),
    )


def _archive_member(set_identity, path, size, *, kind="video"):
    encoded = path.encode()
    digest = hashlib.sha256()
    digest.update(b"comet-archive-member-v1\0")
    digest.update(set_identity.encode())
    digest.update(len(encoded).to_bytes(4, "big"))
    digest.update(encoded)
    digest.update(size.to_bytes(8, "big"))
    return {
        "member_id": digest.hexdigest(),
        "relative_path": path,
        "exact_size": size,
        "kind": kind,
    }


def test_archive_members_reuse_exact_episode_and_movie_selection():
    set_identity = "b" * 64
    members = [
        _archive_member(set_identity, "metadata.par2", 10, kind="par2"),
        _archive_member(set_identity, "Show.S01E01.mkv", 100),
        _archive_member(set_identity, "Show.S01E02E03.mkv", 200),
    ]

    assets = catalog_archive_members(set_identity, members)

    assert select_asset(assets, (1, 1, 2)).relative_path == "Show.S01E02E03.mkv"
    assert select_asset(assets, (0,)).relative_path == "Show.S01E02E03.mkv"
    assert [asset.kind for asset in assets] == ["video", "video"]


def test_archive_member_catalog_revalidates_set_bound_ids_and_accepts_order():
    set_identity = "b" * 64
    member = _archive_member(set_identity, "Movie.mkv", 100)
    with pytest.raises(FileSelectionError, match="file_selection_invalid"):
        catalog_archive_members(set_identity, [{**member, "member_id": "c" * 64}])
    assets = catalog_archive_members(
        set_identity,
        [
            _archive_member(set_identity, "z.mkv", 100),
            _archive_member(set_identity, "a.mkv", 100),
        ],
    )
    assert [asset.relative_path for asset in assets] == ["z.mkv", "a.mkv"]


def test_nested_archive_members_bind_the_display_path_to_the_exact_layer_chain():
    set_identity = "b" * 64
    member = _archive_member(
        set_identity,
        "payload.tar.gz!/Movie.2026.mkv",
        100,
    )
    member["selected_paths"] = ["payload.tar.gz", "Movie.2026.mkv"]

    assets = catalog_nested_archive_members(set_identity, [member])

    assert assets[0].relative_path == "payload.tar.gz!/Movie.2026.mkv"
    assert assets[0].selected_paths == ("payload.tar.gz", "Movie.2026.mkv")
    with pytest.raises(FileSelectionError, match="file_selection_invalid"):
        catalog_nested_archive_members(
            set_identity,
            [{**member, "selected_paths": ["other.zip", "Movie.2026.mkv"]}],
        )
