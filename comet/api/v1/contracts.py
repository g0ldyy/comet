"""Strict request, response, and envelope contracts for API v1."""

from __future__ import annotations

from typing import Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from comet.core.models import ConfigModel
from comet.core.settings_catalog import SettingCatalogEntry

SettingValue = Any


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ApiMeta(StrictModel):
    request_id: str


class ApiSuccess[DataT](StrictModel):
    data: DataT
    meta: ApiMeta


class ApiErrorBody(StrictModel):
    code: str
    message: str
    request_id: str
    details: list[dict[str, Any]] | None = None


class ApiError(StrictModel):
    error: ApiErrorBody


class LoginRequest(StrictModel):
    password: str = Field(min_length=1, max_length=512)


class SessionData(StrictModel):
    authenticated: Literal[True] = True
    csrf_token: str
    expires_in: int


class ConfigureSessionData(StrictModel):
    protected: bool
    authenticated: bool
    csrf_token: str | None
    expires_in: int | None


class ConfiguratorCapabilities(StrictModel):
    proxy_debrid_stream: bool
    torrent_streams: bool
    usenet: bool
    native_usenet: bool
    stremio_api_prefix: str


class ConfiguratorBootstrapData(StrictModel):
    default_configuration: ConfigModel
    capabilities: ConfiguratorCapabilities
    resolutions: list[str]
    result_formats: list[str]
    languages: dict[str, str]
    debrid_services: list[str]
    native_usenet_sources: list[str]
    usenet_provider_kinds: list[str]
    usenet_source_kinds: list[str]


class ConfigValidationRequest(StrictModel):
    configuration: ConfigModel

    @field_validator("configuration", mode="before")
    @classmethod
    def validate_document_size(cls, value: Any) -> Any:
        try:
            encoded = orjson.dumps(value)
        except (TypeError, orjson.JSONEncodeError):
            raise ValueError("configuration must contain JSON values") from None
        if len(encoded) > 24 * 1024:
            raise ValueError("configuration exceeds the 24 KiB JSON limit")
        return value


class OperationalEventData(StrictModel):
    id: int
    created_at: float
    instance_id: str
    process_id: int
    role: str
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    category: str
    event: str
    message: str
    request_id: str | None
    run_id: str | None
    connection_id: str | None
    media_type: str | None
    provider_name: str | None
    outcome: str | None
    error_code: str | None
    details: dict[str, str | bool | int | float]


class OperationalEventPageData(StrictModel):
    items: list[OperationalEventData]
    next_cursor: int | None
    dropped_events: int


class MetricSampleData(StrictModel):
    name: str
    labels: dict[str, str]
    value: float


class CurrentMetricsData(StrictModel):
    collected_at: float
    samples: list[MetricSampleData]
    history_available: bool
    history_ranges: list[Literal["15m", "1h", "6h", "24h", "7d", "30d"]]


class MetricPointData(StrictModel):
    timestamp: float
    value: float | None


class MetricSeriesData(StrictModel):
    labels: dict[str, str]
    points: list[MetricPointData]


class MetricRangeData(StrictModel):
    metric: str
    range: Literal["15m", "1h", "6h", "24h", "7d", "30d"]
    step: int
    series: list[MetricSeriesData]


class SettingView(StrictModel):
    catalog: SettingCatalogEntry
    value: SettingValue
    active_value: SettingValue
    source: Literal[
        "dashboard",
        "environment",
        "default",
        "generated_shared",
    ]


class SettingsSnapshotData(StrictModel):
    stored_revision: int
    applied_revision: int
    pending_restart_keys: list[str]
    settings: list[SettingView]


class SettingsMutationRequest(StrictModel):
    updates: dict[str, SettingValue] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)

    @field_validator("updates")
    @classmethod
    def validate_updates(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 128:
            raise ValueError("at most 128 settings may be updated at once")
        try:
            encoded = orjson.dumps(value)
        except (TypeError, orjson.JSONEncodeError):
            raise ValueError("setting updates must contain JSON values") from None
        if len(encoded) > 256 * 1024:
            raise ValueError("setting updates are too large")
        return value

    @field_validator("reset")
    @classmethod
    def validate_reset(cls, value: list[str]) -> list[str]:
        if len(value) > 128 or len(set(value)) != len(value):
            raise ValueError("reset keys must be unique and bounded")
        return value

    @model_validator(mode="after")
    def validate_disjoint_changes(self):
        requested = {*self.updates, *self.reset}
        if any(
            not key
            or key != key.strip()
            or len(key.encode("utf-8")) > 128
            or not key.replace("_", "").isalnum()
            for key in requested
        ):
            raise ValueError("setting keys must be bounded identifiers")
        overlap = set(self.updates).intersection(self.reset)
        if overlap:
            raise ValueError("a setting cannot be updated and reset")
        if not self.updates and not self.reset:
            raise ValueError("at least one setting change is required")
        return self


class SettingsMutationData(StrictModel):
    revision: int
    changed_keys: list[str]
    live_applied_keys: list[str]
    component_reloaded_keys: list[str]
    restart_required_keys: list[str]
    restart_required: bool


class AuditEntry(StrictModel):
    id: str
    revision: int | None
    key: str
    action: str
    previous_source: str | None
    next_source: str | None
    changed_at: float
    changed_by: str


class AuditPageData(StrictModel):
    items: list[AuditEntry]
    next_cursor: str | None


class RuntimeProcessView(StrictModel):
    process_id: int
    role: str
    started_at: float
    last_heartbeat: float


class ReadinessView(StrictModel):
    state: Literal["ready", "degraded", "unavailable"]
    components: dict[str, str]


class RuntimeInstanceView(StrictModel):
    instance_id: str
    alias: str | None
    hostname: str
    started_at: float
    last_heartbeat: float
    commit_hash: str | None
    branch: str
    build_date: str | None
    applied_revision: int
    pending_restart_keys: list[str]
    readiness: ReadinessView
    restart_capable: bool
    processes: list[RuntimeProcessView]


class SystemSnapshotData(StrictModel):
    stored_revision: int
    applied_revision: int
    current_instance_id: str
    readiness: ReadinessView
    runtimes: list[RuntimeInstanceView]


class BuildInfoData(StrictModel):
    commit_hash: str | None
    branch: str
    build_date: str | None
    container_image: bool
    python_version: str
    python_implementation: str
    native_engine_enabled: bool
    native_engine_api_version: int | None


class DatabaseStateData(StrictModel):
    backend: Literal["sqlite", "postgresql"]
    schema_version: str
    schema_current: bool
    primary_connected: bool
    replicas_configured: int
    replicas_active: int
    replicas_unavailable: int


class StorageVolumeData(StrictModel):
    name: Literal["database", "usenet_artifacts", "usenet_local"]
    capacity_bytes: int
    used_bytes: int
    free_bytes: int
    configured_limit_bytes: int | None


class FeatureStateData(StrictModel):
    torrent_streams: bool
    debrid_stream_proxy: bool
    background_scraper: bool
    usenet: bool
    native_usenet: bool
    cometnet: bool
    prometheus: bool
    read_replicas: bool


class MaintenanceStateData(StrictModel):
    last_retention_at: float | None
    retention_enabled: bool


class SystemDetailsData(StrictModel):
    build: BuildInfoData
    database: DatabaseStateData
    storage: list[StorageVolumeData]
    features: FeatureStateData
    maintenance: MaintenanceStateData


class UpdateCheckData(StrictModel):
    has_update: bool
    latest_commit_hash: str | None
    latest_url: str | None
    checked_at: str
    error: str | None
    install_method: Literal["redeploy_container", "update_checkout"]


class MaintenanceResultData(StrictModel):
    completed_at: float


class RuntimeRestartData(StrictModel):
    instance_id: str
    accepted: Literal[True] = True


class ProxyConnectionView(StrictModel):
    id: str
    ip: str
    content: str
    service: str
    instance_id: str
    process_id: int
    started_at: float
    updated_at: float
    duration: float
    bytes_transferred: int
    current_speed: float
    average_speed: float
    peak_speed: float
    cancellation_pending: bool


class ProxyHistoryEntry(StrictModel):
    id: str
    ip: str
    content: str
    service: str
    instance_id: str
    process_id: int
    started_at: float
    finished_at: float
    duration: float
    bytes_transferred: int
    average_speed: float
    peak_speed: float
    outcome: str
    error_code: str | None


class StreamActivityBucket(StrictModel):
    started_at: float
    bytes_transferred: int
    completed: int
    failed: int
    interrupted: int
    active: int
    peak_active: int | None


class StreamActivityData(StrictModel):
    collected_at: float
    selection: Literal["auto", "15m", "1h", "6h", "24h", "7d"]
    activity_started_at: float | None
    window_started_at: float
    window_ended_at: float
    bucket_seconds: int
    buckets: list[StreamActivityBucket]


class ProxySummary(StrictModel):
    active_connections: int
    current_speed: float
    session_bytes: int
    all_time_bytes: int
    completed_7d: int
    failed_7d: int
    bytes_7d: int
    average_duration_7d: float


class ProxySnapshotData(StrictModel):
    collected_at: float
    enabled: bool
    summary: ProxySummary
    active: list[ProxyConnectionView]


class ProxyHistoryPageData(StrictModel):
    items: list[ProxyHistoryEntry]
    next_cursor: str | None


class CommandResultData(StrictModel):
    resource_id: str
    outcome: str


class ScraperRuntimeView(StrictModel):
    instance_id: str
    process_id: int
    state: Literal["running", "paused", "stopped"]
    draining: bool
    run_id: str | None
    started_at: float | None
    processed: int
    success: int
    failed: int
    torrents_found: int
    discovered_items: int
    errors: int
    last_heartbeat: float


class ScraperQueueSummary(StrictModel):
    items: int
    episodes: int
    ready: int
    running: int
    deferred: int
    failed: int
    dead: int
    oldest_ready_at: float | None
    low_watermark: int
    high_watermark: int
    hard_cap: int


class ScraperRunView(StrictModel):
    run_id: str
    started_at: float
    finished_at: float | None
    status: Literal["running", "completed", "cancelled", "failed"]
    processed: int
    success: int
    failed: int
    torrents_found: int
    duration_ms: int
    worker_count: int
    error_code: str | None


class ScrapingSnapshotData(StrictModel):
    collected_at: float
    runtimes: list[ScraperRuntimeView]
    queue: ScraperQueueSummary
    runs_24h: int
    processed_24h: int
    failed_24h: int
    torrents_found_24h: int
    latest_run: ScraperRunView | None


class ScraperQueueEntry(StrictModel):
    kind: Literal["item", "episode"]
    id: str
    parent_id: str | None
    media_type: Literal["movie", "series"]
    title: str
    year: int
    season: int | None
    episode: int | None
    priority_score: float
    status: str
    consecutive_failures: int
    last_scraped_at: float | None
    last_success_at: float | None
    last_failure_at: float | None
    next_retry_at: float | None
    total_torrents_found: int
    created_at: float | None
    updated_at: float | None


class ScraperQueuePageData(StrictModel):
    items: list[ScraperQueueEntry]
    next_cursor: str | None


class ScraperRunsPageData(StrictModel):
    items: list[ScraperRunView]
    next_cursor: str | None


class ScraperQueueMutationData(StrictModel):
    kind: Literal["item", "episode", "all"]
    resource_id: str
    action: Literal["retry", "defer", "abandon", "requeue_dead"]
    affected: int


class ScraperControlData(StrictModel):
    action: Literal["start", "stop", "pause", "resume", "drain", "cancel_drain"]
    owners: int
    outcome: Literal["succeeded", "rejected"]


class UsenetEngineStats(StrictModel):
    draining: bool
    requests_active: int
    request_workers: int
    request_body_reserved_bytes: int
    request_body_limit_bytes: int
    request_body_busy_rejections_total: int
    request_queue_busy_rejections_total: int
    sessions: int
    session_prefetches_active: int
    nntp_hedges_active: int
    nntp_hedges_started_total: int
    nntp_hedges_won_total: int
    nntp_salvage_holes_total: int
    nntp_salvage_bytes_total: int
    raw_composites: int
    provider_sets: int
    segment_cache_entries: int
    segment_cache_bytes: int
    disk_cache_mappings: int
    disk_cache_blobs: int
    disk_cache_bytes: int
    disk_cache_stats_available: bool
    spool_stats_available: bool
    spool_resident_bytes: int
    spool_reserved_bytes: int
    archive_jobs_active: int
    repair_jobs_active: int
    archive_busy_rejections_total: int
    repair_busy_rejections_total: int
    spool_rejections_total: int
    negative_cache_entries: int
    network_singleflight_active: int
    nntp_pools: int
    nntp_connections_open: int
    nntp_connections_active: int
    nntp_connections_idle: int
    nntp_queue_interactive: int
    nntp_queue_preparation: int
    nntp_queue_background: int
    nntp_preparation_slots: int
    nntp_reserved_commands: int
    nntp_reserved_encoded_bytes: int
    nntp_reserved_decoded_bytes: int
    nntp_scheduler_busy_rejections_total: int
    nntp_connections_poisoned: int
    nntp_circuits_auth_open: int
    nntp_circuits_transient_open: int
    nntp_circuits_half_open: int
    nntp_provider_attempts_total: int
    nntp_provider_suppliers_total: int
    nntp_provider_hits_total: int
    nntp_provider_missing_total: int
    nntp_provider_corrupt_total: int
    nntp_provider_failures_total: int
    nntp_provider_cancellations_total: int
    nntp_provider_failovers_total: int
    nntp_circuit_skips_total: int


class UsenetEngineRuntimeView(StrictModel):
    instance_id: str
    process_id: int
    healthy: bool
    mode: str
    collected_at: float
    stats: UsenetEngineStats | None


class UsenetOperationView(StrictModel):
    id: str
    instance_id: str
    process_id: int
    client_ip: str
    content_id: str
    title: str
    member_path: str
    source_kind: Literal["session", "raw_composite"]
    started_at: float
    updated_at: float
    duration: float
    total_bytes: int
    bytes_transferred: int
    cancellation_pending: bool


class UsenetOperationHistoryView(StrictModel):
    id: str
    instance_id: str
    process_id: int
    client_ip: str
    content_id: str
    title: str
    member_path: str
    source_kind: Literal["session", "raw_composite"]
    started_at: float
    finished_at: float
    duration: float
    total_bytes: int
    bytes_transferred: int
    outcome: Literal["completed", "cancelled", "failed"]
    error_code: str | None


class UsenetPreparationView(StrictModel):
    id: str
    provider_kind: str
    media_id: str
    title: str
    state: Literal["mutation_pending", "submitted"]
    created_at: float
    updated_at: float


class UsenetInventorySummary(StrictModel):
    artifacts: int
    nzb_bytes: int
    materialized_bytes: int
    active_readers: int
    eligible_for_prune: int


class UsenetHistorySummary(StrictModel):
    streams_7d: int
    failed_7d: int
    bytes_7d: int


class UsenetSnapshotData(StrictModel):
    collected_at: float
    enabled: bool
    runtimes: list[UsenetEngineRuntimeView]
    active: list[UsenetOperationView]
    preparations: list[UsenetPreparationView]
    inventory: UsenetInventorySummary
    history: UsenetHistorySummary


class UsenetHistoryPageData(StrictModel):
    items: list[UsenetOperationHistoryView]
    next_cursor: str | None


class UsenetArtifactView(StrictModel):
    artifact_sha256: str
    storage_kind: Literal["nzb", "materialized_asset"]
    publication_state: Literal["publishing", "published", "tombstoned"]
    byte_size: int
    logical_length: int | None
    refcount: int
    active_readers: int
    created_at: float
    last_used_at: float
    eligible_for_prune: bool


class UsenetArtifactPageData(StrictModel):
    items: list[UsenetArtifactView]
    next_cursor: str | None


class UsenetArtifactPruneData(StrictModel):
    artifact_sha256: str
    pruned: bool


class UsenetControlData(StrictModel):
    action: Literal["drain", "resume"]
    instance_id: str
    outcome: Literal["succeeded", "rejected"]


class CometNetNodeView(StrictModel):
    enabled: bool
    healthy: bool
    node_id: str | None
    mode: Literal["local", "relay", "disabled"]
    uptime_seconds: float
    contribution_mode: str | None
    connected_peers: int
    inbound_peers: int
    outbound_peers: int
    average_latency_ms: float
    bytes_sent: int
    bytes_received: int
    messages_sent: int
    messages_received: int
    torrents_sent: int
    torrents_received: int
    invalid_messages: int


class CometNetPeerView(StrictModel):
    node_id: str
    alias: str | None
    connected_at: float
    last_activity: float
    outbound: bool
    latency_ms: float
    reputation: float | None
    trust_level: str | None
    torrents_received: int
    invalid_contributions: int
    bytes_sent: int
    bytes_received: int


class CometNetPoolView(StrictModel):
    pool_id: str
    display_name: str
    description: str
    member_count: int
    version: int
    updated_at: float
    membership: bool
    subscribed: bool


class CometNetSnapshotData(StrictModel):
    collected_at: float
    node: CometNetNodeView
    peers: list[CometNetPeerView]
    pools: list[CometNetPoolView]
    events: list[OperationalEventData]


class CometNetPoolMemberView(StrictModel):
    public_key: str
    node_id: str
    role: Literal["creator", "admin", "member"]
    added_at: float
    contribution_count: int
    last_seen: float
    is_self: bool


class CometNetInviteView(StrictModel):
    invite_code: str
    created_at: float
    expires_at: float | None
    max_uses: int | None
    uses: int
    node_url: str


class CometNetPoolDetailData(StrictModel):
    pool_id: str
    display_name: str
    description: str
    creator_key: str
    join_mode: Literal["invite"]
    version: int
    created_at: float
    updated_at: float
    is_admin: bool
    is_member: bool
    subscribed: bool
    members: list[CometNetPoolMemberView]
    invites: list[CometNetInviteView]


class CometNetMutationData(StrictModel):
    resource_id: str
    action: str


class CometNetInviteData(StrictModel):
    pool_id: str
    invite_link: str
