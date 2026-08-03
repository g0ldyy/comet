import asyncio
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from itertools import chain
from typing import Any

from comet.core.capabilities import (
    CapabilityPlan,
    CapabilityPlanner,
    CapabilityStateSnapshot,
)
from comet.core.capability_bindings import (
    ensure_playback_capability_states,
    native_instance_credential_material,
)
from comet.core.execution import get_executor
from comet.core.models import database, settings
from comet.core.scrape import ScrapeContext
from comet.core.sources import (
    TORRENT_PROVIDER_KINDS,
    ReleaseCandidate,
    ReleaseScope,
)
from comet.debrid.exceptions import DebridAuthError, DebridLinkGenerationError
from comet.discovery import SearchCoordinator, build_discovery_adapters
from comet.discovery.capabilities import (
    build_discovery_branch_fingerprints,
    ensure_discovery_capability_states,
    record_discovery_capability_failure,
)
from comet.discovery.manager import DiscoveryResult
from comet.discovery.models import MAX_TITLE_ALIASES, MediaQuery
from comet.discovery.torrent_repository import torrent_candidate_from_runtime
from comet.metadata.episode_index import EpisodeIndexService
from comet.metadata.filter import release_filter
from comet.metadata.manager import (
    MetadataFetchStatus,
    MetadataScraper,
)
from comet.observability import current_request_id, log, metrics
from comet.playback.presentation import (
    ProviderOption,
    build_provider_options,
    issue_provider_option_capability,
    select_presentation,
)
from comet.playback.registry import build_playback_providers
from comet.playback.repository import RenderedCandidateIds, RenderedReleaseRepository
from comet.playback.tokens import CapabilityCodec
from comet.services.anime import anime_mapper
from comet.services.cache_state import CacheStateManager, mark_scope_scraped
from comet.services.debrid import DebridService
from comet.services.debrid_account_scraper import (
    ensure_account_snapshot_ready,
    get_account_torrents_for_media,
    schedule_account_snapshot_refresh,
)
from comet.services.filtering import filter_release_candidates
from comet.services.orchestration import TorrentResultAccumulator
from comet.services.ranking import sort_candidates
from comet.usenet.access import NativeAccessAuthorizer
from comet.utils.http_client import http_client_manager
from comet.utils.parsing import MediaScope, parse_media_id, resolve_media_scope

BackgroundTaskAdder = Callable[..., Any]
_MAX_DISCOVERY_DIAGNOSTICS = 12
_MAX_DISCOVERY_DIAGNOSTIC_LENGTH = 256


class MediaSearchStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    DISABLED = "disabled"
    UNRELEASED = "unreleased"
    METADATA_UNAVAILABLE = "metadata_unavailable"
    METADATA_NOT_FOUND = "metadata_not_found"
    METADATA_UNSUPPORTED = "metadata_unsupported"
    BUSY = "busy"


@dataclass(slots=True)
class MediaSearchResult:
    status: MediaSearchStatus
    metadata: dict = field(default_factory=dict)
    aliases: dict = field(default_factory=dict)
    media_scope: MediaScope | None = None
    torrents: dict = field(default_factory=dict)
    ranked_info_hashes: list[str] = field(default_factory=list)
    service_cache_status: dict = field(default_factory=dict)
    debrid_errors: dict = field(default_factory=dict)
    cache_state: str = "unknown"
    media_only_id: str = ""
    search_season: int | None = None
    search_episode: int | None = None
    is_torrent_only: bool = False
    show_account_sync_trigger: bool = False
    use_account_scrape: bool = False
    candidates: tuple = ()
    discovery_diagnostics: tuple[str, ...] = ()
    provider_options: tuple[ProviderOption, ...] = ()
    rendered_candidate_ids: dict[str, RenderedCandidateIds] = field(
        default_factory=dict
    )
    provider_capabilities: dict[tuple[str, str], str] = field(default_factory=dict)
    candidate_count: int = 0


@dataclass(slots=True)
class _DiscoveryTaskOwner:
    task: asyncio.Task[DiscoveryResult] | None = None

    def start(self, coroutine) -> asyncio.Task[DiscoveryResult]:
        self.task = asyncio.create_task(coroutine)
        return self.task

    async def close(self) -> None:
        if self.task is None:
            return
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class SearchCapacityTracker:
    """Report persistent search admission pressure without per-request noise."""

    def __init__(self, *, clock=time.monotonic, reminder_seconds: float = 900.0):
        self._clock = clock
        self._reminder_seconds = reminder_seconds
        self._lock = threading.Lock()
        self._busy = False
        self._changed_at = clock()
        self._last_emitted_at = self._changed_at
        self._suppressed_count = 0

    def observe(self, busy: bool) -> None:
        now = self._clock()
        event = None
        fields = {}
        with self._lock:
            if not busy:
                if self._busy:
                    event = "recovered"
                    fields = {
                        "duration_ms": (now - self._changed_at) * 1000,
                        "suppressed_count": self._suppressed_count,
                    }
                    self._busy = False
                    self._changed_at = now
                    self._last_emitted_at = now
                    self._suppressed_count = 0
            elif not self._busy:
                event = "degraded"
                fields = {"suppressed_count": 0}
                self._busy = True
                self._changed_at = now
                self._last_emitted_at = now
                self._suppressed_count = 0
            else:
                self._suppressed_count += 1
                if now - self._last_emitted_at >= self._reminder_seconds:
                    event = "degraded"
                    fields = {"suppressed_count": self._suppressed_count}
                    self._last_emitted_at = now
                    self._suppressed_count = 0
        if event == "degraded":
            log.warning(
                "search.capacity.degraded",
                "Search capacity is degraded",
                error_code="search_capacity",
                suppressed_count=fields["suppressed_count"],
            )
        elif event == "recovered":
            log.info(
                "search.capacity.recovered",
                "Search capacity recovered",
                duration_ms=fields["duration_ms"],
                suppressed_count=fields["suppressed_count"],
            )


_search_capacity_tracker = SearchCapacityTracker()


def _public_discovery_diagnostics(
    plan_diagnostics: tuple[str, ...],
    runtime_diagnostics: tuple[str, ...],
    *,
    has_candidates: bool,
) -> tuple[str, ...]:
    """Retain configured failures and at most one safe all-source outage."""

    def normalize(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not 1 <= len(value) <= _MAX_DISCOVERY_DIAGNOSTIC_LENGTH or any(
            ord(character) < 32 for character in value
        ):
            return None
        return value

    diagnostics = []
    for value in plan_diagnostics:
        normalized = normalize(value)
        if normalized is not None and normalized not in diagnostics:
            diagnostics.append(normalized)
        if len(diagnostics) == _MAX_DISCOVERY_DIAGNOSTICS:
            return tuple(diagnostics)
    if has_candidates:
        return tuple(diagnostics)
    for value in runtime_diagnostics:
        normalized = normalize(value)
        if normalized is not None and normalized not in diagnostics:
            diagnostics.append(normalized)
            break
    return tuple(diagnostics)


def _bittorrent_enabled(config: Mapping[str, Any]) -> bool:
    """Keep legacy configurations torrent-capable while honoring v2 transport intent."""
    if config["schemaVersion"] != 2:
        return True
    return "bittorrent" in config["enabledTransports"]


def _discovery_title_aliases(
    title: str,
    aliases: Mapping[str, list[str]],
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(chain((title,), chain.from_iterable(aliases.values()))))[
        :MAX_TITLE_ALIASES
    ]


async def _search_configured_sources(
    config: Mapping[str, Any],
    session,
    *,
    user_session=None,
    media_id: str,
    media_type: str,
    season: int | None,
    episode: int | None,
    title_aliases: tuple[str, ...] = (),
    year: int | None = None,
    air_date: str | None = None,
    absolute_episode: int | None = None,
    search_scope: str | None = None,
    add_background_task: BackgroundTaskAdder | None = None,
) -> DiscoveryResult:
    """Run only adapters reachable from this request's capability plan."""
    user_session = session if user_session is None else user_session
    capability_states = None
    codec = None
    if config["schemaVersion"] == 2 and settings.COMET_CAPABILITY_SECRET:
        codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
        providers = build_playback_providers(
            config,
            session,
            user_session=user_session,
            database=database,
        )
        provider_states = await ensure_playback_capability_states(
            config,
            codec,
            database,
            providers,
            instance_credential_material={
                "comet_native_usenet": native_instance_credential_material(
                    settings.USENET_NATIVE_ACCESS_TOKEN,
                    settings.USENET_NATIVE_SERVERS,
                )
            },
        )
        discovery_states = await ensure_discovery_capability_states(
            config,
            codec,
            database,
            session,
            user_session=user_session,
        )
        capability_states = CapabilityStateSnapshot(provider_states, discovery_states)
    account_partition = (
        codec.configuration_partition_for_config(dict(config))
        if codec is not None
        else None
    )

    async def record_runtime_discovery_failure(
        source_configuration_id: str,
        state: str,
        error_code: str,
        retry_after: int | None,
    ) -> None:
        if codec is None:
            return
        await record_discovery_capability_failure(
            config,
            codec,
            database,
            source_configuration_id,
            state=state,
            error_code=error_code,
            retry_after=retry_after,
        )

    adapters = build_discovery_adapters(
        config,
        session,
        user_session=user_session,
        database=database if account_partition is not None else None,
        account_partition=account_partition,
        runtime_failure_recorder=(
            record_runtime_discovery_failure if account_partition is not None else None
        ),
    )
    planner = CapabilityPlanner(
        usenet_offered=settings.USENET_ENABLED,
        native_authorizer=NativeAccessAuthorizer(settings.USENET_NATIVE_ACCESS_TOKEN),
        native_engine_enabled=settings.USENET_ENGINE_ENABLED,
        native_instance_pool_available=bool(settings.USENET_NATIVE_SERVERS),
        native_user_servers_allowed=settings.USENET_NATIVE_ALLOW_USER_SERVERS,
    )
    plan = planner.build(dict(config), capability_states)
    branch_fingerprints = (
        {
            (
                branch.source_configuration_id,
                branch.branch_family,
            ): branch
            for branch in build_discovery_branch_fingerprints(
                config,
                codec,
                account_partition=account_partition,
            )
        }
        if codec is not None
        else None
    )
    result = await SearchCoordinator(
        adapters,
        database=database if account_partition is not None else None,
        background_task_adder=add_background_task,
    ).search(
        MediaQuery(
            media_id,
            media_type,
            season,
            episode,
            title_aliases=title_aliases,
            year=year,
            air_date=air_date,
            absolute_episode=absolute_episode,
            search_scope=search_scope,
        ),
        plan,
        account_partition=account_partition,
        trace_id=current_request_id(),
        branch_fingerprints=branch_fingerprints,
    )
    diagnostics = _public_discovery_diagnostics(
        plan.diagnostics,
        result.diagnostics,
        has_candidates=bool(result.candidates),
    )
    if diagnostics == result.diagnostics:
        return result
    return replace(result, diagnostics=diagnostics)


async def _filter_and_rank_discovery_candidates(
    candidates: tuple,
    *,
    title: str,
    year: int,
    year_end: int | None,
    media_type: str,
    aliases: dict,
    remove_adult_content: bool,
    config: Mapping[str, Any],
    content_id: str | None = None,
) -> tuple:
    """Run non-torrent releases through the shared RTN filter and rank rules."""
    if not candidates:
        return ()
    loop = asyncio.get_running_loop()
    filtered = await loop.run_in_executor(
        get_executor(),
        filter_release_candidates,
        candidates,
        title,
        year,
        year_end,
        media_type,
        aliases,
        remove_adult_content,
        content_id,
    )
    ranked = await loop.run_in_executor(
        get_executor(),
        sort_candidates,
        filtered,
        config["rtnSettings"],
        config["rtnRanking"],
        0,
        config["maxSize"],
        config["removeTrash"],
    )
    return ranked


async def _persist_rendered_candidates(
    config: Mapping[str, Any], candidates: tuple
) -> dict[str, RenderedCandidateIds]:
    if not candidates or not settings.COMET_CAPABILITY_SECRET:
        return {}
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    return await RenderedReleaseRepository(database).persist(
        candidates,
        owner_configuration_partition=codec.configuration_partition_for_config(
            dict(config)
        ),
    )


def _apply_torrent_cache_facts(
    options: tuple[ProviderOption, ...],
    service_cache_status: Mapping[str, Mapping[str, bool]],
) -> tuple[ProviderOption, ...]:
    """Attach current exact debrid cache facts without inferring readiness."""
    resolved = []
    for option in options:
        if (
            option.provider.kind not in TORRENT_PROVIDER_KINDS
            or option.provider.kind == "direct_torrent"
        ):
            resolved.append(option)
            continue
        info_hash = option.candidate_id.removeprefix("btih:")
        cached = service_cache_status.get(info_hash, {}).get(
            option.provider.configuration_id,
            False,
        )
        resolved.append(replace(option, cached=True) if cached else option)
    return tuple(resolved)


def _issue_provider_capabilities(
    config: Mapping[str, Any],
    options: tuple[ProviderOption, ...],
    persisted_candidates: Mapping[str, RenderedCandidateIds],
    *,
    media_type: str,
    season: int | None,
    episode: int | None,
) -> dict[tuple[str, str], str]:
    if not settings.COMET_CAPABILITY_SECRET:
        return {}
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    partition = codec.configuration_partition_for_config(dict(config))
    selection_intent = [0] if media_type == "movie" else [1, season or 0, episode or 0]
    capabilities = {}
    for option in options:
        if option.provider.kind in {"direct_torrent", "stremio_nntp"}:
            continue
        persisted = persisted_candidates.get(option.candidate_id)
        if persisted is None:
            continue
        capabilities[(option.candidate_id, option.provider.configuration_id)] = (
            issue_provider_option_capability(
                codec,
                partition=partition,
                option=option,
                persisted=persisted,
                selection_intent=selection_intent,
                client="stremio",
            )
        )
    return capabilities


async def _prepare_provider_view(
    config: Mapping[str, Any],
    candidates: tuple[ReleaseCandidate, ...],
    capability_plan: CapabilityPlan,
    service_cache_status: Mapping[str, Mapping[str, bool]],
    *,
    media_type: str,
    season: int | None,
    episode: int | None,
    season_norm: int,
    episode_norm: int,
) -> tuple[
    tuple[ReleaseCandidate, ...],
    tuple[ProviderOption, ...],
    dict[str, RenderedCandidateIds],
    dict[tuple[str, str], str],
]:
    """Persist and expose one provider-expanded mixed candidate view."""
    provider_options = build_provider_options(candidates, capability_plan)
    provider_options = _apply_torrent_cache_facts(
        provider_options,
        service_cache_status,
    )
    candidates, provider_options = select_presentation(
        candidates,
        provider_options,
        cached_only=config["cachedOnly"],
        max_releases_per_resolution=config["maxResultsPerResolution"],
        season_norm=season_norm,
        episode_norm=episode_norm,
    )
    rendered_candidate_ids = await _persist_rendered_candidates(config, candidates)
    provider_capabilities = _issue_provider_capabilities(
        config,
        provider_options,
        rendered_candidate_ids,
        media_type=media_type,
        season=season,
        episode=episode,
    )
    return (
        candidates,
        provider_options,
        rendered_candidate_ids,
        provider_capabilities,
    )


async def _prepare_discovery_only_view(
    config: Mapping[str, Any],
    discovery_result: DiscoveryResult,
    *,
    title: str,
    content_id: str,
    year: int,
    year_end: int | None,
    media_type: str,
    aliases: dict,
    remove_adult_content: bool,
    season: int | None,
    episode: int | None,
    season_norm: int,
    episode_norm: int,
):
    """Build the independent Usenet view when the torrent branch cannot proceed."""
    candidates = await _filter_and_rank_discovery_candidates(
        discovery_result.candidates,
        title=title,
        year=year,
        year_end=year_end,
        media_type=media_type,
        aliases=aliases,
        remove_adult_content=remove_adult_content,
        config=config,
        content_id=content_id,
    )
    return await _prepare_provider_view(
        config,
        candidates,
        discovery_result.capability_plan,
        {},
        media_type=media_type,
        season=season,
        episode=episode,
        season_norm=season_norm,
        episode_norm=episode_norm,
    )


def episode_matching_policy(
    media_type: str,
    media_only_id: str,
    search_season: int | None,
    search_episode: int | None,
    *,
    cached_only: bool,
    has_debrid: bool,
    enable_torrent: bool,
) -> tuple[bool, bool]:
    is_imdb_episode_request = (
        media_type == "series"
        and search_season is not None
        and search_episode is not None
        and media_only_id.startswith("tt")
    )
    allow_debrid_verified_season_packs = (
        is_imdb_episode_request and cached_only and has_debrid and not enable_torrent
    )
    reject_unknown_episode_files = (
        is_imdb_episode_request and not allow_debrid_verified_season_packs
    )
    return is_imdb_episode_request, reject_unknown_episode_files


def merge_service_cache_status(target: dict, incoming: dict):
    for info_hash, service_map in incoming.items():
        cache_map = target.setdefault(info_hash, {})
        for service, is_cached in service_map.items():
            if is_cached:
                cache_map[service] = True
            elif service not in cache_map:
                cache_map[service] = False


async def _mark_scope_scraped_if_populated(media_id: str, torrents: dict) -> None:
    if torrents:
        await mark_scope_scraped(media_id)


def group_debrid_entries_by_service(
    debrid_entries: list,
) -> list[tuple[str, str, list]]:
    """Keep legacy failover groups while isolating every stable v2 binding."""
    service_entries = {}
    seen_credentials = set()
    for entry in debrid_entries:
        service = entry["service"]
        credential = (service, entry["apiKey"])
        configuration_id = entry.get("configurationId")
        if not isinstance(configuration_id, str) and credential in seen_credentials:
            continue
        seen_credentials.add(credential)
        key = configuration_id if isinstance(configuration_id, str) else service
        service_entries.setdefault((key, service), []).append(entry)
    return [
        (key, service, entries) for (key, service), entries in service_entries.items()
    ]


def _log_debrid_check(
    event: str,
    message: str,
    *,
    content_id: str | None,
    provider_key: str,
    service: str,
    requested_count: int,
    candidate_count: int,
    cache_state: str | None = None,
    error_code: str | None = None,
    exc: BaseException | None = None,
) -> None:
    fields = {
        "debrid_service": service,
        "requested_count": requested_count,
        "candidate_count": candidate_count,
    }
    if content_id:
        fields["content_id"] = content_id
    if provider_key != service:
        fields["provider_name"] = provider_key
    if cache_state is not None:
        fields["cache_state"] = cache_state
    if error_code is not None:
        log.warning(
            event,
            message,
            error_code=error_code,
            exc=exc,
            **fields,
        )
    else:
        log.info(event, message, **fields)


def select_debrid_refresh_hashes(
    current_hashes: set[str],
    initial_hashes: set[str],
    verified_cache_status: dict,
    *,
    had_cached_torrents: bool,
    use_account_scrape: bool,
) -> set[str]:
    if not current_hashes:
        return set()

    verified_count = sum(
        any(service_map.values())
        for info_hash, service_map in verified_cache_status.items()
        if info_hash in current_hashes
    )
    requires_full_refresh = (
        (not had_cached_torrents and not use_account_scrape)
        or verified_count == 0
        or (verified_count / len(current_hashes)) < settings.DEBRID_CACHE_CHECK_RATIO
    )
    return current_hashes if requires_full_refresh else current_hashes - initial_hashes


async def background_scrape(
    torrent_manager: TorrentResultAccumulator,
    media_id: str,
    debrid_entries: list,
    ip: str,
    session,
):
    await torrent_manager.scrape_torrents(ScrapeContext.BACKGROUND)

    if debrid_entries and torrent_manager.torrents:
        await get_and_cache_multi_service_availability(
            session,
            debrid_entries,
            torrent_manager.torrents,
            torrent_manager.media_id,
            torrent_manager.media_only_id,
            torrent_manager.search_season,
            torrent_manager.search_episode,
            torrent_manager.media_scope,
            ip,
            target_air_date=torrent_manager.target_air_date,
        )

    await _mark_scope_scraped_if_populated(media_id, torrent_manager.torrents)


async def check_multi_service_availability(
    debrid_entries: list,
    torrents: dict,
    season: int | None,
    episode: int | None,
    media_scope: MediaScope,
    *,
    content_id: str | None = None,
):
    service_cache_status = defaultdict(dict)
    info_hashes = list(torrents)
    if not info_hashes or not debrid_entries:
        return service_cache_status

    async def check_service(entry):
        service = entry["service"]
        debrid_instance = DebridService(service, entry["apiKey"], "")
        (
            cached_hashes,
            torrent_updates,
        ) = await debrid_instance.check_existing_availability(
            info_hashes, season, episode, media_scope, torrents
        )
        return cached_hashes, torrent_updates

    service_groups = group_debrid_entries_by_service(debrid_entries)
    results = await asyncio.gather(
        *(check_service(entries[0]) for _key, _service, entries in service_groups)
    )

    enriched_hashes = set()
    for (provider_key, service, _entries), result in zip(
        service_groups, results, strict=True
    ):
        cached_hashes, torrent_updates = result
        _log_debrid_check(
            "debrid.cache.checked",
            "Debrid cache checked",
            content_id=content_id,
            provider_key=provider_key,
            service=service,
            requested_count=len(info_hashes),
            candidate_count=len(cached_hashes),
            cache_state="hit" if cached_hashes else "miss",
        )
        for info_hash, update in torrent_updates.items():
            if info_hash in enriched_hashes:
                continue
            torrent = torrents.get(info_hash)
            if torrent is not None:
                torrent.update(update)
                enriched_hashes.add(info_hash)
        for info_hash in cached_hashes:
            service_cache_status[info_hash][provider_key] = True

    return service_cache_status


async def get_and_cache_multi_service_availability(
    session,
    debrid_entries: list,
    torrents: dict,
    media_id: str,
    media_only_id: str,
    season: int | None,
    episode: int | None,
    media_scope: MediaScope,
    ip: str,
    target_air_date: str | None = None,
    known_cache_status: dict | None = None,
):
    service_cache_status = defaultdict(dict)
    errors = {}
    info_hashes = list(torrents)

    if not info_hashes or not debrid_entries:
        return service_cache_status, errors

    seeders_map = {
        info_hash: torrents[info_hash]["seeders"] for info_hash in info_hashes
    }
    tracker_map = {
        info_hash: torrents[info_hash]["tracker"] for info_hash in info_hashes
    }
    sources_map = {
        info_hash: torrents[info_hash]["sources"] for info_hash in info_hashes
    }
    service_groups = group_debrid_entries_by_service(debrid_entries)
    if known_cache_status is None:
        known_cache_status = {}

    async def check_service(provider_key, service, entries):
        service_info_hashes = [
            info_hash
            for info_hash in info_hashes
            if not known_cache_status.get(info_hash, {}).get(provider_key, False)
        ]
        if not service_info_hashes:
            return set(), {}, None

        auth_error = None
        for entry in entries:
            try:
                debrid_instance = DebridService(service, entry["apiKey"], ip)
                (
                    cached_hashes,
                    torrent_updates,
                ) = await debrid_instance.get_and_cache_availability(
                    session,
                    service_info_hashes,
                    seeders_map,
                    tracker_map,
                    sources_map,
                    torrents,
                    media_id,
                    media_only_id,
                    season,
                    episode,
                    media_scope,
                    target_air_date=target_air_date,
                )
                return cached_hashes, torrent_updates, None
            except DebridAuthError as error:
                if auth_error is None:
                    auth_error = error
            except DebridLinkGenerationError as error:
                return None, None, error

        return None, None, auth_error

    results = await asyncio.gather(
        *(
            check_service(provider_key, service, entries)
            for provider_key, service, entries in service_groups
        )
    )

    enriched_hashes = set()
    for (provider_key, service, _entries), result in zip(
        service_groups, results, strict=True
    ):
        cache_map, torrent_updates, error = result
        if error:
            _log_debrid_check(
                "debrid.availability.checked",
                "Debrid availability checked",
                content_id=media_id,
                provider_key=provider_key,
                service=service,
                requested_count=len(info_hashes),
                candidate_count=0,
                error_code=(
                    "credentials_rejected"
                    if isinstance(error, DebridAuthError)
                    else "dependency_failure"
                ),
                exc=error,
            )
            if isinstance(error, DebridAuthError):
                errors[provider_key] = error
            continue
        _log_debrid_check(
            "debrid.availability.checked",
            "Debrid availability checked",
            content_id=media_id,
            provider_key=provider_key,
            service=service,
            requested_count=len(info_hashes),
            candidate_count=len(cache_map),
        )

        for info_hash, update in torrent_updates.items():
            if info_hash in enriched_hashes:
                continue
            torrent = torrents.get(info_hash)
            if torrent is not None:
                torrent.update(update)
                enriched_hashes.add(info_hash)

        if cache_map:
            for info_hash in cache_map:
                service_cache_status[info_hash][provider_key] = True

    return service_cache_status, errors


async def search_media(
    media_type: str,
    media_id: str,
    config: Mapping[str, Any],
    ip: str,
    add_background_task: BackgroundTaskAdder,
    *,
    client_type: str = "stremio",
) -> MediaSearchResult:
    started_at = time.monotonic_ns()
    discovery_owner = _DiscoveryTaskOwner()
    log.info(
        "search.accepted",
        "Media search accepted",
        content_id=media_id,
        media_type=media_type,
        **({"client_type": client_type} if client_type != "stremio" else {}),
    )
    try:
        result = await _search_media(
            media_type,
            media_id,
            config,
            ip,
            add_background_task,
            discovery_owner=discovery_owner,
        )
        busy = result.status == MediaSearchStatus.BUSY
        if busy:
            metrics.observe_search_rejection("busy")
        _search_capacity_tracker.observe(busy)
        if result.status == MediaSearchStatus.OK:
            outcome = "ok"
        elif result.status == MediaSearchStatus.METADATA_UNAVAILABLE:
            outcome = "failed"
        elif (
            result.status == MediaSearchStatus.METADATA_UNSUPPORTED
            or result.status in {MediaSearchStatus.INVALID, MediaSearchStatus.BUSY}
        ):
            outcome = "rejected"
        else:
            outcome = "skipped"
        duration_ms = (time.monotonic_ns() - started_at) / 1_000_000
        completion_fields = {
            "content_id": media_id,
            "candidate_count": result.candidate_count,
            "duration_ms": duration_ms,
        }
        if result.status == MediaSearchStatus.METADATA_UNAVAILABLE:
            log.terminal(
                "search.completed",
                "Media search completed",
                outcome=outcome,
                error_code="metadata_unavailable",
                **completion_fields,
            )
        else:
            log.terminal(
                "search.completed",
                "Media search completed",
                outcome=outcome,
                **completion_fields,
            )
        return result
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.terminal(
            "search.completed",
            "Media search completed",
            outcome="failed",
            content_id=media_id,
            candidate_count=0,
            duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            error_code="search_failure",
            exc=exc,
        )
        raise
    finally:
        await discovery_owner.close()


async def _search_media(
    media_type: str,
    media_id: str,
    config: Mapping[str, Any],
    ip: str,
    add_background_task: BackgroundTaskAdder,
    *,
    discovery_owner: _DiscoveryTaskOwner,
) -> MediaSearchResult:
    try:
        media_only_id, season, episode = parse_media_id(media_type, media_id)
    except ValueError:
        return MediaSearchResult(MediaSearchStatus.INVALID)
    media_scope = resolve_media_scope(media_type, season, episode)

    debrid_entries = config["_debridEntries"]
    bittorrent_enabled = _bittorrent_enabled(config)
    enable_torrent = bool(config["_enableTorrent"] and bittorrent_enabled)
    scrape_debrid_account_torrents = config["scrapeDebridAccountTorrents"]
    if not bittorrent_enabled:
        debrid_entries = []
    use_account_scrape = bool(debrid_entries and scrape_debrid_account_torrents)
    is_torrent_only = enable_torrent and not debrid_entries

    if settings.DISABLE_TORRENT_STREAMS and is_torrent_only:
        return MediaSearchResult(
            MediaSearchStatus.DISABLED,
            is_torrent_only=is_torrent_only,
            use_account_scrape=use_account_scrape,
        )

    session = await http_client_manager.get_session()
    user_session = await http_client_manager.get_user_session()
    metadata_scraper = MetadataScraper(session)

    if settings.DIGITAL_RELEASE_FILTER:
        is_released = await release_filter.check_is_released(
            session, media_type, media_id, season, episode
        )
        if not is_released:
            return MediaSearchResult(
                MediaSearchStatus.UNRELEASED,
                media_scope=media_scope,
                media_only_id=media_only_id,
                search_season=season,
                search_episode=episode,
                is_torrent_only=is_torrent_only,
                use_account_scrape=use_account_scrape,
            )

    metadata_result = await metadata_scraper.fetch_metadata_and_aliases(
        media_type, media_id, media_only_id, season, episode
    )
    metadata = metadata_result.metadata
    aliases = metadata_result.aliases
    metadata_status = metadata_result.status
    if metadata is None:
        status = {
            MetadataFetchStatus.NOT_FOUND: MediaSearchStatus.METADATA_NOT_FOUND,
            MetadataFetchStatus.UNSUPPORTED: MediaSearchStatus.METADATA_UNSUPPORTED,
            MetadataFetchStatus.PROVIDER_UNAVAILABLE: (
                MediaSearchStatus.METADATA_UNAVAILABLE
            ),
            MetadataFetchStatus.TIMEOUT: MediaSearchStatus.METADATA_UNAVAILABLE,
            MetadataFetchStatus.INVALID_RESPONSE: (
                MediaSearchStatus.METADATA_UNAVAILABLE
            ),
        }[metadata_status]
        return MediaSearchResult(
            status,
            media_scope=media_scope,
            media_only_id=media_only_id,
            search_season=season,
            search_episode=episode,
            is_torrent_only=is_torrent_only,
            use_account_scrape=use_account_scrape,
        )

    title = metadata["title"]
    year = metadata["year"]
    year_end = metadata["year_end"]
    season = metadata["season"]
    episode = metadata["episode"]

    is_kitsu = media_id.startswith("kitsu:")
    search_episode = episode
    search_season = season

    if is_kitsu:
        kitsu_mapping = anime_mapper.get_kitsu_episode_mapping(media_only_id)
        if kitsu_mapping:
            from_episode = kitsu_mapping.get("from_episode")
            from_season = kitsu_mapping.get("from_season")
            if from_season is not None and from_season != season:
                search_season = from_season
            if episode is not None and from_episode is not None:
                new_episode = from_episode + episode - 1
                if new_episode != episode:
                    search_episode = new_episode

    presentation_scope = (
        "anime_episode"
        if is_kitsu
        else {
            MediaScope.MOVIE: "movie",
            MediaScope.EPISODE: "episode",
            MediaScope.SEASON: "season_pack",
            MediaScope.SERIES: "series_pack",
        }[media_scope]
    )
    presentation_season = search_season if search_season is not None else -1
    presentation_episode = search_episode if search_episode is not None else -1

    # Discovery is transport-neutral and starts alongside the legacy torrent
    # path.  A capability plan makes this a no-op for legacy and unconfigured
    # profiles, so it cannot fan out to an ineligible service.
    discovery_task = discovery_owner.start(
        _search_configured_sources(
            config,
            session,
            user_session=user_session,
            media_id=media_only_id,
            media_type=media_type,
            season=search_season,
            episode=search_episode,
            title_aliases=_discovery_title_aliases(title, aliases),
            year=year,
            air_date=None,
            absolute_episode=search_episode if is_kitsu else None,
            search_scope=presentation_scope,
            add_background_task=add_background_task,
        )
    )
    remove_adult_content = settings.REMOVE_ADULT_CONTENT and config["removeTrash"]

    # Metadata remains useful to every transport, but the legacy torrent
    # manager owns cache/scraper/debrid side effects and must not run for a
    # Usenet-only profile.
    if not bittorrent_enabled:
        discovery_result = await discovery_task
        (
            candidates,
            provider_options,
            rendered_candidate_ids,
            provider_capabilities,
        ) = await _prepare_discovery_only_view(
            config,
            discovery_result,
            title=title,
            content_id=media_id,
            year=year,
            year_end=year_end,
            media_type=media_type,
            aliases=aliases,
            remove_adult_content=remove_adult_content,
            season=search_season,
            episode=search_episode,
            season_norm=presentation_season,
            episode_norm=presentation_episode,
        )
        return MediaSearchResult(
            MediaSearchStatus.OK,
            metadata=metadata,
            aliases=aliases,
            media_scope=media_scope,
            cache_state="unknown",
            media_only_id=media_only_id,
            search_season=search_season,
            search_episode=search_episode,
            is_torrent_only=False,
            use_account_scrape=False,
            candidates=candidates,
            discovery_diagnostics=discovery_result.diagnostics,
            provider_options=provider_options,
            rendered_candidate_ids=rendered_candidate_ids,
            provider_capabilities=provider_capabilities,
            candidate_count=len(candidates),
        )

    cache_media_ids = [media_only_id]
    if anime_mapper.is_loaded():
        if is_kitsu:
            imdb_id = await anime_mapper.get_imdb_from_kitsu(media_only_id)
            if imdb_id:
                cache_media_ids.append(imdb_id)
        elif anime_mapper.is_anime_content(media_id, media_only_id):
            kitsu_ids = anime_mapper.get_kitsu_ids_from_imdb(media_only_id)
            if kitsu_ids:
                cache_media_ids.extend(kitsu_ids)
            kitsu_id = await anime_mapper.get_kitsu_from_imdb(media_only_id)
            if kitsu_id and kitsu_id not in cache_media_ids:
                cache_media_ids.append(kitsu_id)

    is_imdb_episode_request, reject_unknown_episode_files = episode_matching_policy(
        media_type,
        media_only_id,
        search_season,
        search_episode,
        cached_only=bool(config["cachedOnly"]),
        has_debrid=bool(debrid_entries),
        enable_torrent=enable_torrent,
    )
    target_air_date = None
    if is_imdb_episode_request:
        target_air_date = await EpisodeIndexService(session).get_target_air_date(
            media_only_id,
            search_season,
            search_episode,
        )

    torrent_manager = TorrentResultAccumulator(
        media_type,
        media_id,
        media_only_id,
        title,
        year,
        year_end,
        season,
        episode,
        aliases,
        remove_adult_content,
        is_kitsu=is_kitsu,
        search_episode=search_episode,
        search_season=search_season,
        cache_media_ids=cache_media_ids,
        target_air_date=target_air_date,
        reject_unknown_episode_files=reject_unknown_episode_files,
        media_scope=media_scope,
    )

    await torrent_manager.get_cached_torrents()
    torrent_count = len(torrent_manager.torrents)
    cache_state = "hit" if torrent_count else "miss"
    metrics.observe_torrent_cache(media_type, cache_state, torrent_count)
    log.info(
        "scrape.cache.checked",
        "Torrent cache checked",
        content_id=media_id,
        cache_state=cache_state,
        candidate_count=torrent_count,
    )
    initial_info_hashes = set(torrent_manager.torrents)

    cache_manager = CacheStateManager(media_id)
    cache_result = await cache_manager.check_and_decide(torrent_count)
    force_scrape_now = not torrent_manager.primary_cached
    account_snapshot_ready = False
    torrent_discovery_inflight = False
    discovery_result = None
    if cache_result.should_scrape_background and not force_scrape_now:
        add_background_task(
            background_scrape,
            torrent_manager,
            media_id,
            debrid_entries,
            ip,
            session,
        )

    if cache_result.should_scrape_now or force_scrape_now:
        if use_account_scrape:
            torrent_discovery_result, _ = await asyncio.gather(
                torrent_manager.scrape_torrents(ScrapeContext.LIVE),
                ensure_account_snapshot_ready(session, debrid_entries, ip),
            )
            account_snapshot_ready = True
        else:
            torrent_discovery_result = await torrent_manager.scrape_torrents(
                ScrapeContext.LIVE
            )
        torrent_discovery_inflight = torrent_discovery_result.inflight
        await _mark_scope_scraped_if_populated(media_id, torrent_manager.torrents)

    if discovery_result is None:
        discovery_result = await discovery_task
    candidates = await _filter_and_rank_discovery_candidates(
        discovery_result.candidates,
        title=title,
        year=year,
        year_end=year_end,
        media_type=media_type,
        aliases=aliases,
        remove_adult_content=remove_adult_content,
        config=config,
        content_id=media_id,
    )
    await torrent_manager.ingest_release_candidates(
        "configured-discovery", discovery_result.candidates
    )

    service_cache_status = defaultdict(dict)
    verified_service_cache_status = defaultdict(dict)
    if use_account_scrape:
        if not account_snapshot_ready:
            await ensure_account_snapshot_ready(session, debrid_entries, ip)
        await schedule_account_snapshot_refresh(
            add_background_task, session, debrid_entries, ip
        )
        account_torrents = await get_account_torrents_for_media(
            debrid_entries,
            media_type,
            media_scope,
            title,
            year,
            year_end,
            search_season,
            search_episode,
            aliases,
            remove_adult_content,
            target_air_date=target_air_date,
            reject_unknown_episode_files=reject_unknown_episode_files,
        )

        for info_hash, account_torrent in account_torrents.items():
            existing_torrent = torrent_manager.torrents.get(info_hash)
            if existing_torrent is None:
                torrent_manager.torrents[info_hash] = account_torrent
                continue
            if (
                existing_torrent.get("fileIndex") is None
                and account_torrent["fileIndex"] is not None
            ):
                existing_torrent["fileIndex"] = account_torrent["fileIndex"]
            if (
                existing_torrent.get("size") is None
                and account_torrent["size"] is not None
            ):
                existing_torrent["size"] = account_torrent["size"]
            existing_parsed = existing_torrent.get("parsed")
            if existing_parsed is None or str(existing_parsed.resolution) == "unknown":
                existing_torrent["parsed"] = account_torrent["parsed"]

    if debrid_entries:
        existing_service_cache_status = await check_multi_service_availability(
            debrid_entries,
            torrent_manager.torrents,
            search_season,
            search_episode,
            media_scope,
            content_id=media_id,
        )
        merge_service_cache_status(service_cache_status, existing_service_cache_status)
        merge_service_cache_status(
            verified_service_cache_status, existing_service_cache_status
        )
    elif enable_torrent:
        await DebridService.apply_cached_availability_any_service(
            list(torrent_manager.torrents),
            search_season,
            search_episode,
            media_scope,
            torrent_manager.torrents,
        )

    current_info_hashes = set(torrent_manager.torrents)
    debrid_refresh_hashes = select_debrid_refresh_hashes(
        current_info_hashes,
        initial_info_hashes,
        verified_service_cache_status,
        had_cached_torrents=cache_result.has_cached_torrents,
        use_account_scrape=use_account_scrape,
    )

    debrid_errors = {}
    if debrid_entries and debrid_refresh_hashes:
        torrents_to_check = {
            info_hash: torrent
            for info_hash, torrent in torrent_manager.torrents.items()
            if info_hash in debrid_refresh_hashes
        }
        (
            fresh_service_cache_status,
            debrid_errors,
        ) = await get_and_cache_multi_service_availability(
            session,
            debrid_entries,
            torrents_to_check,
            media_id,
            media_only_id,
            search_season,
            search_episode,
            media_scope,
            ip,
            target_air_date=target_air_date,
            known_cache_status=service_cache_status,
        )
        merge_service_cache_status(service_cache_status, fresh_service_cache_status)

    await torrent_manager.rank_torrents(
        config["rtnSettings"],
        config["rtnRanking"],
        0,
        config["maxSize"],
        config["removeTrash"],
    )

    provider_options = ()
    rendered_candidate_ids = {}
    provider_capabilities = {}
    ranked_info_hashes = list(torrent_manager.ranked_torrents)
    if config["schemaVersion"] == 2:
        torrent_candidates = tuple(
            torrent_candidate_from_runtime(
                info_hash,
                torrent_manager.torrents[info_hash],
                media_id=media_only_id,
                scope=ReleaseScope(presentation_scope),
                season_norm=presentation_season,
                episode_norm=presentation_episode,
            )
            for info_hash in ranked_info_hashes
        )
        torrent_ids = {candidate.candidate_id for candidate in torrent_candidates}
        candidates = torrent_candidates + tuple(
            candidate
            for candidate in candidates
            if candidate.candidate_id not in torrent_ids
        )
        candidates = await asyncio.get_running_loop().run_in_executor(
            get_executor(),
            sort_candidates,
            candidates,
            config["rtnSettings"],
            config["rtnRanking"],
            0,
            config["maxSize"],
            config["removeTrash"],
        )
        ranked_info_hashes = [
            candidate.candidate_id.removeprefix("btih:")
            for candidate in candidates
            if candidate.candidate_id.startswith("btih:")
        ]
        (
            candidates,
            provider_options,
            rendered_candidate_ids,
            provider_capabilities,
        ) = await _prepare_provider_view(
            config,
            candidates,
            discovery_result.capability_plan,
            service_cache_status,
            media_type=media_type,
            season=search_season,
            episode=search_episode,
            season_norm=presentation_season,
            episode_norm=presentation_episode,
        )
        visible_candidate_ids = {candidate.candidate_id for candidate in candidates}
        ranked_info_hashes = [
            info_hash
            for info_hash in ranked_info_hashes
            if f"btih:{info_hash}" in visible_candidate_ids
        ]

    return MediaSearchResult(
        (
            MediaSearchStatus.BUSY
            if torrent_discovery_inflight
            and not ranked_info_hashes
            and not provider_options
            else MediaSearchStatus.OK
        ),
        metadata=metadata,
        aliases=aliases,
        media_scope=media_scope,
        torrents=torrent_manager.torrents,
        ranked_info_hashes=ranked_info_hashes,
        service_cache_status=service_cache_status,
        debrid_errors=debrid_errors,
        cache_state=cache_state,
        media_only_id=media_only_id,
        search_season=search_season,
        search_episode=search_episode,
        is_torrent_only=is_torrent_only,
        show_account_sync_trigger=use_account_scrape,
        use_account_scrape=use_account_scrape,
        candidates=candidates,
        discovery_diagnostics=discovery_result.diagnostics,
        provider_options=provider_options,
        rendered_candidate_ids=rendered_candidate_ids,
        provider_capabilities=provider_capabilities,
        candidate_count=(
            len(candidates) if config["schemaVersion"] == 2 else len(ranked_info_hashes)
        ),
    )
