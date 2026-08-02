"""Stremio-native NNTP handoff; no server-side media resolution occurs here."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from comet.core.sources import MAX_SIGNED_BIGINT
from comet.playback.base import (
    Actionability,
    BytePath,
    ProviderDescriptor,
    ProviderStatus,
    Readiness,
)
from comet.usenet.limits import MAX_NZB_FILES, MAX_NZB_SEGMENTS
from comet.usenet.stremio_nntp_config import (
    validate_handoff_config,
    validate_serialized_servers,
)

_MAX_HANDOFF_SELECTOR_BYTES = 512
_VIDEO_EXTENSION_PATTERN = r"(?:mkv|mp4|m4v|mov|webm|avi|ts|m2ts|mpg|mpeg|wmv|flv)"
_VIDEO_SUBJECT = re.compile(
    rf"\.{_VIDEO_EXTENSION_PATTERN}(?:[^A-Z0-9]|$)",
    re.IGNORECASE,
)
_ARCHIVE_SUBJECT = re.compile(
    r"""(?ix)
    (?P<stem>[a-z0-9][^"'<>|/\x00-\x1f]{0,500}?)
    (?:
        \.part[0-9]{1,4}\.rar
        |\.rar
        |\.r[0-9]{2,3}
        |\.7z(?:\.[0-9]{3})?
        |\.zip
        |\.z[0-9]{2}
        |\.[0-9]{3}
    )
    (?=$|[\s"'])
    """
)


@dataclass(frozen=True, slots=True)
class StremioNntpHandoff:
    nzb_url: str
    servers: tuple[str, ...]
    file_idx: int | None = None
    file_must_include: str | None = None

    def __post_init__(self):
        if not _valid_nzb_url(self.nzb_url):
            raise ValueError("Stremio NNTP requires an artifact URL")
        object.__setattr__(
            self,
            "servers",
            validate_serialized_servers(self.servers),
        )
        if (self.file_idx is None) == (self.file_must_include is None):
            raise ValueError("provide exactly one Stremio NNTP file selector")
        if self.file_idx is not None and (
            isinstance(self.file_idx, bool)
            or not isinstance(self.file_idx, int)
            or not 0 <= self.file_idx < MAX_NZB_FILES
        ):
            raise ValueError("fileIdx is invalid")
        if self.file_must_include is not None:
            _selector_regex(self.file_must_include)

    def render(self) -> dict:
        stream = {"nzbUrl": self.nzb_url, "servers": list(self.servers)}
        if self.file_idx is not None:
            stream["fileIdx"] = self.file_idx
        else:
            stream["fileMustInclude"] = self.file_must_include
        return stream


def _valid_nzb_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("ascii")
        parsed = urlsplit(value)
        _ = parsed.port
    except (UnicodeEncodeError, ValueError):
        return False
    return (
        1 <= len(encoded) <= 4_096
        and parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and bool(parsed.path)
        and not parsed.fragment
        and "%" not in (parsed.hostname or "")
        and "\\" not in value
        and not any(
            ord(character) <= 32 or ord(character) == 127 for character in value
        )
    )


class StremioNntpProvider:
    descriptor = ProviderDescriptor(
        kind="stremio_nntp",
        label="Stremio NNTP",
        accepted_locator_kinds=frozenset({"nzb_artifact"}),
        byte_paths=frozenset({BytePath.CLIENT_DELEGATED}),
        mutates_upstream=False,
    )

    async def validate_config(self, config: dict) -> ProviderStatus:
        try:
            validate_handoff_config(config)
        except ValueError:
            return ProviderStatus(
                Readiness.TERMINAL_FAILURE,
                Actionability.NONE,
                code="nntp_servers_required",
                auth_failed=True,
            )
        return ProviderStatus(Readiness.READY, Actionability.CLIENT_ON_DEMAND)

    def render_client_delegated(
        self, config: dict, nzb_url: str, manifest: list
    ) -> dict:
        """Render one inspected artifact only when Stremio's selector is exact."""
        servers = validate_handoff_config(config)
        return StremioNntpHandoff(
            nzb_url,
            servers,
            file_idx=_direct_file_index(manifest),
        ).render()

    def render_resolved(
        self,
        config: dict,
        nzb_url: str,
        manifest: list,
        file_must_include: str | None,
    ) -> dict:
        """Prefer an exact direct file, otherwise preserve an archive selector."""
        if file_must_include is None:
            return self.render_client_delegated(config, nzb_url, manifest)
        matches = _direct_file_indices(manifest, file_must_include)
        if len(matches) == 1:
            return StremioNntpHandoff(
                nzb_url,
                validate_handoff_config(config),
                file_idx=matches[0],
            ).render()
        validate_handoff_manifest(manifest, file_must_include)
        return self.render_unresolved(config, nzb_url, file_must_include)

    def render_unresolved(
        self,
        config: dict,
        nzb_url: str,
        file_must_include: str,
    ) -> dict:
        """Render one lazy transform with its already-bound deterministic selector."""
        return StremioNntpHandoff(
            nzb_url,
            validate_handoff_config(config),
            file_must_include=file_must_include,
        ).render()


def handoff_selector(
    title: str,
    selection_intent: tuple[object, ...],
) -> str | None:
    """Project a signed media target into one bounded Stremio selector."""
    if selection_intent[:1] == (1,) and len(selection_intent) == 3:
        season, episode = selection_intent[1:]
        if (
            not isinstance(season, bool)
            and isinstance(season, int)
            and 0 <= season <= 65_535
            and not isinstance(episode, bool)
            and isinstance(episode, int)
            and 0 <= episode <= 65_535
        ):
            marker = (
                rf"(?:S{season:02d}E{episode:02d}|"
                rf"{season}x{episode:02d})"
            )
            return _wire_selector(
                rf"(?<![A-Z0-9]){marker}(?![A-Z0-9])"
                rf".*?\.{_VIDEO_EXTENSION_PATTERN}(?:[^A-Z0-9]|$)"
            )
        return None
    if selection_intent != (0,) or not isinstance(title, str):
        return None
    stem = " ".join(title.strip().split())
    if stem.lower().endswith(".nzb"):
        stem = stem[:-4].rstrip()
    suffix = re.search(
        rf"(?i)\.{_VIDEO_EXTENSION_PATTERN}$",
        stem,
    )
    if suffix is not None:
        stem = stem[: suffix.start()]
    if not stem:
        return None
    literal = re.escape(stem).replace("/", r"\/")
    return _wire_selector(rf"{literal}.*?\.{_VIDEO_EXTENSION_PATTERN}(?:[^A-Z0-9]|$)")


def _wire_selector(pattern: str) -> str | None:
    selector = f"/{pattern}/i"
    try:
        encoded = selector.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_HANDOFF_SELECTOR_BYTES:
        return None
    return selector


def _selector_regex(selector: str) -> re.Pattern[str]:
    try:
        encoded = selector.encode("utf-8") if isinstance(selector, str) else b""
    except UnicodeEncodeError:
        encoded = b""
    if (
        not isinstance(selector, str)
        or not 4 <= len(encoded) <= _MAX_HANDOFF_SELECTOR_BYTES
        or not selector.startswith("/")
        or not selector.endswith("/i")
        or any(ord(character) < 32 or ord(character) == 127 for character in selector)
    ):
        raise ValueError("Stremio NNTP handoff selection is invalid")
    try:
        return re.compile(selector[1:-2].replace(r"\/", "/"), re.IGNORECASE)
    except re.error as exc:
        raise ValueError("Stremio NNTP handoff selection is invalid") from exc


def _direct_file_index(manifest: object) -> int:
    matches = _direct_file_indices(manifest)
    if len(matches) != 1:
        raise ValueError("Stremio NNTP requires an unambiguous selected file")
    return matches[0]


def _direct_file_indices(
    manifest: object,
    selector: str | None = None,
) -> list[int]:
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= MAX_NZB_FILES:
        raise ValueError("Stremio NNTP requires an unambiguous selected file")
    pattern = _selector_regex(selector) if selector is not None else None
    matches = []
    for index, item in enumerate(manifest):
        subject = item.get("subject") if isinstance(item, dict) else None
        if not _valid_manifest_subject(subject):
            continue
        if _VIDEO_SUBJECT.search(subject) and (
            pattern is None or pattern.search(subject)
        ):
            matches.append(index)
            if len(matches) == 2:
                return matches
    return matches


def _valid_manifest_subject(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return 1 <= len(encoded) <= 4_096


def _archive_stems(manifest: list) -> set[str]:
    stems: set[str] = set()
    for item in manifest:
        subject = item.get("subject") if isinstance(item, dict) else None
        if not _valid_manifest_subject(subject):
            continue
        archive = _ARCHIVE_SUBJECT.search(subject)
        if archive is not None:
            stems.add(
                re.sub(r"[\W_]+", ".", archive.group("stem").casefold()).strip(".")
            )
            if len(stems) == 2:
                return stems
    return stems


def _declared_file_size(item: object) -> int | None:
    postings = item.get("postings") if isinstance(item, dict) else None
    if not isinstance(postings, list) or not postings:
        return None
    previous = 0
    declared = 0
    for posting in postings:
        if not isinstance(posting, dict):
            return None
        number = posting.get("number")
        byte_size = posting.get("bytes")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not 1 <= number <= MAX_NZB_SEGMENTS
            or number < previous
            or isinstance(byte_size, bool)
            or not isinstance(byte_size, int)
            or not 1 <= byte_size <= 16 * 1024 * 1024
        ):
            return None
        if number != previous:
            if number != previous + 1:
                return None
            declared += byte_size
            if declared > MAX_SIGNED_BIGINT:
                return None
            previous = number
    return declared


def _matches_single_file_hint(
    manifest: list,
    selector: str,
    selection_hint: tuple[str, int] | None,
) -> bool:
    if selection_hint is None or len(manifest) != 1:
        return False
    name, byte_size = selection_hint
    try:
        encoded_name = name.encode("utf-8") if isinstance(name, str) else b""
    except UnicodeEncodeError:
        return False
    if (
        not 1 <= len(encoded_name) <= 512
        or isinstance(byte_size, bool)
        or not isinstance(byte_size, int)
        or not 1 <= byte_size <= MAX_SIGNED_BIGINT
        or not _VIDEO_SUBJECT.search(name)
    ):
        return False
    if (
        _direct_file_indices(manifest)
        or _archive_stems(manifest)
        or _declared_file_size(manifest[0]) != byte_size
    ):
        return False
    return _selector_regex(selector).search(name) is not None


def validate_handoff_manifest(
    manifest: object,
    selector: str,
    selection_hint: tuple[str, int] | None = None,
) -> None:
    """Prove one direct file, or conservatively recognize one archive set."""
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= MAX_NZB_FILES:
        raise ValueError("Stremio NNTP handoff selection is invalid")
    matches = _direct_file_indices(manifest, selector)
    if len(matches) == 1:
        return
    if not matches and len(_archive_stems(manifest)) == 1:
        return
    if not matches and _matches_single_file_hint(
        manifest,
        selector,
        selection_hint,
    ):
        return
    raise ValueError("Stremio NNTP handoff selection is ambiguous")
