"""Transport-neutral discovery and playback domain values."""

from dataclasses import dataclass, field
from enum import StrEnum

MAX_SIGNED_BIGINT = 2**63 - 1
MAX_REMOTE_GUID_LENGTH = 1_024


class TransportKind(StrEnum):
    BITTORRENT = "bittorrent"
    USENET = "usenet"


class ReleaseScope(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"
    SEASON_PACK = "season_pack"
    SERIES_PACK = "series_pack"
    DAILY_EPISODE = "daily_episode"
    ANIME_EPISODE = "anime_episode"


class LocatorKind(StrEnum):
    TORRENT = "torrent"
    REAL_NZB = "real_nzb"
    NZB_ARTIFACT = "nzb_artifact"
    EASYNEWS_HTTP = "easynews_http"


TORRENT_PROVIDER_KINDS = frozenset(
    {
        "alldebrid",
        "debrider",
        "debridlink",
        "direct_torrent",
        "easydebrid",
        "offcloud",
        "pikpak",
        "premiumize",
        "realdebrid",
        "torbox",
    }
)
REAL_NZB_PROVIDER_KINDS = frozenset(
    {
        "altmount",
        "comet_native_usenet",
        "nzbdav",
        "stremio_nntp",
        "stremthru_newz",
        "torbox_usenet",
    }
)
SERVER_USENET_PROVIDER_KINDS = (REAL_NZB_PROVIDER_KINDS - {"stremio_nntp"}) | {
    "easynews"
}
USENET_PLAYBACK_PROVIDER_KINDS = SERVER_USENET_PROVIDER_KINDS | {"stremio_nntp"}
CAPABILITY_VALIDATED_PLAYBACK_PROVIDER_KINDS = USENET_PLAYBACK_PROVIDER_KINDS | (
    TORRENT_PROVIDER_KINDS - {"direct_torrent"}
)
LOCATOR_PROVIDER_KINDS = {
    LocatorKind.TORRENT: TORRENT_PROVIDER_KINDS,
    LocatorKind.REAL_NZB: REAL_NZB_PROVIDER_KINDS,
    LocatorKind.NZB_ARTIFACT: REAL_NZB_PROVIDER_KINDS,
    LocatorKind.EASYNEWS_HTTP: frozenset({"easynews"}) | REAL_NZB_PROVIDER_KINDS,
}


@dataclass(frozen=True, slots=True)
class LocatorPolicy:
    allowed_provider_kinds: frozenset[str]
    owner_configuration_partition: bytes | None = None
    exact_provider_configuration_id: str | None = None
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class Locator:
    locator_id: str
    kind: LocatorKind
    policy: LocatorPolicy


@dataclass(frozen=True, slots=True)
class TorrentLocator(Locator):
    info_hash: str
    file_index: int | None = None
    season_norm: int = -1
    episode_norm: int = -1
    selection_title: str | None = None
    selection_size: int | None = None
    selection_parsed_json: str | None = None


@dataclass(frozen=True, slots=True)
class RealNzbRef(Locator):
    adapter_configuration_id: str
    remote_guid: str


@dataclass(frozen=True, slots=True)
class NzbArtifactRef(Locator):
    artifact_sha256: str
    manifest_identity: str
    selection_hint_name: str | None = None
    selection_hint_size: int | None = None


@dataclass(frozen=True, slots=True)
class EasynewsHttpRef(Locator):
    account_configuration_id: str
    file_identifier: str
    download_farm: str
    download_port: str
    content_hash: str
    item_identifier: str
    filename: str
    extension: str
    signature: str | None = None
    byte_size: int | None = None


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    candidate_id: str
    media_id: str
    scope: ReleaseScope
    transport: TransportKind
    title: str
    locators: tuple[Locator, ...]
    size: int | None = None
    published_at_ms: int | None = None
    source: str = ""
    parsed: object | None = None
    transport_stats: dict[str, object] = field(default_factory=dict)
    identities: tuple[str, ...] = ()

    def __post_init__(self):
        if self.transport is TransportKind.BITTORRENT:
            return
        if not isinstance(self.scope, ReleaseScope):
            raise ValueError("candidate scope is invalid")
        if not isinstance(self.transport, TransportKind):
            raise ValueError("candidate transport is invalid")
        if not self.locators:
            raise ValueError("a release candidate requires at least one locator")
        if self.size is not None and (
            type(self.size) is not int or not 1 <= self.size <= MAX_SIGNED_BIGINT
        ):
            raise ValueError("candidate size must be a positive signed bigint")
        if self.published_at_ms is not None and (
            type(self.published_at_ms) is not int
            or not 0 <= self.published_at_ms <= MAX_SIGNED_BIGINT
        ):
            raise ValueError(
                "candidate published time must be a non-negative signed bigint"
            )
