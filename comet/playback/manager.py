"""Resolve signed playback intents through the configured provider graph."""

import asyncio
import hmac
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

import orjson

from comet.core.capabilities import CapabilityPlanner, CapabilityStateSnapshot
from comet.core.capability_bindings import (
    ensure_playback_capability_states,
    native_instance_credential_material,
    playback_provider_account_scopes,
    resolve_capability_options,
)
from comet.core.models import settings
from comet.core.provider_governor import ProviderGovernor
from comet.discovery.adapters.newznab import NewznabError
from comet.observability import log
from comet.playback.base import Readiness
from comet.playback.preparations import (
    ASSET_PREPARATION_PLAN_VERSIONS,
    PlaybackPreparation,
    PlaybackPreparationRepository,
)
from comet.playback.provider_preparations import (
    ProviderPreparationRepository,
    provider_selection_json,
)
from comet.playback.providers.altmount import AltMountError
from comet.playback.providers.nzbdav import NzbDavError
from comet.playback.providers.stremthru_newz import StremThruNewzError
from comet.playback.providers.torbox_usenet import (
    TorBoxUsenetError,
    cache_hashes_from_manifest,
)
from comet.playback.registry import build_playback_providers
from comet.playback.repository import RenderedReleaseRepository, ResolvedPlaybackIntent
from comet.playback.resolution_cache import ProviderResolutionCacheRepository
from comet.playback.tokens import CapabilityCodec, PlaybackIntent
from comet.services.lock import DistributedLock
from comet.usenet.access import NativeAccessAuthorizer
from comet.usenet.easynews import EasynewsNzbError
from comet.usenet.engine_client import EngineArchiveError, EngineClient, EngineNntpError
from comet.usenet.engine_transport import EngineUnavailable
from comet.usenet.file_selection import (
    ArchiveVolumeGroup,
    FileSelectionError,
    UsenetAsset,
    catalog_archive_members,
    catalog_archive_volume_groups,
    catalog_engine_source_assets,
    catalog_nested_archive_members,
    catalog_par2_assets,
    catalog_par2_source_assets,
    select_archive_volume_group,
    select_archive_volume_groups,
    select_asset,
)
from comet.usenet.limits import MAX_PAR2_VOLUMES, MAX_USENET_LOGICAL_BYTES
from comet.usenet.materialized_artifacts import (
    MaterializedArtifact,
    MaterializedArtifactRepository,
)
from comet.usenet.nzb_broker import NzbBrokerError
from comet.usenet.provider_exports import NzbProviderExportRepository, export_base_url
from comet.utils.http_client import http_client_manager

_NZBDAV_RECONCILIATION_SECONDS = 120
_TORBOX_RECONCILIATION_SECONDS = 60


@dataclass(frozen=True, slots=True)
class PlaybackIntentResolution:
    intent: PlaybackIntent
    provider: object
    provider_options: dict
    release: ResolvedPlaybackIntent
    account_partition: bytes
    credential_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedPlaybackIntent:
    resolution: PlaybackIntentResolution
    preparation: PlaybackPreparation
    capability: str


class NzbSourceError(ValueError):
    """A failed brokerage with explicit source and retry semantics."""

    def __init__(
        self,
        source_configuration_id: str,
        source_kind: str,
        *,
        code: str = "nzb_source_unavailable",
        operation: str,
        retryable: bool = False,
        retry_after: int | None = None,
        auth_failed: bool = False,
    ):
        super().__init__("NZB brokerage failed")
        self.source_configuration_id = source_configuration_id
        self.source_kind = source_kind
        self.code = code
        self.operation = operation
        self.retryable = retryable
        self.retry_after = retry_after
        self.auth_failed = auth_failed


@dataclass(frozen=True, slots=True)
class _Par2RecoverySet:
    volumes: tuple[tuple[str, str, int], ...]
    catalog: dict[str, object]
    asset_ids: tuple[str, ...]


@dataclass(slots=True)
class _Par2ArchiveRepairState:
    volumes: dict[str, tuple[str, str, int]]
    artifacts: dict[str, MaterializedArtifact]
    partial_source_mapped: bool


async def _gather_or_cancel(*operations):
    """Never leave sibling engine requests running after one operation fails."""
    tasks = tuple(asyncio.create_task(operation) for operation in operations)
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _log_par2_repair_attempt(
    repair_kind: str,
    recovery_volumes: list[tuple[str, str, int]],
    complete_sources: list[tuple[str, str, int]],
    partial_source_count: int,
) -> None:
    log.info(
        "usenet.par2_repair.started",
        "Usenet PAR2 repair started",
        operation=f"par2_repair_{repair_kind}",
        item_count=len(recovery_volumes),
        transferred_bytes=sum(volume[2] for volume in recovery_volumes),
        candidate_count=len(complete_sources),
        requested_count=partial_source_count,
    )


def _native_representation_signature(target: dict) -> tuple[object, ...]:
    """Describe one native blueprint without its replica-local handle."""
    source_kind = target["source_kind"]
    if source_kind == "session":
        revision = target["session_revision"]
    elif source_kind == "raw_composite":
        revision = target["asset_revision"]
    else:
        raise ValueError("native playback source is corrupt")
    return (
        source_kind,
        target["byte_size"],
        revision,
        target.get("strong_asset_revision"),
        target["selected_asset_id"],
        target["relative_path"],
        target.get("archive_set_identity"),
    )


def _native_archive_target(
    *,
    identity: str,
    byte_size: int,
    revision: str,
    selected_asset_id: str,
    relative_path: str,
    archive_set_identity: str,
    provider_set_generation: str,
    strong_asset_revision: str | None = None,
) -> dict[str, object]:
    target = {
        "source_kind": "raw_composite",
        "raw_composite_id": identity,
        "byte_size": byte_size,
        "asset_revision": revision,
        "selected_asset_id": selected_asset_id,
        "relative_path": relative_path,
        "archive_set_identity": archive_set_identity,
        "native_provider_set_generation": provider_set_generation,
    }
    if strong_asset_revision is not None:
        target["strong_asset_revision"] = strong_asset_revision
    return target


def _reconcile_native_representation(
    previous: dict,
    current: dict[str, object],
) -> bool:
    """Accept only an identical blueprint and monotonic strong-revision evidence."""
    previous_signature = _native_representation_signature(previous)
    current_signature = _native_representation_signature(current)
    previous_base = previous_signature[:3] + previous_signature[4:]
    current_base = current_signature[:3] + current_signature[4:]
    if previous_base != current_base:
        return False
    previous_revision = previous_signature[3]
    current_revision = current_signature[3]
    if previous_revision is None:
        return True
    if current_revision is None:
        current["strong_asset_revision"] = previous_revision
        return True
    return current_revision == previous_revision


def _provider_entry(config: Mapping[str, object], configuration_id: str) -> dict:
    for entry in config["playbackProviders"]:
        if entry["configurationId"] == configuration_id:
            return entry
    raise ValueError("playback provider is unavailable")


async def resolve_playback_intent(
    token: str,
    config: dict,
    database,
    session,
    *,
    client_ip: str = "",
    expected_client: str | None = "stremio",
) -> PlaybackIntentResolution:
    """Resolve only one signed provider option and its owner-scoped locators."""
    if not settings.COMET_CAPABILITY_SECRET:
        raise ValueError("signed playback is unavailable")
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    partition = codec.configuration_partition_for_config(config)
    intent = codec.decode_playback_intent(token, partition=partition)
    return await _resolve_decoded_intent(
        intent,
        config,
        database,
        session,
        partition=partition,
        client_ip=client_ip,
        expected_client=expected_client,
    )


async def resolve_nzb_handoff_intent(
    token: str,
    config: dict,
    database,
    session,
    *,
    client_ip: str = "",
) -> PlaybackIntentResolution:
    """Resolve one ``ni2`` only through its current Stremio-NNTP binding."""
    if not settings.USENET_ENABLED or not settings.COMET_CAPABILITY_SECRET:
        raise ValueError("Usenet playback is unavailable")
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    partition = codec.configuration_partition_for_config(config)
    intent = codec.decode_nzb_handoff_intent(token, partition=partition)
    resolution = await _resolve_decoded_intent(
        intent,
        config,
        database,
        session,
        partition=partition,
        client_ip=client_ip,
        expected_client="stremio",
    )
    if resolution.provider.descriptor.kind != "stremio_nntp":
        raise ValueError("NZB handoff provider is unavailable")
    return resolution


async def _resolve_decoded_intent(
    intent: PlaybackIntent,
    config: dict,
    database,
    session,
    *,
    partition: bytes,
    client_ip: str,
    expected_client: str | None,
) -> PlaybackIntentResolution:
    if expected_client is not None and intent.client != expected_client:
        raise ValueError("playback client is unavailable")
    entry = _provider_entry(config, intent.provider_configuration_id)
    user_session = await http_client_manager.get_user_session()
    providers = build_playback_providers(
        config,
        session,
        user_session=user_session,
        database=database,
        client_ip=client_ip,
        eligible_configuration_ids=frozenset({intent.provider_configuration_id}),
    )
    instance_credential_material = {
        "comet_native_usenet": native_instance_credential_material(
            settings.USENET_NATIVE_ACCESS_TOKEN,
            settings.USENET_NATIVE_SERVERS,
        )
    }
    provider_states = await ensure_playback_capability_states(
        config,
        CapabilityCodec(settings.COMET_CAPABILITY_SECRET),
        database,
        providers,
        provider_configuration_ids=frozenset({intent.provider_configuration_id}),
        instance_credential_material=instance_credential_material,
    )
    plan = CapabilityPlanner(
        usenet_offered=settings.USENET_ENABLED,
        native_authorizer=NativeAccessAuthorizer(settings.USENET_NATIVE_ACCESS_TOKEN),
        native_engine_enabled=settings.USENET_ENGINE_ENABLED,
        native_instance_pool_available=bool(settings.USENET_NATIVE_SERVERS),
        native_user_servers_allowed=settings.USENET_NATIVE_ALLOW_USER_SERVERS,
    ).build(
        config,
        CapabilityStateSnapshot(provider_states, {}),
    )
    eligible = next(
        (
            provider
            for provider in plan.providers
            if provider.configuration_id == intent.provider_configuration_id
        ),
        None,
    )
    if eligible is None:
        raise ValueError("playback provider is unavailable")
    provider = providers.get(eligible.configuration_id)
    if provider is None or provider.descriptor.kind != eligible.kind:
        raise ValueError("playback provider is unavailable")
    repository = RenderedReleaseRepository(database)
    release = await repository.resolve_intent(
        intent.candidate_id,
        list(intent.locator_ids),
        owner_configuration_partition=partition,
    )
    repository.authorize_intent(
        release,
        provider_configuration_id=eligible.configuration_id,
        provider_kind=eligible.kind,
        owner_configuration_partition=partition,
    )
    accounts = config["accounts"] or {}
    account_scope = playback_provider_account_scopes(
        config,
        CapabilityCodec(settings.COMET_CAPABILITY_SECRET),
        provider_configuration_ids=frozenset({intent.provider_configuration_id}),
        instance_credential_material=instance_credential_material,
    ).get(intent.provider_configuration_id)
    if account_scope is None:
        raise ValueError("playback provider account is unavailable")
    return PlaybackIntentResolution(
        intent,
        provider,
        resolve_capability_options(entry, accounts),
        release,
        account_scope[0],
        account_scope[1],
    )


async def create_playback_preparation(
    token: str,
    config: dict,
    database,
    session,
    *,
    client_ip: str = "",
    expected_client: str | None = "stremio",
) -> PreparedPlaybackIntent:
    """Persist one v2 intent before any provider-side preparation begins."""
    resolution = await resolve_playback_intent(
        token,
        config,
        database,
        session,
        client_ip=client_ip,
        expected_client=expected_client,
    )
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    partition = codec.configuration_partition_for_config(config)
    preparation = await PlaybackPreparationRepository(database).get_or_create(
        resolution.intent,
        provider_kind=resolution.provider.descriptor.kind,
        owner_configuration_partition=partition,
        preparation_intent_key=codec.asset_preparation_intent_key(
            partition=partition,
            candidate_id=resolution.intent.candidate_id,
            provider_configuration_id=(resolution.intent.provider_configuration_id),
            ordered_locator_ids=resolution.intent.locator_ids,
            selection_intent=resolution.intent.selection_intent,
            parser_selector_plan_versions=ASSET_PREPARATION_PLAN_VERSIONS,
        ),
    )
    capability = codec.encode(
        "pa2",
        partition=partition,
        suffix=[uuid.UUID(preparation.preparation_id).bytes],
        ttl=6 * 60 * 60,
    )
    return PreparedPlaybackIntent(resolution, preparation, capability)


async def resolve_prepared_asset(
    token: str,
    config: dict,
    database,
    session,
    *,
    client_ip: str = "",
    expected_client: str | None = "stremio",
) -> PreparedPlaybackIntent:
    """Resolve a ``pa2`` record through the same capability checks as ``pi2``."""
    if not settings.COMET_CAPABILITY_SECRET:
        raise ValueError("signed playback is unavailable")
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    partition = codec.configuration_partition_for_config(config)
    asset = codec.decode_prepared_asset(token, partition=partition)
    preparation = await PlaybackPreparationRepository(database).resolve(
        asset.preparation_id, owner_configuration_partition=partition
    )
    intent = PlaybackIntent(
        preparation.candidate_id,
        preparation.provider_configuration_id,
        preparation.locator_ids,
        preparation.selection_intent,
        expected_client if expected_client is not None else preparation.client,
    )
    resolution = await _resolve_decoded_intent(
        intent,
        config,
        database,
        session,
        partition=partition,
        client_ip=client_ip,
        expected_client=expected_client,
    )
    if resolution.provider.descriptor.kind != preparation.provider_kind:
        raise ValueError("playback provider is unavailable")
    if preparation.state == "ready":
        now = time.time()
        await ProviderResolutionCacheRepository(database).validate_ready(
            rendered_candidate_id=preparation.candidate_id,
            provider_kind=preparation.provider_kind,
            provider_configuration_id=preparation.provider_configuration_id,
            account_partition=resolution.account_partition,
            selection_intent_json=orjson.dumps(
                list(preparation.selection_intent)
            ).decode(),
            client=preparation.client,
            target_kind=preparation.target_kind,
            representation=(
                PlaybackPreparationRepository.representation_metadata(
                    preparation.target_ref,
                    state=preparation.state,
                )
            ),
            observed_at=now,
            expires_at=preparation.expires_at,
        )
    return PreparedPlaybackIntent(resolution, preparation, token)


async def broker_nzb_sources(
    prepared: PreparedPlaybackIntent,
    broker,
    database,
    adapters: Mapping[str, object],
    *,
    owner_configuration_partition: bytes,
) -> PreparedPlaybackIntent:
    """Resolve signed NZB transforms through only their originating accounts."""
    transformed = await broker_nzb_release(
        prepared.resolution.release,
        broker,
        database,
        adapters,
        provider_configuration_id=(prepared.preparation.provider_configuration_id),
        provider_kind=prepared.preparation.provider_kind,
        owner_configuration_partition=owner_configuration_partition,
    )
    artifact_payload = next(
        (
            locator["payload"]
            for locator in transformed.locators
            if locator["kind"] == "nzb_artifact"
        ),
        None,
    )
    if artifact_payload is not None:
        artifact_sha256 = artifact_payload["artifact_sha256"]
        manifest_identity = artifact_payload["manifest_identity"]
        owned = await broker.resolve_owned_artifact(
            artifact_sha256,
            owner_configuration_partition=owner_configuration_partition,
        )
        if owned.nm1 != manifest_identity:
            raise ValueError("brokered NZB manifest changed")
        await PlaybackPreparationRepository(database).bind_artifact(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            artifact_grant_id=owned.grant_id,
            artifact_sha256=artifact_sha256,
            manifest_identity=manifest_identity,
        )
    resolution = PlaybackIntentResolution(
        prepared.resolution.intent,
        prepared.resolution.provider,
        prepared.resolution.provider_options,
        transformed,
        prepared.resolution.account_partition,
        prepared.resolution.credential_fingerprint,
    )
    return PreparedPlaybackIntent(
        resolution,
        prepared.preparation,
        prepared.capability,
    )


async def broker_nzb_release(
    release: ResolvedPlaybackIntent,
    broker,
    database,
    adapters: Mapping[str, object],
    *,
    provider_configuration_id: str,
    provider_kind: str,
    owner_configuration_partition: bytes,
) -> ResolvedPlaybackIntent:
    """Transform one resolved release without creating a playback preparation."""
    if any(locator["kind"] == "nzb_artifact" for locator in release.locators):
        RenderedReleaseRepository.authorize_intent(
            release,
            provider_configuration_id=provider_configuration_id,
            provider_kind=provider_kind,
            owner_configuration_partition=owner_configuration_partition,
        )
        return release
    sources = tuple(
        locator
        for locator in release.locators
        if locator["kind"] in {"real_nzb", "easynews_http"}
    )
    if not sources:
        return release
    repository = RenderedReleaseRepository(database)

    async def reuse(source: dict) -> ResolvedPlaybackIntent | None:
        source_locator_id = source["locator_id"]
        attached = await repository.brokered_artifacts(
            release.candidate_id,
            source_locator_id,
            owner_configuration_partition=owner_configuration_partition,
        )
        for locator in attached:
            artifact_sha256 = locator["payload"]["artifact_sha256"]
            try:
                await broker.resolve_owned_artifact(
                    artifact_sha256,
                    owner_configuration_partition=owner_configuration_partition,
                )
            except NzbBrokerError:
                continue
            return _release_with_brokered_artifact(
                release,
                locator,
                provider_configuration_id=provider_configuration_id,
                provider_kind=provider_kind,
                owner_configuration_partition=owner_configuration_partition,
            )
        return None

    for source in sources:
        if reused := await reuse(source):
            return reused

    async def transform(
        source,
        operation,
        operation_argument,
        source_locator_id,
        adapter_configuration_id,
        normalized_source_kind,
        failure_operation,
    ) -> ResolvedPlaybackIntent:
        if reused := await reuse(source):
            return reused
        try:
            document = await operation(operation_argument)
        except (NewznabError, EasynewsNzbError) as exc:
            raise NzbSourceError(
                adapter_configuration_id,
                normalized_source_kind,
                code=exc.code,
                operation=failure_operation,
                retryable=exc.retryable,
                retry_after=exc.retry_after,
                auth_failed=exc.auth_failed,
            ) from exc
        try:
            artifact = await broker.ingest_bytes(
                document,
                owner_configuration_partition=owner_configuration_partition,
            )
        except EngineUnavailable as exc:
            raise NzbSourceError(
                adapter_configuration_id,
                normalized_source_kind,
                code="native_engine_unavailable",
                operation="nzb_parse",
                retryable=True,
            ) from exc
        except NzbBrokerError as exc:
            raise NzbSourceError(
                adapter_configuration_id,
                normalized_source_kind,
                code=exc.code,
                operation="nzb_parse",
            ) from exc
        locator = await repository.attach_brokered_artifact(
            release.candidate_id,
            source_locator_id,
            artifact.artifact_sha256,
            artifact.nm1,
            owner_configuration_partition=owner_configuration_partition,
        )
        return _release_with_brokered_artifact(
            release,
            locator,
            provider_configuration_id=provider_configuration_id,
            provider_kind=provider_kind,
            owner_configuration_partition=owner_configuration_partition,
        )

    last_failure: NzbSourceError
    for source in sources:
        payload = source["payload"]
        source_locator_id = source["locator_id"]
        source_kind = source["kind"]
        if source_kind == "real_nzb":
            adapter_configuration_id = payload["adapter_configuration_id"]
            operation_name = "grab"
            operation_argument = payload["remote_guid"]
            failure_operation = "nzb_grab"
            normalized_source_kind = "real_nzb"
        else:
            adapter_configuration_id = payload["account_configuration_id"]
            operation_name = "generate_nzb"
            operation_argument = payload
            failure_operation = "nzb_generate"
            normalized_source_kind = "easynews"
        adapter = adapters.get(adapter_configuration_id)
        operation = getattr(adapter, operation_name, None)
        if not callable(operation):
            last_failure = NzbSourceError(
                adapter_configuration_id,
                normalized_source_kind,
                operation=failure_operation,
            )
            continue
        lock = DistributedLock(
            f"usenet-broker:{source_locator_id}",
            timeout=180,
            retry_interval=0.1,
            database=database,
        )
        if not await lock.acquire(wait_timeout=20):
            last_failure = NzbSourceError(
                adapter_configuration_id,
                normalized_source_kind,
                code="provider_limit_exhausted",
                operation=failure_operation,
                retryable=True,
                retry_after=2,
            )
            continue

        try:
            return await lock.run(
                transform(
                    source,
                    operation,
                    operation_argument,
                    source_locator_id,
                    adapter_configuration_id,
                    normalized_source_kind,
                    failure_operation,
                )
            )
        except asyncio.CancelledError:
            raise
        except NzbSourceError as exc:
            last_failure = exc
        finally:
            await lock.release()
    raise last_failure


async def prepare_nzbdav(
    prepared: PreparedPlaybackIntent,
    broker,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    """Submit one owner-granted immutable NZB to the selected NzbDAV binding."""
    artifact_locator = _brokered_artifact_locator(prepared)
    if artifact_locator is None:
        raise ValueError("NzbDAV requires a brokered NZB artifact")
    artifact_sha256 = artifact_locator["payload"]["artifact_sha256"]
    owned = await broker.resolve_owned_artifact(
        artifact_sha256,
        owner_configuration_partition=owner_configuration_partition,
    )
    provider = prepared.resolution.provider
    provider_options = prepared.resolution.provider_options
    category = provider.category_for(
        provider_options,
        prepared.preparation.selection_intent,
    )
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    endpoint, credential_material = provider.credential_binding(provider_options)
    fingerprint = codec.provider_credential_fingerprint(
        "nzbdav",
        endpoint,
        credential_material,
    )
    selection_json = provider_selection_json(prepared.preparation.selection_intent)
    mutation_key = codec.provider_mutation_key(
        partition=owner_configuration_partition,
        provider_configuration_id=prepared.preparation.provider_configuration_id,
        credential_fingerprint=fingerprint,
        source_discriminator=owned.grant_id,
        selection_json=selection_json,
        operation=f"nzbdav-addfile:{category}",
        contract_version="v2",
    )
    provider_repository = ProviderPreparationRepository(database)
    ledger_arguments = {
        "owner_configuration_partition": owner_configuration_partition,
        "provider_configuration_id": prepared.preparation.provider_configuration_id,
        "credential_fingerprint": fingerprint,
        "candidate_id": prepared.preparation.candidate_id,
        "locator_id": artifact_locator["locator_id"],
        "artifact_grant_id": owned.grant_id,
        "selection_json": selection_json,
        "mutation_idempotency_key": mutation_key,
        "provider_kind": "nzbdav",
    }
    ledger = await provider_repository.get_existing(**ledger_arguments)
    created = False
    if ledger is None:
        ledger, created = await provider_repository.get_or_create(**ledger_arguments)
    playback_repository = PlaybackPreparationRepository(database)
    if ledger.state == "terminal":
        ledger_status = ledger.payload["status"]
        if ledger_status == "selected":
            target = _nzbdav_selected_target(ledger.payload)
            await playback_repository.mark_ready(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                target_kind="cloud",
                target_ref={
                    "provider_preparation_id": ledger.preparation_id,
                    **target,
                    "verified_name": f"comet-{artifact_sha256}",
                    "category": category,
                },
            )
            return "ready"
        await playback_repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code=(
                "nzbdav_credentials_rejected"
                if ledger_status == "credentials_rejected"
                else "remote_failed"
            ),
        )
        return "failed"
    reconciliation = None
    resubmit = False
    if not created and ledger.state == "mutation_pending":
        if ledger.payload:
            await playback_repository.mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                code="ambiguous_submission",
            )
            return "failed"
        reconciliation = await provider.reconcile_artifact(
            provider_options,
            artifact_sha256,
            category,
        )
        if reconciliation is None:
            if time.time() - ledger.created_at < _NZBDAV_RECONCILIATION_SECONDS:
                await playback_repository.mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code="ambiguous_submission",
                )
                return "failed"
            await asyncio.sleep(2)
            reconciliation = await provider.reconcile_artifact(
                provider_options,
                artifact_sha256,
                category,
            )
            if reconciliation is None:
                resubmit = await provider_repository.begin_nzbdav_resubmission(
                    ledger.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                )
                if not resubmit:
                    await playback_repository.mark_failed(
                        prepared.preparation.preparation_id,
                        owner_configuration_partition=owner_configuration_partition,
                        provider_account_partition=prepared.resolution.account_partition,
                        code="ambiguous_submission",
                    )
                    return "failed"
    if created or resubmit:
        try:
            job_id = await provider.submit_artifact(
                provider_options,
                await broker.read_owned_artifact(
                    artifact_sha256,
                    owner_configuration_partition=owner_configuration_partition,
                ),
                artifact_sha256,
                category,
            )
        except NzbDavError as exc:
            if exc.mutation_rejected:
                if created:
                    await provider_repository.discard_rejected_nzbdav_submission(
                        ledger.preparation_id,
                        owner_configuration_partition=owner_configuration_partition,
                    )
                else:
                    await provider_repository.restore_rejected_nzbdav_resubmission(
                        ledger.preparation_id,
                        owner_configuration_partition=owner_configuration_partition,
                    )
            raise
        await provider_repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            remote_id=job_id,
            remote_hash=artifact_sha256,
            status="queued",
            ownership="created",
        )
    elif reconciliation is not None:
        job_id = reconciliation.job_id
        await provider_repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            remote_id=job_id,
            remote_hash=artifact_sha256,
            status=reconciliation.status,
            ownership="unknown",
        )
    else:
        job_id = ledger.payload["remote_id"]
        if ledger.payload["remote_hash"] != artifact_sha256:
            raise ValueError("NzbDAV preparation is corrupt")
    if reconciliation is not None and reconciliation.status == "failed":
        await provider_repository.record_terminal_status(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            status=reconciliation.status,
        )
        await playback_repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="remote_failed",
        )
        return "failed"
    await playback_repository.record_pending_target(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "provider_preparation_id": ledger.preparation_id,
            "remote_job_id": job_id,
            "artifact_sha256": artifact_sha256,
            "category": category,
        },
    )
    return "pending"


def _archive_acquisition_plan(
    archive_assets: tuple[UsenetAsset, ...],
    par2_source_assets: tuple[UsenetAsset, ...],
    selection_intent: tuple[object, ...],
) -> tuple[
    ArchiveVolumeGroup | None,
    tuple[UsenetAsset, ...],
    tuple[UsenetAsset, ...],
]:
    if not archive_assets:
        return None, par2_source_assets, ()
    group = select_archive_volume_group(archive_assets, selection_intent)
    selected_ids = {asset.asset_id for asset in group.volumes}
    recovery_assets = par2_source_assets + tuple(
        asset for asset in archive_assets if asset.asset_id not in selected_ids
    )
    return group, group.volumes, recovery_assets


def _par2_proven_archive_group(
    archive_assets: tuple[UsenetAsset, ...],
    recovery: _Par2RecoverySet,
    selection_intent: tuple[object, ...],
) -> ArchiveVolumeGroup:
    """Bind a fully obfuscated NZB archive set to its PAR2 description."""
    source_assets = catalog_par2_source_assets(
        recovery.catalog["set_id"],
        recovery.catalog["slice_size"],
        recovery.catalog["files"],
    )
    groups = catalog_archive_volume_groups(source_assets)
    recovery_group = select_archive_volume_groups(groups, selection_intent)
    if (
        len(groups) != 1
        or len(recovery_group.volumes) != len(archive_assets)
        or sorted(asset.declared_bytes for asset in recovery_group.volumes)
        != sorted(asset.declared_bytes for asset in archive_assets)
    ):
        raise EngineArchiveError("par2_source_unmatched", retryable=False)
    return ArchiveVolumeGroup(recovery_group.selection_path, archive_assets)


async def _open_par2_proven_session_archive_source(
    engine: EngineClient,
    manifest: object,
    archive_assets: tuple[UsenetAsset, ...],
    par2_assets: tuple[UsenetAsset, ...],
    source_failure: EngineArchiveError,
    selection_intent: tuple[object, ...],
    *,
    engine_servers: list[dict[str, object]],
    account_partition: bytes,
    provider_set_generation: str,
) -> tuple[
    ArchiveVolumeGroup,
    tuple[dict[str, object], str, int, str, UsenetAsset],
]:
    async for recovery in _iter_par2_recovery(
        engine,
        manifest,
        par2_assets,
        source_failure,
        engine_servers=engine_servers,
        account_partition=account_partition,
        provider_set_generation=provider_set_generation,
    ):
        try:
            group = await asyncio.to_thread(
                _par2_proven_archive_group,
                archive_assets,
                recovery,
                selection_intent,
            )
        except (EngineArchiveError, FileSelectionError):
            continue
        return group, await _open_session_archive_source(
            engine,
            manifest,
            group,
            selection_intent,
            engine_servers=engine_servers,
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
        )
    raise source_failure


async def prepare_native_usenet(
    prepared: PreparedPlaybackIntent,
    broker,
    database,
    engine: EngineClient,
    *,
    owner_configuration_partition: bytes,
) -> tuple[str, int, str]:
    """Open one unambiguous brokered file as a local sparse native session."""
    artifact = _brokered_artifact(prepared)
    if artifact is None:
        raise ValueError("native Usenet requires a brokered NZB artifact")
    artifact_sha256 = artifact["artifact_sha256"]
    resolved = await broker.resolve_owned_artifact(
        artifact_sha256, owner_configuration_partition=owner_configuration_partition
    )
    archive_passphrase = resolved.metadata.get("password")
    selection_hint = artifact_selection_hint(artifact)
    engine_assets = await engine.catalog_nntp_artifact(
        artifact_sha256,
        resolved.nm1,
        resolved.metadata,
        resolved.manifest,
        selection_hint=selection_hint,
    )
    source_assets = await asyncio.to_thread(
        catalog_engine_source_assets, artifact_sha256, engine_assets
    )
    par2_assets = tuple(asset for asset in source_assets if asset.kind == "par2")
    par2_source_assets = tuple(
        asset for asset in source_assets if asset.kind == "par2_source"
    )
    provider = prepared.resolution.provider
    servers = provider.servers_for(prepared.resolution.provider_options)
    # Python's sort is stable: configured list order deliberately breaks ties
    # within the same primary/backup priority class.
    ordered_servers = sorted(servers, key=lambda item: (item.backup, item.priority))
    engine_servers = [
        {
            "provider_configuration_id": server.name,
            "host": server.host,
            "port": server.port,
            "tls_mode": server.tls_mode,
            "allow_private": prepared.resolution.provider_options.get("source")
            == "instance_pool",
            "username": server.username,
            "password": server.password,
            "connections": server.connections,
            "pipeline": server.pipeline,
            "priority": server.priority,
            "backup": server.backup,
        }
        for server in ordered_servers
    ]
    native_account_partition = prepared.resolution.account_partition
    provider_set_generation = hmac.digest(
        native_account_partition,
        b"comet-native-provider-set-v1\0"
        + orjson.dumps(engine_servers, option=orjson.OPT_SORT_KEYS),
        "sha256",
    ).hex()
    materialized_artifacts: list[MaterializedArtifact] = []
    direct_assets = tuple(asset for asset in source_assets if asset.kind == "video")
    if direct_assets:
        selected_asset = await asyncio.to_thread(
            select_asset,
            direct_assets,
            prepared.preparation.selection_intent,
        )
        ordered_postings, group = _native_manifest_source(
            resolved.manifest, selected_asset
        )
        try:
            await engine.inspect_nntp_postings(
                artifact_sha256,
                ordered_postings,
                group=group,
                servers=engine_servers,
                account_partition=native_account_partition,
                provider_set_generation=provider_set_generation,
            )
            (
                identity,
                byte_size,
                revision,
                strong_asset_revision,
            ) = await engine.open_nntp_session(
                ordered_postings,
                group=group,
                servers=engine_servers,
                account_partition=native_account_partition,
                provider_set_generation=provider_set_generation,
                allow_degraded_playback=settings.USENET_DEGRADED_PLAYBACK_ENABLED,
            )
        except EngineNntpError as exc:
            if not exc.source_failure:
                raise
            source_failure = exc
            (
                (
                    identity,
                    byte_size,
                    revision,
                    target_ref,
                ),
                published,
            ) = await _run_published_materialization(
                prepared,
                resolved,
                database,
                owner_configuration_partition,
                lambda: _repair_direct_asset(
                    engine,
                    resolved.manifest,
                    par2_assets,
                    selected_asset,
                    source_failure,
                    prepared.preparation.selection_intent,
                    engine_servers=engine_servers,
                    account_partition=native_account_partition,
                    provider_set_generation=provider_set_generation,
                ),
                lambda result: (
                    MaterializedArtifact(
                        result[0],
                        result[1],
                        result[3]["selected_asset_id"],
                        result[3]["strong_asset_revision"],
                    ),
                ),
            )
            materialized_artifacts.extend(published)
        else:
            target_ref = {
                "source_kind": "session",
                "session_id": identity,
                "byte_size": byte_size,
                "session_revision": revision,
                "selected_asset_id": selected_asset.asset_id.hex(),
                "relative_path": selected_asset.relative_path,
                "native_provider_set_generation": provider_set_generation,
            }
            if strong_asset_revision is not None:
                target_ref["strong_asset_revision"] = strong_asset_revision
    else:
        archive_source_assets = tuple(
            asset
            for asset in source_assets
            if asset.kind in {"archive", "split", "logical_split", "logical_archive"}
        )
        (
            archive_group,
            initial_volume_assets,
            recovery_source_assets,
        ) = await asyncio.to_thread(
            _archive_acquisition_plan,
            archive_source_assets,
            par2_source_assets,
            prepared.preparation.selection_intent,
        )
        logical_source = False
        sparse_result = None
        if archive_group is not None:
            logical_source = archive_group.volumes[0].kind in {
                "logical_split",
                "logical_archive",
            }
            try:
                (
                    sparse_plan,
                    identity,
                    byte_size,
                    revision,
                    sparse_asset,
                ) = await _open_session_archive_source(
                    engine,
                    resolved.manifest,
                    archive_group,
                    prepared.preparation.selection_intent,
                    engine_servers=engine_servers,
                    account_partition=native_account_partition,
                    provider_set_generation=provider_set_generation,
                )
            except EngineArchiveError as exc:
                if exc.retryable:
                    raise
                if par2_assets and len(archive_source_assets) > len(
                    initial_volume_assets
                ):
                    try:
                        (
                            archive_group,
                            sparse_result,
                        ) = await _open_par2_proven_session_archive_source(
                            engine,
                            resolved.manifest,
                            archive_source_assets,
                            par2_assets,
                            exc,
                            prepared.preparation.selection_intent,
                            engine_servers=engine_servers,
                            account_partition=native_account_partition,
                            provider_set_generation=provider_set_generation,
                        )
                    except EngineNntpError:
                        raise
                    except (EngineArchiveError, FileSelectionError):
                        pass
            except EngineNntpError as exc:
                if not exc.source_failure:
                    raise
            else:
                sparse_result = (
                    sparse_plan,
                    identity,
                    byte_size,
                    revision,
                    sparse_asset,
                )
            if sparse_result is not None:
                (
                    sparse_plan,
                    identity,
                    byte_size,
                    revision,
                    sparse_asset,
                ) = sparse_result
                target_ref = _native_archive_target(
                    identity=identity,
                    byte_size=byte_size,
                    revision=revision,
                    selected_asset_id=sparse_asset.asset_id.hex(),
                    relative_path=sparse_asset.relative_path,
                    archive_set_identity=sparse_plan["set_identity"],
                    provider_set_generation=provider_set_generation,
                )
                return await _finish_native_usenet_preparation(
                    prepared,
                    database,
                    owner_configuration_partition=owner_configuration_partition,
                    selection_hint=selection_hint,
                    target_ref=target_ref,
                    identity=identity,
                    byte_size=byte_size,
                    revision=revision,
                    materialized_artifacts=[],
                )
        runtime_stats = await engine.stats()
        materialization_slots = min(
            sum(server["connections"] for server in engine_servers),
            runtime_stats["request_workers"] - runtime_stats["requests_active"] + 1,
            runtime_stats["nntp_preparation_slots"],
        )
        materialization_gate = asyncio.Semaphore(materialization_slots)

        async def materialize_volume(volume_asset):
            postings, group = _native_manifest_source(resolved.manifest, volume_asset)
            volume_asset_id = volume_asset.asset_id.hex()
            try:
                async with materialization_gate:
                    (
                        (
                            volume_identity,
                            volume_size,
                            _volume_asset_revision,
                        ),
                        published,
                    ) = await _run_published_materialization(
                        prepared,
                        resolved,
                        database,
                        owner_configuration_partition,
                        lambda: engine.materialize_nntp_postings(
                            postings,
                            group=group,
                            servers=engine_servers,
                            account_partition=native_account_partition,
                            provider_set_generation=provider_set_generation,
                        ),
                        lambda result: (
                            MaterializedArtifact(
                                result[0],
                                result[1],
                                volume_asset_id,
                                result[2],
                            ),
                        ),
                    )
            except EngineNntpError as exc:
                if not exc.source_failure:
                    raise
                return None, (), (volume_asset, exc)
            return (
                (
                    volume_identity,
                    volume_asset.relative_path,
                    volume_size,
                ),
                published,
                None,
            )

        volume_results = await _gather_or_cancel(
            *(
                materialize_volume(volume_asset)
                for volume_asset in initial_volume_assets
            )
        )
        volumes = [
            volume
            for volume, _published, _failure in volume_results
            if volume is not None
        ]
        materialized_artifacts.extend(
            artifact
            for _volume, published, _failure in volume_results
            for artifact in published
        )
        failures = [
            failure
            for _volume, _published, failure in volume_results
            if failure is not None
        ]
        recovery_source_results = None

        async def materialize_recovery_sources():
            nonlocal recovery_source_results
            if recovery_source_results is None:
                recovery_source_results = await _gather_or_cancel(
                    *(materialize_volume(asset) for asset in recovery_source_assets)
                )
                materialized_artifacts.extend(
                    artifact
                    for _volume, published, _failure in recovery_source_results
                    for artifact in published
                )
            return (
                [
                    volume
                    for volume, _published, _failure in recovery_source_results
                    if volume is not None
                ],
                [
                    failure[0]
                    for _volume, _published, failure in recovery_source_results
                    if failure is not None
                ],
            )

        source_failure = failures[0][1] if failures else None
        partial_source_assets = [volume_asset for volume_asset, _failure in failures]
        if source_failure is not None or archive_group is None:
            (
                recovery_sources,
                partial_recovery_sources,
            ) = await materialize_recovery_sources()
            partial_source_assets.extend(partial_recovery_sources)
            (
                (
                    archive_group,
                    volumes,
                    repaired_artifacts,
                ),
                _published,
            ) = await _run_published_materialization(
                prepared,
                resolved,
                database,
                owner_configuration_partition,
                lambda: _repair_archive_closure(
                    engine,
                    resolved.manifest,
                    par2_assets,
                    tuple(partial_source_assets),
                    volumes + recovery_sources,
                    source_failure
                    or EngineArchiveError(
                        "archive_identity_unproven",
                        retryable=False,
                    ),
                    prepared.preparation.selection_intent,
                    engine_servers=engine_servers,
                    account_partition=native_account_partition,
                    provider_set_generation=provider_set_generation,
                ),
                lambda result: tuple(result[2]),
                allow_empty=True,
            )
            materialized_artifacts.extend(repaired_artifacts)
        try:
            plan = await engine.plan_archive_volumes(volumes)
        except EngineArchiveError as exc:
            if not par2_assets:
                raise
            layout_failure = exc
            (
                recovery_sources,
                partial_recovery_sources,
            ) = await materialize_recovery_sources()
            (
                (
                    archive_group,
                    volumes,
                    repaired_artifacts,
                ),
                _published,
            ) = await _run_published_materialization(
                prepared,
                resolved,
                database,
                owner_configuration_partition,
                lambda: _repair_archive_closure(
                    engine,
                    resolved.manifest,
                    par2_assets,
                    tuple(partial_recovery_sources),
                    volumes + recovery_sources,
                    layout_failure,
                    prepared.preparation.selection_intent,
                    engine_servers=engine_servers,
                    account_partition=native_account_partition,
                    provider_set_generation=provider_set_generation,
                ),
                lambda result: tuple(result[2]),
                allow_empty=True,
            )
            materialized_artifacts.extend(repaired_artifacts)
            plan = await engine.plan_archive_volumes(volumes)
        layout = plan["kind"]["layout"]
        if layout == "raw_split":
            identity, byte_size, revision = await engine.open_raw_composite(volumes)
            await engine.inspect_raw_composite(identity, byte_size)
            target_ref = _native_archive_target(
                identity=identity,
                byte_size=byte_size,
                revision=revision,
                selected_asset_id=plan["set_identity"],
                relative_path=archive_group.selection_path,
                archive_set_identity=plan["set_identity"],
                provider_set_generation=provider_set_generation,
            )
        else:
            stored_direct = True
            try:
                catalog_plan, members = await engine.catalog_stored_archive_volumes(
                    volumes
                )
            except EngineArchiveError as exc:
                if exc.retryable:
                    raise
                stored_direct = False
                catalog_plan, members = await engine.catalog_nested_archive_volumes(
                    volumes,
                    passphrase=archive_passphrase,
                )
            direct_assets = (
                catalog_archive_members(plan["set_identity"], members)
                if stored_direct
                else ()
            )
            strong_asset_revision = None
            if stored_direct and direct_assets:
                if catalog_plan != plan:
                    raise ValueError("native archive plan changed during preparation")
                selected_asset = await asyncio.to_thread(
                    select_asset,
                    direct_assets,
                    prepared.preparation.selection_intent,
                )
                (
                    _,
                    identity,
                    byte_size,
                    revision,
                ) = await engine.open_stored_archive_member(
                    volumes,
                    selected_asset.declared_bytes,
                    selected_asset.relative_path,
                )
                await engine.inspect_raw_composite(identity, byte_size)
                source_identity = identity
            else:
                if stored_direct:
                    catalog_plan, members = await engine.catalog_nested_archive_volumes(
                        volumes,
                        passphrase=archive_passphrase,
                    )
                if catalog_plan != plan:
                    raise ValueError("native archive plan changed during preparation")
                selected_asset = await asyncio.to_thread(
                    select_asset,
                    catalog_nested_archive_members(plan["set_identity"], members),
                    prepared.preparation.selection_intent,
                )
                (
                    (
                        identity,
                        byte_size,
                        strong_asset_revision,
                    ),
                    published,
                ) = await _run_published_materialization(
                    prepared,
                    resolved,
                    database,
                    owner_configuration_partition,
                    lambda: engine.extract_nested_archive_volume_set(
                        volumes,
                        selected_asset.declared_bytes,
                        selected_asset.selected_paths,
                        passphrase=archive_passphrase,
                    ),
                    lambda result: (
                        MaterializedArtifact(
                            result[0],
                            result[1],
                            selected_asset.asset_id.hex(),
                            result[2],
                        ),
                    ),
                )
                await engine.inspect_materialization(
                    identity,
                    byte_size,
                )
                source_identity = identity
                revision = identity
                materialized_artifacts = list(published)
            target_ref = _native_archive_target(
                identity=source_identity,
                byte_size=byte_size,
                revision=revision,
                selected_asset_id=selected_asset.asset_id.hex(),
                relative_path=selected_asset.relative_path,
                archive_set_identity=plan["set_identity"],
                provider_set_generation=provider_set_generation,
                strong_asset_revision=strong_asset_revision,
            )
        if logical_source:
            target_ref["relative_path"] = archive_group.selection_path
    return await _finish_native_usenet_preparation(
        prepared,
        database,
        owner_configuration_partition=owner_configuration_partition,
        selection_hint=selection_hint,
        target_ref=target_ref,
        identity=identity,
        byte_size=byte_size,
        revision=revision,
        materialized_artifacts=materialized_artifacts,
    )


async def _run_published_materialization(
    prepared: PreparedPlaybackIntent,
    resolved,
    database,
    owner_configuration_partition: bytes,
    operation,
    describe_artifacts,
    *,
    allow_empty: bool = False,
):
    """Keep orphan reconciliation out until published files enter the ledger."""
    repository = MaterializedArtifactRepository(
        settings.USENET_ARTIFACT_DIR,
        database,
    )
    lease = await repository.acquire_publication_lease(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
    )
    try:
        result = await operation()
        artifacts = tuple(describe_artifacts(result))
        if not artifacts and not allow_empty:
            raise ValueError("native publication returned no materializations")
        if artifacts:
            await repository.register_for_preparation(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                source_nm1=resolved.nm1,
                artifacts=artifacts,
            )
        return result, artifacts
    finally:
        await asyncio.shield(lease.close())


async def _repair_direct_asset(
    engine: EngineClient,
    manifest: object,
    par2_assets: tuple[UsenetAsset, ...],
    failed_asset: UsenetAsset,
    source_failure: EngineNntpError,
    selection_intent: tuple[object, ...],
    *,
    engine_servers: list[dict[str, object]],
    account_partition: bytes,
    provider_set_generation: str,
) -> tuple[str, int, str, dict[str, object]]:
    if len(selection_intent) == 2 and selection_intent[0] == 2:
        raise source_failure
    latest_recoveries = {}
    last_insufficient = None
    attempted_recoveries = set()
    required_recovery_blocks = {}
    async for recovery in _iter_par2_recovery(
        engine,
        manifest,
        par2_assets,
        source_failure,
        engine_servers=engine_servers,
        account_partition=account_partition,
        provider_set_generation=provider_set_generation,
    ):
        latest_recoveries[recovery.catalog["set_id"]] = recovery
        candidates = await asyncio.to_thread(
            catalog_par2_assets,
            recovery.catalog["set_id"],
            recovery.catalog["slice_size"],
            recovery.catalog["files"],
            known_video=(failed_asset.relative_path, failed_asset.declared_bytes),
        )
        exact_candidates = tuple(
            asset
            for asset in candidates
            if asset.kind == "video"
            and asset.relative_path == failed_asset.relative_path
            and asset.declared_bytes == failed_asset.declared_bytes
        )
        if not exact_candidates or not recovery.catalog["recovery_exponents"]:
            continue
        set_id = recovery.catalog["set_id"]
        if len(recovery.catalog["recovery_exponents"]) < required_recovery_blocks.get(
            set_id, 0
        ):
            continue
        attempted_recoveries.add((recovery.catalog["set_id"], recovery.asset_ids))
        try:
            return await _repair_direct_from_recovery(
                engine,
                manifest,
                failed_asset,
                recovery,
                exact_candidates,
                selection_intent,
                account_partition=account_partition,
                provider_set_generation=provider_set_generation,
            )
        except EngineArchiveError as exc:
            if exc.code != "repair_insufficient":
                raise
            last_insufficient = exc
            if exc.required_recovery_blocks is not None:
                required_recovery_blocks[set_id] = exc.required_recovery_blocks
                if exc.required_recovery_blocks > _maximum_par2_recovery_blocks(
                    par2_assets,
                    recovery.catalog["slice_size"],
                ):
                    raise

    recovery_by_asset_id = {}
    fallback_candidates = []
    for recovery in latest_recoveries.values():
        catalog = recovery.catalog
        assets = await asyncio.to_thread(
            catalog_par2_assets,
            catalog["set_id"],
            catalog["slice_size"],
            catalog["files"],
            known_video=(failed_asset.relative_path, failed_asset.declared_bytes),
        )
        fallback_candidates.extend(asset for asset in assets if asset.kind == "video")
        recovery_by_asset_id.update((asset.asset_id, recovery) for asset in assets)
    if not fallback_candidates:
        if last_insufficient is not None:
            raise last_insufficient
        raise FileSelectionError("container_probe_unsupported")
    selected = await asyncio.to_thread(
        select_asset,
        tuple(fallback_candidates),
        selection_intent,
    )
    recovery = recovery_by_asset_id[selected.asset_id]
    if not recovery.catalog["recovery_exponents"]:
        if last_insufficient is not None:
            raise last_insufficient
        raise source_failure
    set_id = recovery.catalog["set_id"]
    if last_insufficient is not None and (
        (set_id, recovery.asset_ids) in attempted_recoveries
        or len(recovery.catalog["recovery_exponents"])
        < required_recovery_blocks.get(set_id, 0)
    ):
        raise last_insufficient
    try:
        return await _repair_direct_from_recovery(
            engine,
            manifest,
            failed_asset,
            recovery,
            tuple(
                asset
                for asset in fallback_candidates
                if recovery_by_asset_id[asset.asset_id] is recovery
            ),
            selection_intent,
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
        )
    except EngineArchiveError as exc:
        if exc.code == "repair_insufficient" and last_insufficient is not None:
            raise last_insufficient
        raise


async def _repair_direct_from_recovery(
    engine: EngineClient,
    manifest: object,
    failed_asset: UsenetAsset,
    recovery: _Par2RecoverySet,
    candidates: tuple[UsenetAsset, ...],
    selection_intent: tuple[object, ...],
    *,
    account_partition: bytes,
    provider_set_generation: str,
) -> tuple[str, int, str, dict[str, object]]:
    partial_source = _native_manifest_source(manifest, failed_asset)
    set_id = recovery.catalog["set_id"]
    recovery_volumes = list(recovery.volumes)
    remaining = list(candidates)
    verified_sources: list[tuple[str, str, int]] = []
    mismatch: EngineArchiveError | None = None
    selected_asset = None
    repaired = None
    inspected = False
    while remaining:
        selected_asset = await asyncio.to_thread(
            select_asset,
            tuple(remaining),
            selection_intent,
        )
        file_id = selected_asset.source_file_id
        _log_par2_repair_attempt(
            "direct",
            recovery_volumes,
            verified_sources,
            1,
        )
        repaired = await engine.repair_par2(
            recovery_volumes,
            list(verified_sources),
            file_id,
            partial_sources=[partial_source],
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
            recovery_set_id=set_id,
        )
        if (
            repaired["set_id"] != set_id
            or repaired["relative_path"] != selected_asset.relative_path
            or repaired["byte_size"] != selected_asset.declared_bytes
        ):
            raise ValueError("native PAR2 repair changed selected source")
        identity = repaired["identity"]
        byte_size = repaired["byte_size"]
        try:
            await engine.inspect_materialization(
                identity,
                byte_size,
            )
            inspected = True
        except EngineArchiveError as exc:
            if exc.code != "container_signature_mismatch":
                raise
            mismatch = exc
            verified_sources.append((identity, selected_asset.relative_path, byte_size))
            remaining.remove(selected_asset)
            if selection_intent != (0,):
                break
            continue
        break
    else:
        selected_asset = None

    if selected_asset is None or repaired is None or not inspected:
        if mismatch is not None:
            raise mismatch
        raise FileSelectionError("file_selection_ambiguous")
    identity = repaired["identity"]
    byte_size = repaired["byte_size"]
    return (
        identity,
        byte_size,
        identity,
        {
            "source_kind": "raw_composite",
            "raw_composite_id": identity,
            "byte_size": byte_size,
            "asset_revision": identity,
            "strong_asset_revision": repaired["asset_revision"],
            "selected_asset_id": selected_asset.asset_id.hex(),
            "relative_path": selected_asset.relative_path,
            "native_provider_set_generation": provider_set_generation,
        },
    )


async def _repair_archive_closure(
    engine: EngineClient,
    manifest: object,
    par2_assets: tuple[UsenetAsset, ...],
    partial_source_assets: tuple[UsenetAsset, ...],
    complete_sources: list[tuple[str, str, int]],
    source_failure: EngineNntpError | EngineArchiveError,
    selection_intent: tuple[object, ...],
    *,
    engine_servers: list[dict[str, object]],
    account_partition: bytes,
    provider_set_generation: str,
) -> tuple[
    ArchiveVolumeGroup,
    list[tuple[str, str, int]],
    list[MaterializedArtifact],
]:
    latest_recoveries = {}
    repair_states = {}
    last_insufficient = None
    attempted_recoveries = set()
    required_recovery_blocks = {}
    async for recovery in _iter_par2_recovery(
        engine,
        manifest,
        par2_assets,
        source_failure,
        engine_servers=engine_servers,
        account_partition=account_partition,
        provider_set_generation=provider_set_generation,
    ):
        set_id = recovery.catalog["set_id"]
        latest_recoveries[set_id] = recovery
        if len(recovery.catalog["recovery_exponents"]) < required_recovery_blocks.get(
            set_id, 0
        ):
            continue
        try:
            attempted_recoveries.add((set_id, recovery.asset_ids))
            return await _repair_archive_from_recovery(
                engine,
                manifest,
                (recovery,),
                partial_source_assets,
                complete_sources,
                selection_intent,
                repair_states=repair_states,
                account_partition=account_partition,
                provider_set_generation=provider_set_generation,
            )
        except EngineArchiveError as exc:
            if exc.code not in {"par2_source_unmatched", "repair_insufficient"}:
                raise
            if exc.code == "repair_insufficient":
                last_insufficient = exc
                if exc.required_recovery_blocks is not None:
                    required_recovery_blocks[set_id] = exc.required_recovery_blocks
                    if exc.required_recovery_blocks > _maximum_par2_recovery_blocks(
                        par2_assets,
                        recovery.catalog["slice_size"],
                    ):
                        raise

    fallback = tuple(
        recovery
        for recovery in latest_recoveries.values()
        if recovery.catalog["recovery_exponents"]
    )
    if not fallback:
        if last_insufficient is not None:
            raise last_insufficient
        raise source_failure
    if last_insufficient is not None and all(
        (recovery.catalog["set_id"], recovery.asset_ids) in attempted_recoveries
        or len(recovery.catalog["recovery_exponents"])
        < required_recovery_blocks.get(recovery.catalog["set_id"], 0)
        for recovery in fallback
    ):
        raise last_insufficient
    try:
        return await _repair_archive_from_recovery(
            engine,
            manifest,
            fallback,
            partial_source_assets,
            complete_sources,
            selection_intent,
            repair_states=repair_states,
            account_partition=account_partition,
            provider_set_generation=provider_set_generation,
        )
    except EngineArchiveError as exc:
        if exc.code == "repair_insufficient" and last_insufficient is not None:
            raise last_insufficient
        raise


async def _repair_archive_from_recovery(
    engine: EngineClient,
    manifest: object,
    recovery_sets: tuple[_Par2RecoverySet, ...],
    partial_source_assets: tuple[UsenetAsset, ...],
    complete_sources: list[tuple[str, str, int]],
    selection_intent: tuple[object, ...],
    *,
    repair_states: dict[str, _Par2ArchiveRepairState],
    account_partition: bytes,
    provider_set_generation: str,
) -> tuple[
    ArchiveVolumeGroup,
    list[tuple[str, str, int]],
    dict[str, object] | None,
    list[MaterializedArtifact],
]:
    recovery_assets = []
    for recovery in recovery_sets:
        catalog = recovery.catalog
        source_assets = await asyncio.to_thread(
            catalog_par2_source_assets,
            catalog["set_id"],
            catalog["slice_size"],
            catalog["files"],
        )
        recovery_assets.append((recovery, source_assets))

    mapped_complete_sources: dict[str, tuple[str, str, int]] = {}
    ordered_complete_sources = sorted(complete_sources, key=lambda source: source[0])
    complete_sources_by_identity = {
        source[0]: source for source in ordered_complete_sources
    }
    if ordered_complete_sources:
        mapped_recovery_sets = []
        for candidate_recovery, candidate_assets in recovery_assets:
            candidate_set_id = candidate_recovery.catalog["set_id"]
            try:
                source_map = await engine.map_par2_sources(
                    list(candidate_recovery.volumes),
                    ordered_complete_sources,
                    recovery_set_id=candidate_set_id,
                )
            except EngineArchiveError as exc:
                if exc.code == "par2_source_unmatched":
                    continue
                raise
            candidate_sources = {
                mapping["file_id"]: complete_sources_by_identity[
                    mapping["content_identity"]
                ]
                for mapping in source_map["mappings"]
            }
            mapped_recovery_sets.append(
                (candidate_recovery, candidate_assets, candidate_sources)
            )
        if len(mapped_recovery_sets) > 1:
            raise FileSelectionError("file_selection_ambiguous")
        if not mapped_recovery_sets:
            raise EngineArchiveError("par2_source_unmatched", retryable=False)
        recovery, source_assets, mapped_complete_sources = mapped_recovery_sets[0]

    mapped_file_ids = set(mapped_complete_sources)
    if mapped_file_ids:
        mapped_assets = [
            asset for asset in source_assets if asset.source_file_id in mapped_file_ids
        ]
        if len(mapped_assets) != len(mapped_file_ids):
            raise FileSelectionError("file_selection_ambiguous")
        recovery_group = await asyncio.to_thread(
            select_archive_volume_group,
            source_assets,
            (2, mapped_assets[0].asset_id),
        )
        if not mapped_file_ids.issubset(
            {
                asset.source_file_id
                for asset in recovery_group.volumes
                if asset.source_file_id is not None
            }
        ):
            raise FileSelectionError("file_selection_ambiguous")
    else:
        groups = []
        recovery_by_group = {}
        for candidate_recovery, candidate_assets in recovery_assets:
            for group in await asyncio.to_thread(
                catalog_archive_volume_groups,
                candidate_assets,
            ):
                groups.append(group)
                recovery_by_group[id(group)] = (candidate_recovery, candidate_assets)
        recovery_group = await asyncio.to_thread(
            select_archive_volume_groups,
            groups,
            selection_intent,
        )
        recovery, source_assets = recovery_by_group[id(recovery_group)]

    set_id = recovery.catalog["set_id"]
    recovery_volumes = list(recovery.volumes)
    repair_state = repair_states.setdefault(
        set_id,
        _Par2ArchiveRepairState({}, {}, False),
    )

    repaired_volumes = []
    repaired_file_ids = []
    partial_sources = [
        _native_manifest_source(manifest, asset) for asset in partial_source_assets
    ]
    partial_source_options = (
        {
            "partial_sources": partial_sources,
            "account_partition": account_partition,
            "provider_set_generation": provider_set_generation,
        }
        if partial_sources
        else {}
    )
    repair_sources = list(ordered_complete_sources)
    repair_source_identities = {source[0] for source in ordered_complete_sources}
    source_mapping_proven = (
        bool(mapped_complete_sources) or repair_state.partial_source_mapped
    )
    for source_asset in recovery_group.volumes:
        file_id = source_asset.source_file_id
        complete_source = mapped_complete_sources.get(file_id)
        if complete_source is not None:
            if complete_source[2] != source_asset.declared_bytes:
                raise ValueError("native PAR2 source mapping changed source size")
            repaired_volumes.append(
                (
                    complete_source[0],
                    source_asset.relative_path,
                    source_asset.declared_bytes,
                )
            )
            continue
        cached_volume = repair_state.volumes.get(file_id)
        if cached_volume is not None:
            repaired_volumes.append(cached_volume)
            repaired_file_ids.append(file_id)
            if cached_volume[0] not in repair_source_identities:
                repair_sources.append(cached_volume)
                repair_sources.sort(key=lambda source: source[0])
                repair_source_identities.add(cached_volume[0])
            continue
        _log_par2_repair_attempt(
            "archive",
            recovery_volumes,
            repair_sources,
            len(partial_sources),
        )
        repaired = await engine.repair_par2(
            recovery_volumes,
            list(repair_sources),
            file_id,
            recovery_set_id=set_id,
            **partial_source_options,
        )
        if (
            repaired["set_id"] != set_id
            or repaired["relative_path"] != source_asset.relative_path
            or repaired["byte_size"] != source_asset.declared_bytes
        ):
            raise ValueError("native PAR2 repair changed selected source")
        repair_state.partial_source_mapped |= repaired["partial_source_mapped"]
        source_mapping_proven |= repair_state.partial_source_mapped
        if partial_sources and not source_mapping_proven:
            raise EngineArchiveError("par2_source_unmatched", retryable=False)
        repaired_volumes.append(
            (
                repaired["identity"],
                source_asset.relative_path,
                source_asset.declared_bytes,
            )
        )
        repaired_artifact = MaterializedArtifact(
            repaired["identity"],
            source_asset.declared_bytes,
            source_asset.asset_id.hex(),
            repaired["asset_revision"],
        )
        repair_state.volumes[file_id] = repaired_volumes[-1]
        repair_state.artifacts[file_id] = repaired_artifact
        if repaired["identity"] not in repair_source_identities:
            repair_sources.append(repaired_volumes[-1])
            repair_sources.sort(key=lambda source: source[0])
            repair_source_identities.add(repaired["identity"])
        repaired_file_ids.append(file_id)
    return (
        recovery_group,
        repaired_volumes,
        [repair_state.artifacts[file_id] for file_id in repaired_file_ids],
    )


def _par2_recovery_window(
    volumes: list[tuple[tuple[str, str, int], str]],
) -> list[tuple[tuple[str, str, int], str]]:
    newest = volumes[-1]
    selected = [newest]
    selected_bytes = newest[0][2]
    for candidate in sorted(
        volumes[:-1],
        key=lambda item: (item[0][2], item[0][1], item[0][0]),
        reverse=True,
    ):
        if len(selected) == MAX_PAR2_VOLUMES:
            break
        next_bytes = selected_bytes + candidate[0][2]
        if next_bytes > MAX_USENET_LOGICAL_BYTES:
            continue
        selected.append(candidate)
        selected_bytes = next_bytes
    return selected


def _maximum_par2_recovery_blocks(
    assets: tuple[UsenetAsset, ...],
    slice_size: int,
) -> int:
    packet_bytes = slice_size + 68
    return sum(asset.declared_bytes // packet_bytes for asset in assets)


async def _iter_par2_recovery(
    engine: EngineClient,
    manifest: object,
    par2_assets: tuple[UsenetAsset, ...],
    source_failure: EngineNntpError | EngineArchiveError,
    *,
    engine_servers: list[dict[str, object]],
    account_partition: bytes,
    provider_set_generation: str,
) -> AsyncIterator[_Par2RecoverySet]:
    if not par2_assets:
        raise source_failure
    ordered_assets = tuple(
        sorted(
            par2_assets,
            key=lambda asset: (
                asset.declared_bytes,
                asset.file_index,
                asset.asset_id,
            ),
        )
    )
    log.info(
        "usenet.par2_recovery.started",
        "Usenet PAR2 recovery started",
        item_count=len(ordered_assets),
    )
    volumes_by_set: dict[str, list[tuple[tuple[str, str, int], str]]] = {}
    yielded = False
    last_discovery_failure = None
    for par2_asset in ordered_assets:
        postings, group = _native_manifest_source(manifest, par2_asset)
        try:
            (
                identity,
                byte_size,
                _asset_revision,
            ) = await engine.materialize_nntp_postings(
                postings,
                group=group,
                servers=engine_servers,
                account_partition=account_partition,
                provider_set_generation=provider_set_generation,
            )
        except EngineNntpError as exc:
            if not exc.source_failure:
                raise
            continue
        volume = (identity, par2_asset.relative_path, byte_size)
        asset_id = par2_asset.asset_id.hex()
        try:
            discovered = await engine.discover_par2_sets([volume])
        except EngineArchiveError as exc:
            if exc.retryable:
                raise
            last_discovery_failure = exc
            log.warning(
                "usenet.par2_recovery.rejected",
                "Usenet PAR2 recovery volume rejected",
                failure_reason=exc.code,
                transferred_bytes=byte_size,
            )
            continue
        for discovered_set in discovered:
            set_id = discovered_set["set_id"]
            entries = volumes_by_set.setdefault(set_id, [])
            entry = (volume, asset_id)
            entries.append(entry)
            window = _par2_recovery_window(entries)
            if len(window) == 1:
                catalog = discovered_set
            else:
                merged = await engine.discover_par2_sets(
                    [candidate[0] for candidate in window]
                )
                try:
                    catalog = next(
                        candidate
                        for candidate in merged
                        if candidate["set_id"] == set_id
                    )
                except StopIteration:
                    raise ValueError("native PAR2 recovery set disappeared") from None
            entries_by_identity = {candidate[0][0]: candidate for candidate in window}
            selected = tuple(
                entries_by_identity[identity]
                for identity in catalog["volume_content_identities"]
            )
            recovery = _Par2RecoverySet(
                volumes=tuple(candidate[0] for candidate in selected),
                catalog=catalog,
                asset_ids=tuple(candidate[1] for candidate in selected),
            )
            yielded = True
            log.info(
                "usenet.par2_recovery.selected",
                "Usenet PAR2 recovery selected",
                item_count=len(recovery.volumes),
                transferred_bytes=sum(volume[2] for volume in recovery.volumes),
                result_count=len(catalog["recovery_exponents"]),
            )
            yield recovery
    if not yielded:
        if last_discovery_failure is not None:
            raise last_discovery_failure
        raise source_failure


def _native_manifest_source(
    manifest: object, asset: UsenetAsset
) -> tuple[list[tuple[int, int, str]], str | None]:
    manifest_file = manifest[asset.file_index]
    group = next(iter(manifest_file["groups"]), None)
    return (
        [
            (posting["number"], posting["bytes"], posting["message_id"])
            for posting in manifest_file["postings"]
        ],
        group,
    )


async def _open_session_archive_source(
    engine: EngineClient,
    manifest: object,
    archive_group,
    selection_intent,
    *,
    engine_servers: list[dict[str, object]],
    account_partition: bytes,
    provider_set_generation: str,
) -> tuple[dict[str, object], str, int, str, UsenetAsset]:
    runtime_stats = await engine.stats()
    concurrency = min(
        sum(server["connections"] for server in engine_servers),
        runtime_stats["request_workers"] - runtime_stats["requests_active"] + 1,
        runtime_stats["nntp_preparation_slots"],
    )
    gate = asyncio.Semaphore(concurrency)

    async def open_volume(asset):
        postings, group = _native_manifest_source(manifest, asset)
        async with gate:
            identity, size, revision, _strong_revision = await engine.open_nntp_session(
                postings,
                group=group,
                servers=engine_servers,
                account_partition=account_partition,
                provider_set_generation=provider_set_generation,
                preparation=True,
            )
        return identity, revision, asset.relative_path, size

    volumes = list(
        await _gather_or_cancel(
            *(open_volume(asset) for asset in archive_group.volumes)
        )
    )
    plan, members = await engine.catalog_session_archive_volumes(volumes)
    assets = catalog_archive_members(plan["set_identity"], members)
    if not assets:
        raise EngineArchiveError("archive_direct_unsupported", retryable=False)
    selected = await asyncio.to_thread(select_asset, assets, selection_intent)
    _, identity, exact_size, revision = await engine.open_session_archive_member(
        volumes,
        selected.declared_bytes,
        selected.relative_path,
    )
    await engine.inspect_raw_composite(identity, exact_size)
    return plan, identity, exact_size, revision, selected


async def _finish_native_usenet_preparation(
    prepared: PreparedPlaybackIntent,
    database,
    *,
    owner_configuration_partition: bytes,
    selection_hint: tuple[str, int] | None,
    target_ref: dict[str, object],
    identity: str,
    byte_size: int,
    revision: str,
    materialized_artifacts: list[MaterializedArtifact],
) -> tuple[str, int, str]:
    if (
        selection_hint is not None
        and target_ref["relative_path"] == selection_hint[0]
        and byte_size != selection_hint[1]
    ):
        raise FileSelectionError("file_selection_size_mismatch")
    repository = PlaybackPreparationRepository(database)
    if prepared.preparation.state == "ready" and not _reconcile_native_representation(
        prepared.preparation.target_ref,
        target_ref,
    ):
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="native_representation_changed",
        )
        raise ValueError("native media representation changed")
    if materialized_artifacts:
        await MaterializedArtifactRepository(
            settings.USENET_ARTIFACT_DIR,
            database,
        ).retain_for_preparation(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            artifact_sha256s=tuple(
                artifact.artifact_sha256 for artifact in materialized_artifacts
            ),
        )
    await repository.mark_ready(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="native",
        target_ref=target_ref,
    )
    return identity, byte_size, revision


async def prepare_altmount(
    prepared: PreparedPlaybackIntent,
    broker,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    """Prepare one artifact behind AltMount's durable idempotent mutation ledger."""
    provider = prepared.resolution.provider
    provider_options = prepared.resolution.provider_options
    artifact_locator = _brokered_artifact_locator(prepared)
    if artifact_locator is None:
        raise ValueError("AltMount requires a brokered NZB artifact")
    artifact_sha256 = artifact_locator["payload"]["artifact_sha256"]
    owned = await broker.resolve_owned_artifact(
        artifact_sha256,
        owner_configuration_partition=owner_configuration_partition,
    )
    category = provider.category_for(provider_options)
    deadline_seconds = provider.preparation_deadline()
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    endpoint, credential_material = provider.credential_binding(provider_options)
    fingerprint = codec.provider_credential_fingerprint(
        "altmount",
        endpoint,
        credential_material,
    )
    selection_json = provider_selection_json(prepared.preparation.selection_intent)
    mutation_key = codec.provider_mutation_key(
        partition=owner_configuration_partition,
        provider_configuration_id=prepared.preparation.provider_configuration_id,
        credential_fingerprint=fingerprint,
        source_discriminator=owned.grant_id,
        selection_json=selection_json,
        operation=f"altmount-native:{category}",
        contract_version="v1",
    )
    provider_repository = ProviderPreparationRepository(database)
    ledger_arguments = {
        "owner_configuration_partition": owner_configuration_partition,
        "provider_configuration_id": prepared.preparation.provider_configuration_id,
        "credential_fingerprint": fingerprint,
        "candidate_id": prepared.preparation.candidate_id,
        "locator_id": artifact_locator["locator_id"],
        "artifact_grant_id": owned.grant_id,
        "selection_json": selection_json,
        "mutation_idempotency_key": mutation_key,
        "provider_kind": "altmount",
    }
    ledger = await provider_repository.get_existing(**ledger_arguments)
    created = False
    if ledger is None:
        ledger, created = await provider_repository.get_or_create(**ledger_arguments)
    playback_repository = PlaybackPreparationRepository(database)
    if ledger.state == "terminal":
        if ledger.payload["status"] == "selected":
            target = _altmount_selected_target(ledger.payload)
            await playback_repository.mark_ready(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                target_kind="cloud",
                target_ref={
                    "provider_preparation_id": ledger.preparation_id,
                    **target,
                },
            )
            return "ready"
        await playback_repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code=ledger.payload["error_code"],
        )
        return "failed"
    attempt = "initial"
    if not created:
        attempt = await provider_repository.claim_altmount_retry(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            deadline_seconds=deadline_seconds,
        )
        if attempt is None:
            return "pending"
    try:
        result = await provider.submit_artifact(
            provider_options,
            await broker.read_owned_artifact(
                artifact_sha256,
                owner_configuration_partition=owner_configuration_partition,
            ),
            artifact_sha256,
            prepared.preparation.selection_intent,
        )
        selected = provider.select_file(
            result,
            prepared.preparation.selection_intent,
        )
    except AltMountError as exc:
        if exc.terminal_status is not None:
            await provider_repository.record_altmount_failure(
                ledger.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                status=exc.terminal_status,
                error_code=exc.code,
            )
            await playback_repository.mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                code=exc.code,
            )
            if exc.auth_failed:
                raise
            return "failed"
        if exc.retryable:
            if attempt == "final":
                await provider_repository.record_altmount_failure(
                    ledger.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                    status="failed",
                    error_code=exc.code,
                )
                await playback_repository.mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code=exc.code,
                )
                return "failed"
            return "pending"
        raise
    await provider_repository.record_altmount_selection(
        ledger.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        virtual_path=selected.virtual_path,
    )
    await playback_repository.mark_ready(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "provider_preparation_id": ledger.preparation_id,
            "virtual_path": selected.virtual_path,
        },
    )
    return "ready"


def _altmount_selected_target(payload: dict) -> dict:
    return {"virtual_path": payload["virtual_path"]}


async def prepare_easynews(
    prepared: PreparedPlaybackIntent,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    """Seal one account-bound Easynews locator for direct playback."""
    source = _easynews_source(prepared)
    if source is None:
        raise ValueError("Easynews media is unavailable")
    locator = source["payload"]
    if (
        locator["account_configuration_id"]
        != prepared.preparation.provider_configuration_id
    ):
        raise ValueError("Easynews media is unavailable")
    await PlaybackPreparationRepository(database).mark_ready(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "file_identifier": locator["file_identifier"],
        },
    )
    return "ready"


async def cleanup_torbox_usenet(
    provider,
    database,
    *,
    owner_configuration_partition: bytes,
    provider_configuration_id: str,
    credential_fingerprint: str,
) -> bool:
    """Best-effort cleanup of one expired job under sealed ownership."""
    repository = ProviderPreparationRepository(database)
    target = await repository.claim_torbox_cleanup(
        owner_configuration_partition=owner_configuration_partition,
        provider_configuration_id=provider_configuration_id,
        credential_fingerprint=credential_fingerprint,
    )
    if target is None:
        return False
    try:
        await provider.delete_owned(target.usenet_id)
    except TorBoxUsenetError:
        return False
    await repository.record_torbox_cleanup_complete(
        target.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        usenet_id=target.usenet_id,
    )
    return True


async def prepare_torbox_usenet(
    prepared: PreparedPlaybackIntent,
    broker,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    """Create one TorBox-owned item from its brokered NZB source."""
    locator = _brokered_artifact_locator(prepared)
    if locator is None:
        raise ValueError("TorBox requires a brokered NZB artifact")
    payload = locator["payload"]
    locator_id = locator["locator_id"]
    provider = prepared.resolution.provider
    artifact_sha256 = payload["artifact_sha256"]
    artifact = await broker.resolve_owned_artifact(
        artifact_sha256,
        owner_configuration_partition=owner_configuration_partition,
    )
    artifact_grant_id = artifact.grant_id
    cache_hashes = cache_hashes_from_manifest(artifact.manifest)
    source_discriminator = artifact_grant_id
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    endpoint, credential_material = provider.credential_binding()
    fingerprint = codec.provider_credential_fingerprint(
        "torbox_usenet",
        endpoint,
        credential_material,
    )
    account_scope = bytes.fromhex(fingerprint)
    governor = ProviderGovernor(database)
    selection_json = provider_selection_json(prepared.preparation.selection_intent)
    mutation_key = codec.provider_mutation_key(
        partition=owner_configuration_partition,
        provider_configuration_id=prepared.preparation.provider_configuration_id,
        credential_fingerprint=fingerprint,
        source_discriminator=source_discriminator,
        selection_json=selection_json,
        operation="torbox-usenet-create",
        contract_version="v2",
    )
    provider_repository = ProviderPreparationRepository(database)
    ledger_arguments = {
        "owner_configuration_partition": owner_configuration_partition,
        "provider_configuration_id": prepared.preparation.provider_configuration_id,
        "credential_fingerprint": fingerprint,
        "candidate_id": prepared.preparation.candidate_id,
        "locator_id": locator_id,
        "artifact_grant_id": artifact_grant_id,
        "selection_json": selection_json,
        "mutation_idempotency_key": mutation_key,
        "provider_kind": "torbox_usenet",
    }
    ledger = await provider_repository.get_existing(**ledger_arguments)
    ledger_created = False
    existing_item = None
    release_name = prepared.resolution.release.title
    if ledger is None or ledger.state == "mutation_pending":
        await cleanup_torbox_usenet(
            provider,
            database,
            owner_configuration_partition=owner_configuration_partition,
            provider_configuration_id=prepared.preparation.provider_configuration_id,
            credential_fingerprint=fingerprint,
        )
        if cache_hashes:
            existing_item = await provider.find_existing(cache_hashes)
        if ledger is None:
            ledger, ledger_created = await provider_repository.get_or_create(
                **ledger_arguments
            )
    repository = PlaybackPreparationRepository(database)
    if ledger.state == "terminal":
        ledger_status = ledger.payload["status"]
        if ledger_status == "selected":
            target = _torbox_selected_target(ledger.payload, artifact_sha256)
            await repository.mark_ready(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                target_kind="cloud",
                target_ref={
                    "provider_preparation_id": ledger.preparation_id,
                    **target,
                },
            )
            return "ready"
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code=(
                "ambiguous_submission"
                if ledger_status == "ambiguous_submission"
                else "remote_failed"
            ),
        )
        return "failed"
    if ledger_created or ledger.state == "mutation_pending":
        if existing_item is not None:
            item = existing_item
            ownership = "adopted" if ledger_created else "unknown"
        elif not ledger_created:
            if time.time() - ledger.created_at < _TORBOX_RECONCILIATION_SECONDS:
                return "pending"
            await provider_repository.record_ambiguous_submission(
                ledger.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_kind="torbox_usenet",
            )
            await repository.mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                code="ambiguous_submission",
            )
            return "failed"
        else:
            item = await provider.submit_artifact(
                await broker.read_owned_artifact(
                    artifact_sha256,
                    owner_configuration_partition=owner_configuration_partition,
                ),
                name=release_name,
                governor=governor,
                governor_scope=account_scope,
            )
            ownership = "created"
        remote_hash = artifact_sha256
        remote_usenet_id = item.usenet_id
        item_status = item.status
        item_readiness = provider.status(item).readiness
        await provider_repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            remote_id=str(remote_usenet_id),
            remote_hash=remote_hash,
            status=item_status,
            ownership=ownership,
        )
        if item_readiness is Readiness.TERMINAL_FAILURE:
            await provider_repository.record_terminal_status(
                ledger.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                status=item_status,
            )
            await repository.mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                code="remote_failed",
            )
            return "failed"
    else:
        if ledger.payload["remote_hash"] != artifact_sha256:
            raise ValueError("TorBox preparation is corrupt")
        remote_usenet_id = int(ledger.payload["remote_id"])
    await repository.record_pending_target(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "provider_preparation_id": ledger.preparation_id,
            "usenet_id": remote_usenet_id,
        },
    )
    return "pending"


async def poll_torbox_usenet(
    prepared: PreparedPlaybackIntent,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    target = prepared.preparation.target_ref
    usenet_id = target["usenet_id"]
    ledger_id = target["provider_preparation_id"]
    item = await prepared.resolution.provider.get_item(usenet_id)
    status = prepared.resolution.provider.status(item)
    repository = PlaybackPreparationRepository(database)
    provider_repository = ProviderPreparationRepository(database)
    ledger = await provider_repository.record_poll(
        ledger_id,
        owner_configuration_partition=owner_configuration_partition,
    )
    if ledger is None:
        raise ValueError("TorBox item is unavailable")
    if status.readiness is Readiness.TERMINAL_FAILURE:
        await provider_repository.record_terminal_status(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
            status=item.status,
        )
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="remote_failed",
        )
        return "failed"
    if status.readiness is not Readiness.READY:
        return "pending"
    try:
        selected = prepared.resolution.provider.select_file(
            item, prepared.preparation.selection_intent
        )
    except TorBoxUsenetError as exc:
        if not exc.terminal:
            raise
        await provider_repository.record_terminal_status(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
            status="invalid",
        )
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="file_selection_ambiguous",
        )
        return "failed"
    await provider_repository.record_selected_file(
        ledger_id,
        owner_configuration_partition=owner_configuration_partition,
        remote_id=str(item.usenet_id),
        remote_hash=ledger.payload["remote_hash"],
        file_index=selected.file_id,
        file_size=selected.size,
        locked_link=None,
    )
    await repository.mark_ready(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "provider_preparation_id": ledger_id,
            "usenet_id": item.usenet_id,
            "file_id": selected.file_id,
            "byte_size": selected.size,
        },
    )
    return "ready"


def _torbox_selected_target(payload: dict, artifact_sha256: str) -> dict:
    if payload["remote_hash"] != artifact_sha256:
        raise ValueError("TorBox selected file is corrupt")
    return {
        "usenet_id": int(payload["remote_id"]),
        "file_id": payload["file_index"],
        "byte_size": payload["file_size"],
    }


async def _torbox_download_url(prepared: PreparedPlaybackIntent) -> str:
    target = prepared.preparation.target_ref
    download = await prepared.resolution.provider.request_download(
        target["usenet_id"],
        file_id=target["file_id"],
    )
    return download.url


async def prepare_stremthru_newz(
    prepared: PreparedPlaybackIntent,
    broker,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    """Commit a stable export before submitting the exact NZB to StremThru."""
    artifact_locator = _brokered_artifact_locator(prepared)
    if artifact_locator is None:
        raise ValueError("StremThru requires a brokered NZB artifact")
    artifact_sha256 = artifact_locator["payload"]["artifact_sha256"]
    owned = await broker.resolve_owned_artifact(
        artifact_sha256, owner_configuration_partition=owner_configuration_partition
    )
    codec = CapabilityCodec(settings.COMET_CAPABILITY_SECRET)
    provider = prepared.resolution.provider
    endpoint, credential_material = provider.credential_binding()
    fingerprint = codec.provider_credential_fingerprint(
        "stremthru_newz", endpoint, credential_material
    )
    selection_json = provider_selection_json(prepared.preparation.selection_intent)
    mutation_key = codec.provider_mutation_key(
        partition=owner_configuration_partition,
        provider_configuration_id=prepared.preparation.provider_configuration_id,
        credential_fingerprint=fingerprint,
        source_discriminator=owned.grant_id,
        selection_json=selection_json,
        operation="newz-submit",
        contract_version="v2",
    )
    provider_repository = ProviderPreparationRepository(database)
    ledger_arguments = {
        "owner_configuration_partition": owner_configuration_partition,
        "provider_configuration_id": prepared.preparation.provider_configuration_id,
        "credential_fingerprint": fingerprint,
        "candidate_id": prepared.preparation.candidate_id,
        "locator_id": artifact_locator["locator_id"],
        "artifact_grant_id": owned.grant_id,
        "selection_json": selection_json,
        "mutation_idempotency_key": mutation_key,
    }
    ledger = await provider_repository.get_existing(**ledger_arguments)
    created = False
    export_url = None
    if ledger is None:
        export = await NzbProviderExportRepository(database).get_or_create(
            owner_configuration_partition=owner_configuration_partition,
            grant_id=owned.grant_id,
            provider_configuration_id=prepared.preparation.provider_configuration_id,
            credential_fingerprint=fingerprint,
        )
        export_url = f"{export_base_url()}/nzb/export/v1/{export}.nzb"
        ledger, created = await provider_repository.get_or_create(**ledger_arguments)
    if not created and ledger.state == "mutation_pending":
        await provider_repository.record_ambiguous_submission(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_kind="stremthru_newz",
        )
        await PlaybackPreparationRepository(database).mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="ambiguous_submission",
        )
        return "failed"
    if ledger.state == "terminal":
        ledger_status = ledger.payload["status"]
        if ledger_status == "selected":
            target = _stremthru_selected_target(ledger.payload)
            await PlaybackPreparationRepository(database).mark_ready(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                target_kind="cloud",
                target_ref={
                    "provider_preparation_id": ledger.preparation_id,
                    **target,
                },
            )
            return "ready"
        failure_code = (
            "file_selection_ambiguous"
            if ledger_status == "file_selection_ambiguous"
            else "ambiguous_submission"
            if ledger_status == "ambiguous_submission"
            else "remote_failed"
        )
        await PlaybackPreparationRepository(database).mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code=failure_code,
        )
        return "failed"
    if created:
        try:
            submission = await provider.submit_export(export_url)
        except StremThruNewzError as exc:
            if exc.mutation_rejected:
                await provider_repository.discard_rejected_stremthru_submission(
                    ledger.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                )
            raise
        await provider_repository.record_submission(
            ledger.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            remote_id=submission.remote_id,
            remote_hash=submission.remote_hash,
            status=submission.status,
            ownership="created",
        )
        remote_id, remote_hash, status = (
            submission.remote_id,
            submission.remote_hash,
            submission.status,
        )
    else:
        status = ledger.payload["status"]
        if status == "readd_mutation_pending":
            await PlaybackPreparationRepository(database).mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                code="ambiguous_submission",
            )
            return "failed"
        if status == "remote_missing":
            export = await NzbProviderExportRepository(database).get_or_create(
                owner_configuration_partition=owner_configuration_partition,
                grant_id=owned.grant_id,
                provider_configuration_id=(
                    prepared.preparation.provider_configuration_id
                ),
                credential_fingerprint=fingerprint,
            )
            export_url = f"{export_base_url()}/nzb/export/v1/{export}.nzb"
            if not await provider_repository.begin_stremthru_resubmission(
                ledger.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
            ):
                await PlaybackPreparationRepository(database).mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code="ambiguous_submission",
                )
                return "failed"
            try:
                submission = await provider.submit_export(export_url)
            except StremThruNewzError as exc:
                if exc.mutation_rejected:
                    await provider_repository.restore_rejected_stremthru_resubmission(
                        ledger.preparation_id,
                        owner_configuration_partition=owner_configuration_partition,
                    )
                raise
            await provider_repository.record_stremthru_resubmission(
                ledger.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                remote_id=submission.remote_id,
                remote_hash=submission.remote_hash,
                status=submission.status,
            )
            remote_id = submission.remote_id
            remote_hash = submission.remote_hash
            status = submission.status
        else:
            remote_id = ledger.payload["remote_id"]
            remote_hash = ledger.payload["remote_hash"]
    await PlaybackPreparationRepository(database).record_pending_target(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "provider_preparation_id": ledger.preparation_id,
            "remote_id": remote_id,
            "remote_hash": remote_hash,
        },
    )
    return "pending"


async def poll_stremthru_newz(
    prepared: PreparedPlaybackIntent,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    target = prepared.preparation.target_ref
    remote_id, remote_hash, ledger_id = (
        target["remote_id"],
        target["remote_hash"],
        target["provider_preparation_id"],
    )
    provider_repository = ProviderPreparationRepository(database)
    try:
        item = await prepared.resolution.provider.get_item(remote_id, remote_hash)
    except StremThruNewzError as exc:
        if not exc.remote_missing:
            raise
        retry = await provider_repository.record_stremthru_missing(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
        )
        repository = PlaybackPreparationRepository(database)
        if retry:
            await repository.clear_pending_target(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
            )
            return "reprepare"
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="remote_item_missing",
        )
        return "failed"
    await provider_repository.record_poll(
        ledger_id, owner_configuration_partition=owner_configuration_partition
    )
    repository = PlaybackPreparationRepository(database)
    if item.terminal:
        await provider_repository.record_terminal_status(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
            status="failed",
        )
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="remote_failed",
        )
        return "failed"
    if not item.files:
        return "pending"
    try:
        selected = prepared.resolution.provider.select_file(
            item, prepared.preparation.selection_intent
        )
    except StremThruNewzError as exc:
        if not exc.terminal:
            raise
        await provider_repository.record_terminal_status(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
            status="file_selection_ambiguous",
        )
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="file_selection_ambiguous",
        )
        return "failed"
    await provider_repository.record_selected_file(
        ledger_id,
        owner_configuration_partition=owner_configuration_partition,
        remote_id=remote_id,
        remote_hash=remote_hash,
        file_index=selected.index,
        file_size=selected.size,
        locked_link=selected.locked_link,
    )
    await repository.mark_ready(
        prepared.preparation.preparation_id,
        owner_configuration_partition=owner_configuration_partition,
        provider_account_partition=prepared.resolution.account_partition,
        target_kind="cloud",
        target_ref={
            "provider_preparation_id": ledger_id,
            "byte_size": selected.size,
            "locked_link": selected.locked_link,
        },
    )
    return "ready"


def _stremthru_selected_target(payload: dict) -> dict:
    return {
        "byte_size": payload["file_size"],
        "locked_link": payload["locked_link"],
    }


async def _stremthru_download_url(
    prepared: PreparedPlaybackIntent,
) -> str:
    generated = await prepared.resolution.provider.generate_link(
        prepared.preparation.target_ref["locked_link"]
    )
    return generated.url


def _brokered_artifact(prepared: PreparedPlaybackIntent) -> dict | None:
    return next(
        (
            locator["payload"]
            for locator in prepared.resolution.release.locators
            if locator["kind"] == "nzb_artifact"
        ),
        None,
    )


def artifact_selection_hint(
    artifact_payload: Mapping[str, object],
) -> tuple[str, int] | None:
    name = artifact_payload.get("selection_hint_name")
    if name is None:
        return None
    return name, artifact_payload["selection_hint_size"]


def _brokered_artifact_locator(prepared: PreparedPlaybackIntent) -> dict | None:
    return next(
        (
            locator
            for locator in prepared.resolution.release.locators
            if locator["kind"] == "nzb_artifact"
        ),
        None,
    )


def _release_with_brokered_artifact(
    release: ResolvedPlaybackIntent,
    locator: dict,
    *,
    provider_configuration_id: str,
    provider_kind: str,
    owner_configuration_partition: bytes,
) -> ResolvedPlaybackIntent:
    transformed = ResolvedPlaybackIntent(
        release.candidate_id,
        release.transport,
        release.title,
        release.byte_size,
        (locator,),
        release.media_id,
    )
    RenderedReleaseRepository.authorize_intent(
        transformed,
        provider_configuration_id=provider_configuration_id,
        provider_kind=provider_kind,
        owner_configuration_partition=owner_configuration_partition,
    )
    return transformed


def _easynews_source(prepared: PreparedPlaybackIntent) -> dict | None:
    locators = [
        locator
        for locator in prepared.resolution.release.locators
        if locator["kind"] == "easynews_http"
    ]
    return locators[0] if len(locators) == 1 else None


async def poll_nzbdav(
    prepared: PreparedPlaybackIntent,
    database,
    *,
    owner_configuration_partition: bytes,
) -> str:
    """Poll only the NzbDAV job sealed to this preparation."""
    target = prepared.preparation.target_ref
    job_id = target["remote_job_id"]
    ledger_id = target["provider_preparation_id"]
    category = target["category"]
    artifact_sha256 = target["artifact_sha256"]
    job = await prepared.resolution.provider.poll_artifact(
        prepared.resolution.provider_options,
        job_id,
        artifact_sha256,
        category,
    )
    repository = PlaybackPreparationRepository(database)
    provider_repository = ProviderPreparationRepository(database)
    await provider_repository.record_poll(
        ledger_id,
        owner_configuration_partition=owner_configuration_partition,
    )
    if job.status.code == "remote_item_missing":
        absence = await provider_repository.record_sab_absence(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
        )
        if absence == "pending":
            return "pending"
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code="remote_item_missing",
        )
        return "failed"
    if job.observed:
        present = await provider_repository.clear_sab_absence(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
        )
        if not present:
            await repository.mark_failed(
                prepared.preparation.preparation_id,
                owner_configuration_partition=owner_configuration_partition,
                provider_account_partition=prepared.resolution.account_partition,
                code="remote_item_missing",
            )
            return "failed"
    if job.status.readiness is Readiness.TERMINAL_FAILURE:
        terminal_status = (
            "credentials_rejected"
            if job.status.code == "nzbdav_credentials_rejected"
            else "invalid"
            if job.status.code in {"category_mismatch", "job_mismatch"}
            else "failed"
        )
        await provider_repository.record_terminal_status(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
            status=terminal_status,
        )
        await repository.mark_failed(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            code=job.status.code or "remote_failed",
        )
        if job.status.code == "nzbdav_credentials_rejected":
            raise NzbDavError(
                "nzbdav_credentials_rejected",
                auth_failed=True,
                terminal=True,
            )
        return "failed"
    if job.verified_name is not None:
        try:
            selected = await prepared.resolution.provider.completed_file(
                prepared.resolution.provider_options,
                job.verified_name,
                category,
                prepared.preparation.selection_intent,
            )
        except NzbDavError as exc:
            if exc.auth_failed:
                await provider_repository.record_terminal_status(
                    ledger_id,
                    owner_configuration_partition=owner_configuration_partition,
                    status="credentials_rejected",
                )
                await repository.mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code=exc.code,
                )
                raise
            if exc.terminal:
                await provider_repository.record_terminal_status(
                    ledger_id,
                    owner_configuration_partition=owner_configuration_partition,
                    status="invalid",
                )
                await repository.mark_failed(
                    prepared.preparation.preparation_id,
                    owner_configuration_partition=owner_configuration_partition,
                    provider_account_partition=prepared.resolution.account_partition,
                    code="file_selection_ambiguous",
                )
                return "failed"
            raise
        await provider_repository.record_selected_file(
            ledger_id,
            owner_configuration_partition=owner_configuration_partition,
            remote_id=job_id,
            remote_hash=artifact_sha256,
            file_index=0,
            file_size=selected.byte_size,
            locked_link=selected.relative_path,
        )
        await repository.mark_ready(
            prepared.preparation.preparation_id,
            owner_configuration_partition=owner_configuration_partition,
            provider_account_partition=prepared.resolution.account_partition,
            target_kind="cloud",
            target_ref={
                "provider_preparation_id": ledger_id,
                "verified_name": job.verified_name,
                "relative_path": selected.relative_path,
                "byte_size": selected.byte_size,
                "category": category,
            },
        )
        return "ready"
    return "pending"


def _nzbdav_selected_target(payload: dict) -> dict:
    return {
        "relative_path": payload["locked_link"],
        "byte_size": payload["file_size"],
    }


async def _nzbdav_download_url(prepared: PreparedPlaybackIntent) -> str:
    """Resolve an authenticated WebDAV URL from a ready sealed record."""
    target = prepared.preparation.target_ref
    return prepared.resolution.provider.direct_download_url(
        prepared.resolution.provider_options,
        target["verified_name"],
        target["category"],
        target["relative_path"],
    )


async def _altmount_download_url(prepared: PreparedPlaybackIntent) -> str:
    """Rebuild one client-consumable AltMount URL from a validated path."""
    return prepared.resolution.provider.stream_url(
        prepared.resolution.provider_options,
        prepared.preparation.target_ref["virtual_path"],
    )


async def _easynews_download_url(
    prepared: PreparedPlaybackIntent,
) -> str:
    """Build one authenticated Easynews download URL without fetching it."""
    preparation = prepared.preparation
    source = _easynews_source(prepared)
    locator = source["payload"] if source is not None else None
    if (
        locator is None
        or preparation.target_ref["file_identifier"] != locator["file_identifier"]
        or locator["account_configuration_id"] != preparation.provider_configuration_id
    ):
        raise ValueError("Easynews media is unavailable")
    return prepared.resolution.provider.playback_url(locator)


_REMOTE_DOWNLOAD_RESOLVERS = {
    "altmount": _altmount_download_url,
    "easynews": _easynews_download_url,
    "nzbdav": _nzbdav_download_url,
    "stremthru_newz": _stremthru_download_url,
    "torbox_usenet": _torbox_download_url,
}


async def remote_download_url(prepared: PreparedPlaybackIntent) -> str:
    """Resolve a ready remote Usenet preparation to its direct media URL."""
    try:
        resolver = _REMOTE_DOWNLOAD_RESOLVERS[prepared.preparation.provider_kind]
    except KeyError:
        raise ValueError("remote Usenet media is unavailable") from None
    return await resolver(prepared)
