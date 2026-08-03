"""Bounded deterministic selection over Rust-parsed NZB file metadata."""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from RTN import parse

from comet.usenet.archive_paths import normalize_archive_relative_path
from comet.usenet.identity import archive_member_id as _archive_member_id
from comet.usenet.identity import is_sha256_hex as _is_sha256_hex
from comet.usenet.limits import (
    MAX_ARCHIVE_VOLUMES,
    MAX_NZB_FILES,
    MAX_USENET_LOGICAL_BYTES,
)
from comet.utils.parsing import is_video

MAX_CATALOG_FILES = MAX_NZB_FILES


_EPISODE_ABSOLUTE_RE = re.compile(
    r"\[\s*0*([0-9]+)(?:v\d+)?\s*\]"
    r"|(?:^|[._\s])(?:e|ep|episode)[._\-\s]*0*([0-9]+)(?:v\d+)?"
    r"(?:$|[._\-\s\[\]()])"
    r"|-\s*0*([0-9]+)(?:v\d+)?(?:$|[._\-\s\[\]()])",
    re.IGNORECASE,
)

_SAMPLE_RE = re.compile(
    r"(?:^|[\\/._\-\s])(?:sample|proof)(?:[\\/._\-\s]|$)", re.IGNORECASE
)
_SAMPLE_MIN_RELEASE_SHARE_DENOMINATOR = 20

_ARCHIVE_VOLUME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(.+)\.part([0-9]+)\.rar"), "rar_part"),
    (re.compile(r"(.+)\.r([0-9]{2})"), "rar_legacy"),
    (re.compile(r"(.+)\.7z\.([0-9]+)"), "seven_zip_split"),
    (re.compile(r"(.+)\.z([0-9]{2})"), "zip_split"),
    (re.compile(r"(.+)\.([0-9]+)"), "numeric_split"),
)


@dataclass(frozen=True, slots=True)
class UsenetAsset:
    asset_id: bytes
    file_index: int
    relative_path: str
    declared_bytes: int
    kind: str = "video"
    source_file_id: str | None = None


@dataclass(frozen=True, slots=True)
class NestedArchiveAsset(UsenetAsset):
    selected_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveVolumeGroup:
    selection_path: str
    volumes: tuple[UsenetAsset, ...]


class FileSelectionError(ValueError):
    """A stable fail-closed native file-selection error."""


def _asset_id(artifact_sha256: str, file_index: int, relative_path: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"comet-nzb-asset-v1\0")
    digest.update(bytes.fromhex(artifact_sha256))
    digest.update(file_index.to_bytes(4, "big"))
    encoded_path = relative_path.encode("utf-8")
    digest.update(len(encoded_path).to_bytes(4, "big"))
    digest.update(encoded_path)
    return digest.digest()


def _valid_logical_size(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= MAX_USENET_LOGICAL_BYTES
    )


def catalog_engine_source_assets(
    artifact_sha256: str, engine_assets: Sequence[object]
) -> tuple[UsenetAsset, ...]:
    if (
        not isinstance(artifact_sha256, str)
        or not _is_sha256_hex(artifact_sha256)
        or not isinstance(engine_assets, (list, tuple))
        or len(engine_assets) > MAX_CATALOG_FILES
    ):
        raise FileSelectionError("file_selection_invalid")
    assets = []
    file_indices = set()
    paths = set()
    for engine_asset in engine_assets:
        if (
            not isinstance(engine_asset, Mapping)
            or not {
                "asset_id",
                "file_index",
                "relative_path",
                "declared_bytes",
                "kind",
            }
            <= engine_asset.keys()
        ):
            raise FileSelectionError("file_selection_invalid")
        asset_id = engine_asset["asset_id"]
        file_index = engine_asset["file_index"]
        relative_path = engine_asset["relative_path"]
        declared_bytes = engine_asset["declared_bytes"]
        kind = engine_asset["kind"]
        if (
            not isinstance(asset_id, str)
            or not _is_sha256_hex(asset_id)
            or isinstance(file_index, bool)
            or not isinstance(file_index, int)
            or not 0 <= file_index < MAX_CATALOG_FILES
            or file_index in file_indices
            or not isinstance(relative_path, str)
            or normalize_archive_relative_path(relative_path) != relative_path
            or not _valid_logical_size(declared_bytes)
            or kind
            not in {
                "video",
                "archive",
                "split",
                "logical_split",
                "logical_archive",
                "par2",
                "par2_source",
            }
            or bytes.fromhex(asset_id)
            != _asset_id(artifact_sha256, file_index, relative_path)
            or relative_path.lower() in paths
        ):
            raise FileSelectionError("file_selection_invalid")
        file_indices.add(file_index)
        paths.add(relative_path.lower())
        assets.append(
            UsenetAsset(
                asset_id=bytes.fromhex(asset_id),
                file_index=file_index,
                relative_path=relative_path,
                declared_bytes=declared_bytes,
                kind=kind,
            )
        )
    return tuple(assets)


def catalog_par2_assets(
    set_id: str,
    slice_size: int,
    catalog_files: Sequence[object],
    *,
    known_video: tuple[str, int] | None = None,
) -> tuple[UsenetAsset, ...]:
    """Turn independently validated PAR2 descriptions into playable candidates."""
    if (
        not isinstance(set_id, str)
        or len(set_id) != 32
        or any(character not in "0123456789abcdef" for character in set_id)
        or isinstance(slice_size, bool)
        or not isinstance(slice_size, int)
        or not 4 <= slice_size <= 16 * 1024 * 1024
        or slice_size % 4 != 0
        or not isinstance(catalog_files, (list, tuple))
        or not 1 <= len(catalog_files) <= MAX_CATALOG_FILES
    ):
        raise FileSelectionError("file_selection_invalid")
    assets = []
    file_ids = set()
    paths = set()
    for file_index, catalog_file in enumerate(catalog_files):
        if (
            not isinstance(catalog_file, Mapping)
            or not {
                "file_id",
                "relative_path",
                "exact_size",
                "full_md5",
                "first_16k_md5",
                "slice_count",
            }
            <= catalog_file.keys()
        ):
            raise FileSelectionError("file_selection_invalid")
        file_id = catalog_file["file_id"]
        relative_path = catalog_file["relative_path"]
        exact_size = catalog_file["exact_size"]
        slice_count = catalog_file["slice_count"]
        if (
            not isinstance(file_id, str)
            or len(file_id) != 32
            or any(character not in "0123456789abcdef" for character in file_id)
            or file_id in file_ids
            or not isinstance(relative_path, str)
            or normalize_archive_relative_path(relative_path) != relative_path
            or relative_path.lower() in paths
            or not _valid_logical_size(exact_size)
            or any(
                not isinstance(catalog_file[field], str)
                or len(catalog_file[field]) != 32
                or any(
                    character not in "0123456789abcdef"
                    for character in catalog_file[field]
                )
                for field in ("full_md5", "first_16k_md5")
            )
            or isinstance(slice_count, bool)
            or not isinstance(slice_count, int)
            or not 1 <= slice_count <= 32_768
            or slice_count != (exact_size + slice_size - 1) // slice_size
        ):
            raise FileSelectionError("file_selection_invalid")
        file_ids.add(file_id)
        paths.add(relative_path.lower())
        if known_video == (relative_path, exact_size) or is_video(relative_path):
            kind = "video"
        elif _archive_volume_hint(relative_path) is not None:
            kind = "archive"
        else:
            continue
        digest = hashlib.sha256()
        digest.update(b"comet-par2-source-asset-v1\0")
        digest.update(bytes.fromhex(set_id))
        digest.update(bytes.fromhex(file_id))
        assets.append(
            UsenetAsset(
                asset_id=digest.digest(),
                file_index=file_index,
                relative_path=relative_path,
                declared_bytes=exact_size,
                kind=kind,
                source_file_id=file_id,
            )
        )
    return tuple(assets)


def catalog_par2_source_assets(
    set_id: str, slice_size: int, catalog_files: Sequence[object]
) -> tuple[UsenetAsset, ...]:
    return tuple(
        asset
        for asset in catalog_par2_assets(set_id, slice_size, catalog_files)
        if asset.kind == "archive"
    )


def catalog_archive_members(
    set_identity: str, members: Sequence[object]
) -> tuple[UsenetAsset, ...]:
    if (
        not isinstance(set_identity, str)
        or not _is_sha256_hex(set_identity)
        or not isinstance(members, (list, tuple))
        or len(members) > MAX_CATALOG_FILES
    ):
        raise FileSelectionError("file_selection_invalid")
    assets = []
    paths = set()
    for file_index, member in enumerate(members):
        if (
            not isinstance(member, Mapping)
            or not {
                "member_id",
                "relative_path",
                "exact_size",
                "kind",
            }
            <= member.keys()
        ):
            raise FileSelectionError("file_selection_invalid")
        member_id = member["member_id"]
        relative_path = member["relative_path"]
        exact_size = member["exact_size"]
        kind = member["kind"]
        if (
            not isinstance(member_id, str)
            or not _is_sha256_hex(member_id)
            or normalize_archive_relative_path(relative_path) != relative_path
            or not _valid_logical_size(exact_size)
            or kind not in {"video", "archive", "split", "par2"}
            or _archive_member_id(set_identity, relative_path, exact_size)
            != bytes.fromhex(member_id)
            or relative_path.lower() in paths
        ):
            raise FileSelectionError("file_selection_invalid")
        paths.add(relative_path.lower())
        if kind == "video":
            assets.append(
                UsenetAsset(
                    asset_id=bytes.fromhex(member_id),
                    file_index=file_index,
                    relative_path=relative_path,
                    declared_bytes=exact_size,
                    kind=kind,
                )
            )
    return tuple(assets)


def catalog_nested_archive_members(
    set_identity: str, members: Sequence[object]
) -> tuple[NestedArchiveAsset, ...]:
    if (
        not isinstance(set_identity, str)
        or not _is_sha256_hex(set_identity)
        or not isinstance(members, (list, tuple))
        or len(members) > MAX_CATALOG_FILES
    ):
        raise FileSelectionError("file_selection_invalid")
    assets = []
    paths = set()
    for file_index, member in enumerate(members):
        if (
            not isinstance(member, Mapping)
            or not {
                "member_id",
                "relative_path",
                "exact_size",
                "kind",
                "selected_paths",
            }
            <= member.keys()
        ):
            raise FileSelectionError("file_selection_invalid")
        member_id = member["member_id"]
        relative_path = member["relative_path"]
        exact_size = member["exact_size"]
        kind = member["kind"]
        selected_paths = member["selected_paths"]
        if (
            not isinstance(member_id, str)
            or not _is_sha256_hex(member_id)
            or normalize_archive_relative_path(relative_path) != relative_path
            or not _valid_logical_size(exact_size)
            or kind != "video"
            or not isinstance(selected_paths, (list, tuple))
            or not 1 <= len(selected_paths) <= 4
            or any(
                normalize_archive_relative_path(selected_path) != selected_path
                for selected_path in selected_paths
            )
            or relative_path != "!/".join(selected_paths)
            or _archive_member_id(set_identity, relative_path, exact_size)
            != bytes.fromhex(member_id)
            or relative_path.lower() in paths
        ):
            raise FileSelectionError("file_selection_invalid")
        paths.add(relative_path.lower())
        assets.append(
            NestedArchiveAsset(
                asset_id=bytes.fromhex(member_id),
                file_index=file_index,
                relative_path=relative_path,
                declared_bytes=exact_size,
                kind=kind,
                selected_paths=tuple(selected_paths),
            )
        )
    return tuple(assets)


def matches_episode_path(relative_path: str, season: int, episode: int) -> bool:
    parsed = parse(relative_path)
    if season in parsed.seasons and episode in parsed.episodes:
        return True
    if season != 0:
        return False
    stem = relative_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    pos = 0
    while True:
        match = _EPISODE_ABSOLUTE_RE.search(stem, pos)
        if match is None:
            return False
        captured = match.group(1) or match.group(2) or match.group(3)
        if captured is not None and int(captured) == episode:
            return True
        pos = match.start() + 1


def catalog_archive_volume_groups(
    assets: Sequence[UsenetAsset],
) -> tuple[ArchiveVolumeGroup, ...]:
    groups: dict[tuple[str, str], list[tuple[int, UsenetAsset]]] = {}
    for asset in assets:
        if asset.kind not in {
            "archive",
            "split",
            "logical_split",
            "logical_archive",
        }:
            continue
        hint = _asset_volume_hint(asset)
        if hint is None:
            raise FileSelectionError("file_selection_invalid")
        scheme, base, number, _selection_path = hint
        groups.setdefault((scheme, base), []).append((number, asset))
    candidates = []
    for entries in groups.values():
        entries.sort(
            key=lambda entry: (
                entry[0],
                entry[1].relative_path.lower(),
                entry[1].relative_path,
                entry[1].file_index,
            )
        )
        if len(entries) > MAX_ARCHIVE_VOLUMES or any(
            left[0] == right[0] for left, right in itertools.pairwise(entries)
        ):
            raise FileSelectionError("file_selection_invalid")
        selection_path = _asset_volume_hint(entries[0][1])[3]
        candidates.append(
            ArchiveVolumeGroup(
                selection_path=selection_path,
                volumes=tuple(asset for _number, asset in entries),
            )
        )
    return tuple(candidates)


def select_archive_volume_groups(
    candidates: Sequence[ArchiveVolumeGroup],
    selection_intent: tuple[object, ...],
) -> ArchiveVolumeGroup:
    if not candidates:
        raise FileSelectionError("file_selection_ambiguous")
    if selection_intent == (0,):
        return min(
            candidates,
            key=lambda group: (
                -sum(asset.declared_bytes for asset in group.volumes),
                group.selection_path.lower(),
                group.selection_path,
                group.volumes[0].file_index,
            ),
        )
    if (
        len(selection_intent) == 3
        and selection_intent[0] == 1
        and not isinstance(selection_intent[1], bool)
        and isinstance(selection_intent[1], int)
        and not isinstance(selection_intent[2], bool)
        and isinstance(selection_intent[2], int)
        and 0 <= selection_intent[1] <= 65_535
        and 0 <= selection_intent[2] <= 65_535
    ):
        matches = [
            group
            for group in candidates
            if matches_episode_path(
                group.selection_path, selection_intent[1], selection_intent[2]
            )
        ]
        if not matches and len(candidates) == 1:
            matches = candidates
    elif (
        len(selection_intent) == 2
        and selection_intent[0] == 2
        and isinstance(selection_intent[1], bytes)
        and len(selection_intent[1]) == 32
    ):
        matches = [
            group
            for group in candidates
            if any(asset.asset_id == selection_intent[1] for asset in group.volumes)
        ]
    else:
        raise FileSelectionError("file_selection_ambiguous")
    if len(matches) != 1:
        raise FileSelectionError("file_selection_ambiguous")
    return matches[0]


def select_archive_volume_group(
    assets: Sequence[UsenetAsset], selection_intent: tuple[object, ...]
) -> ArchiveVolumeGroup:
    return select_archive_volume_groups(
        catalog_archive_volume_groups(assets),
        selection_intent,
    )


def _archive_volume_hint(
    relative_path: str,
) -> tuple[str, str, int, str] | None:
    lower = relative_path.lower()
    rar_part = _ARCHIVE_VOLUME_PATTERNS[0]
    match = rar_part[0].fullmatch(lower)
    if match is not None:
        return rar_part[1], match[1], int(match[2]), match[1]
    if lower.endswith(".rar"):
        return "rar_legacy", lower[:-4], 0, relative_path[:-4]
    rar_split = _ARCHIVE_VOLUME_PATTERNS[1]
    match = rar_split[0].fullmatch(lower)
    if match is not None:
        return rar_split[1], match[1], int(match[2]) + 1, match[1]
    for pattern, scheme in _ARCHIVE_VOLUME_PATTERNS[2:]:
        match = pattern.fullmatch(lower)
        if match is not None:
            return scheme, match[1], int(match[2]), match[1]
    if lower.endswith((".7z", ".zip", ".tar", ".tar.gz", ".tgz")):
        return "single", lower, 0, relative_path
    return None


def _asset_volume_hint(
    asset: UsenetAsset,
) -> tuple[str, str, int, str] | None:
    if asset.kind == "logical_split":
        return _logical_split_hint(asset.relative_path)
    if asset.kind == "logical_archive":
        return _logical_archive_hint(asset)
    return _archive_volume_hint(asset.relative_path)


def _logical_split_hint(
    relative_path: str,
) -> tuple[str, str, int, str] | None:
    selection_path, separator, suffix = relative_path.rpartition("/part.")
    if separator and len(suffix) == 3 and suffix.isascii() and suffix.isdigit():
        return (
            "logical_split",
            selection_path.casefold(),
            int(suffix),
            selection_path,
        )
    return None


def _logical_archive_hint(
    asset: UsenetAsset,
) -> tuple[str, str, int, str] | None:
    selection_path, separator, archive_path = asset.relative_path.partition("/archive/")
    if separator and archive_path:
        return (
            "logical_archive",
            selection_path.casefold(),
            asset.file_index,
            selection_path,
        )
    return None


def select_asset(
    assets: Sequence[UsenetAsset], selection_intent: tuple[object, ...]
) -> UsenetAsset:
    if not assets:
        raise FileSelectionError("file_selection_ambiguous")
    if selection_intent == (0,):
        return min(
            assets,
            key=lambda asset: (
                -asset.declared_bytes,
                asset.relative_path.casefold(),
                asset.relative_path,
                asset.file_index,
            ),
        )
    if (
        len(selection_intent) == 3
        and selection_intent[0] == 1
        and not isinstance(selection_intent[1], bool)
        and isinstance(selection_intent[1], int)
        and not isinstance(selection_intent[2], bool)
        and isinstance(selection_intent[2], int)
        and 0 <= selection_intent[1] <= 65_535
        and 0 <= selection_intent[2] <= 65_535
    ):
        matches = [
            asset
            for asset in assets
            if matches_episode_path(
                asset.relative_path, selection_intent[1], selection_intent[2]
            )
        ]
        if not matches and len(assets) == 1:
            matches = [assets[0]]
    elif (
        len(selection_intent) == 2
        and selection_intent[0] == 2
        and isinstance(selection_intent[1], bytes)
        and len(selection_intent[1]) == 32
    ):
        matches = [asset for asset in assets if asset.asset_id == selection_intent[1]]
    else:
        raise FileSelectionError("file_selection_ambiguous")
    if len(matches) != 1:
        raise FileSelectionError("file_selection_ambiguous")
    return matches[0]


def eligible_video_assets(
    assets: Sequence[UsenetAsset],
) -> tuple[UsenetAsset, ...]:
    videos = tuple(asset for asset in assets if asset.kind == "video")
    features = tuple(
        asset for asset in videos if _SAMPLE_RE.search(asset.relative_path) is None
    )
    if features:
        return features
    release_bytes = sum(asset.declared_bytes for asset in assets)
    return tuple(
        asset
        for asset in videos
        if asset.declared_bytes * _SAMPLE_MIN_RELEASE_SHARE_DENOMINATOR >= release_bytes
    )


def select_remote_video_file[RemoteFileT](
    files: Iterable[tuple[RemoteFileT, str, int]],
    selection_intent: tuple[object, ...],
) -> RemoteFileT:
    """Apply the canonical selector to a provider's downloaded file list."""
    candidates = tuple(file for file in files if is_video(file[1]))
    selected = select_asset(
        tuple(
            UsenetAsset(
                position.to_bytes(32, "big"),
                position,
                path,
                size,
            )
            for position, (_file, path, size) in enumerate(candidates)
        ),
        selection_intent,
    )
    return candidates[selected.file_index][0]
