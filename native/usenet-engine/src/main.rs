use serde::Deserialize;
use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

use comet_usenet_engine::{nntp_protocol, nzb, observability, yenc};

#[cfg(not(test))]
#[global_allocator]
static GLOBAL_ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[cfg(not(test))]
unsafe extern "C" {
    fn mi_collect(force: bool);
}

thread_local! {
    static ALLOCATOR_COLLECT_NEEDED: Cell<bool> = const { Cell::new(false) };
}

#[cfg(all(test, feature = "quality-gates"))]
mod quality_benchmarks;
#[cfg(all(test, feature = "quality-gates"))]
mod quality_stress;

pub mod archive;
pub mod archive_group;
mod archive_nested;
mod archive_runtime;
mod cache;
mod child_process;
pub mod inspect;
mod limits;
mod materialization;
mod nntp;
pub mod par2;
mod par2_tool;
mod provider;
mod rar_stored;
mod raw_composite;
mod reader_lease;
mod resources;
mod session;

const API_VERSION: &str = "1";
const MAX_NZB_BYTES: usize = limits::MAX_NZB_METADATA_BYTES;
const MAX_ARTICLE_BYTES: usize = limits::MAX_DECLARED_POSTING_BYTES as usize;
const MAX_CONTROL_REQUEST_BYTES: usize = 1024 * 1024;
const MAX_PROVIDER_SET_REQUEST_BYTES: usize = 2 * 1024 * 1024;
const MAX_NATIVE_CATALOG_BYTES: usize = 2 * 1024 * 1024;
const MAX_NZB_METADATA_BYTES: usize = limits::MAX_NZB_METADATA_BYTES;
const MAX_REQUEST_HEADER_BYTES: usize = 16 * 1024;
const ENGINE_REQUEST_WORKERS: usize = 32;
const ENGINE_REQUEST_QUEUE: usize = 64;
const ENGINE_INTERACTIVE_WORKER_RESERVE: usize = 2;
const ENGINE_REQUEST_HEADER_TIMEOUT: Duration = Duration::from_secs(5);
const ENGINE_REQUEST_BODY_TIMEOUT: Duration = Duration::from_secs(35);
const ENGINE_RESPONSE_WRITE_TIMEOUT: Duration = Duration::from_secs(35);
const ENGINE_BACKGROUND_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(test)]
const PROVIDER_TEST_WORKERS: usize = 4;
const MAX_CONCURRENT_PREFETCH_SESSIONS: usize = 8;
const MAX_ACTIVE_HEDGES: usize = 16;
const HEDGE_RESULT_POLL: Duration = Duration::from_millis(10);
const ADMISSION_RETRY_POLL: Duration = Duration::from_millis(25);
#[cfg(test)]
const PROVIDER_TEST_DEADLINE: Duration = Duration::from_secs(30);
const DEFAULT_SPOOL_BYTES: u64 = 100 * 1024 * 1024 * 1024;
const DEFAULT_ARCHIVE_JOBS: usize = 2;
const DEFAULT_REPAIR_JOBS: usize = 1;

#[inline]
fn release_allocator_slack() {
    #[cfg(not(test))]
    unsafe {
        mi_collect(true);
    }
}

#[inline]
fn mark_allocator_collect() {
    ALLOCATOR_COLLECT_NEEDED.set(true);
}

#[inline]
fn release_allocator_slack_if_needed() {
    if ALLOCATOR_COLLECT_NEEDED.replace(false) {
        release_allocator_slack();
    }
}
#[cfg(test)]
const CONNECTION_TEST_PHASES: [&str; 7] = [
    "dns_address_policy",
    "tcp",
    "tls_certificate",
    "greeting_capabilities",
    "reader_mode",
    "authentication",
    "date",
];

#[derive(serde::Serialize)]
struct NzbParseResponse<'a> {
    version: u32,
    files: usize,
    segments: usize,
    nh1: &'a str,
    nm1: &'a str,
    metadata: &'a BTreeMap<String, String>,
    manifest: &'a [nzb::File],
}

#[derive(serde::Serialize)]
struct AssetCatalogResponse<'a> {
    version: u32,
    artifact_sha256: &'a str,
    assets: &'a [inspect::CatalogAsset],
}

#[derive(serde::Serialize)]
struct ArchiveCatalogMember {
    member_id: String,
    relative_path: String,
    exact_size: u64,
    kind: inspect::AssetKind,
}

#[derive(serde::Serialize)]
struct ArchiveCatalogResponse<'a> {
    version: u32,
    plan: &'a archive_group::VolumePlan,
    members: Vec<ArchiveCatalogMember>,
}

#[derive(serde::Serialize)]
struct NestedCatalogResponse<'a> {
    version: u32,
    plan: &'a archive_group::VolumePlan,
    members: Vec<archive_nested::NestedMember>,
}

#[derive(serde::Serialize)]
struct Par2DiscoveryResponse {
    version: u32,
    sets: Vec<serde_json::Value>,
}

#[derive(serde::Serialize)]
struct Par2SourceMapResponse {
    version: u32,
    set_id: String,
    slice_size: u64,
    mappings: Vec<serde_json::Value>,
}

#[derive(serde::Serialize)]
struct Par2RepairResponse {
    version: u32,
    set_id: String,
    file_id: String,
    relative_path: String,
    identity: String,
    byte_size: u64,
    asset_revision: String,
    partial_source_mapped: bool,
}

#[derive(serde::Serialize)]
struct ProviderSetRegistrationResponse<'a> {
    version: u32,
    provider_set_id: &'a str,
    generation: &'a str,
}

#[derive(serde::Serialize)]
struct SessionOpenResponse {
    version: u32,
    identity: String,
    byte_size: u64,
    revision: String,
    asset_revision: Option<String>,
}

#[derive(serde::Serialize)]
struct ArtifactInspectResponse<'a> {
    version: u32,
    artifact_sha256: &'a str,
    inspection_state: &'a str,
    container: &'a str,
    duration_millis: Option<u64>,
    inspected_head_bytes: usize,
    inspected_tail_bytes: usize,
}

#[derive(Deserialize)]
struct SessionRangeRequest {
    expected_size: u64,
    start: u64,
    end: u64,
    reader_lease_id: String,
}

#[derive(Deserialize)]
struct RawCompositeRangeRequest {
    expected_size: u64,
    start: u64,
    end: u64,
    reader_lease_id: String,
}

#[derive(Deserialize)]
struct ArchivePlanVolumeRequest {
    content_identity: String,
    relative_path: String,
    expected_size: u64,
}

#[derive(Deserialize)]
struct ArchivePlanRequest {
    volumes: Vec<ArchivePlanVolumeRequest>,
}

#[derive(Deserialize)]
struct ArchiveNestedCatalogRequest {
    volumes: Vec<ArchivePlanVolumeRequest>,
    #[serde(default)]
    passphrase: Option<String>,
}

#[derive(Deserialize)]
struct Par2CatalogRequest {
    files: Vec<ArchivePlanVolumeRequest>,
}

#[derive(Deserialize)]
struct Par2SourceMapRequest {
    files: Vec<ArchivePlanVolumeRequest>,
    sources: Vec<ArchivePlanVolumeRequest>,
    #[serde(default)]
    set_id: Option<String>,
}

#[derive(Deserialize)]
struct Par2RepairRequest {
    files: Vec<ArchivePlanVolumeRequest>,
    sources: Vec<ArchivePlanVolumeRequest>,
    #[serde(default)]
    partial_sources: Vec<MaterializationRequest>,
    #[serde(default)]
    set_id: Option<String>,
    selected_file_id: String,
}

#[derive(Deserialize)]
struct ArchiveSetExtractionRequest {
    volumes: Vec<ArchivePlanVolumeRequest>,
    expected_output_size: u64,
    selected_path: String,
}

#[derive(Deserialize)]
struct SessionArchiveVolumeRequest {
    session_id: String,
    revision: String,
    relative_path: String,
    expected_size: u64,
}

#[derive(Deserialize)]
struct SessionArchiveCatalogRequest {
    volumes: Vec<SessionArchiveVolumeRequest>,
}

#[derive(Deserialize)]
struct SessionArchiveOpenRequest {
    volumes: Vec<SessionArchiveVolumeRequest>,
    expected_output_size: u64,
    selected_path: String,
}

#[derive(Deserialize)]
struct ArchiveNestedExtractionRequest {
    volumes: Vec<ArchivePlanVolumeRequest>,
    expected_output_size: u64,
    selected_paths: Vec<String>,
    #[serde(default)]
    passphrase: Option<String>,
}

#[derive(Clone, Deserialize)]
struct MaterializationPosting {
    number: u64,
    bytes: u64,
    message_id: String,
}

#[derive(Deserialize)]
struct MaterializationRequest {
    postings: Vec<MaterializationPosting>,
    #[serde(default)]
    group: Option<String>,
    account_partition: String,
    provider_set_id: String,
}

#[derive(Deserialize)]
struct SessionCreateRequest {
    #[serde(flatten)]
    source: MaterializationRequest,
    #[serde(default)]
    allow_degraded_playback: bool,
    #[serde(default)]
    preparation: bool,
}

#[derive(Deserialize)]
struct NativeCatalogSelectionHint {
    relative_path: String,
    exact_size: u64,
}

#[derive(Deserialize)]
struct NativeCatalogRequest {
    manifest_identity: String,
    metadata: BTreeMap<String, String>,
    manifest: Vec<nzb::File>,
    #[serde(default)]
    selection_hint: Option<NativeCatalogSelectionHint>,
}

#[derive(Deserialize)]
struct NativeInspectRequest {
    postings: Vec<MaterializationPosting>,
    #[serde(default)]
    group: Option<String>,
    account_partition: String,
    provider_set_id: String,
}

#[derive(Deserialize)]
struct MaterializationInspectRequest {
    expected_size: u64,
}

#[derive(Clone)]
struct SessionSource {
    provider_set: Arc<provider::ProviderSet>,
    group: Option<String>,
    scheduler_session: String,
    work_class: nntp::WorkClass,
    use_memory_cache: bool,
    use_disk_cache: bool,
    admit_disk_cache: bool,
}

fn normalize_materialization_request(
    request: MaterializationRequest,
    provider_sets: &Mutex<provider::Registry>,
    work_class: nntp::WorkClass,
) -> Result<(SessionSource, Vec<session::SessionPosting>), &'static str> {
    if request.postings.is_empty()
        || request.postings.len() > nzb::MAX_SEGMENTS
        || request.postings.iter().any(|posting| {
            posting.bytes == 0 || posting.bytes > session::MAX_DECLARED_POSTING_BYTES
        })
        || request
            .group
            .as_deref()
            .is_some_and(|group| !nntp::valid_group(group))
        || !provider::valid_identity(&request.provider_set_id)
    {
        return Err("invalid_materialization");
    }
    let account_partition = provider::decode_partition(&request.account_partition)
        .map_err(|_| "invalid_materialization")?;
    let provider_set = provider_sets
        .lock()
        .expect("provider set registry lock")
        .acquire(
            &request.provider_set_id,
            account_partition,
            std::time::Instant::now(),
        )?;
    let mut request_postings = request.postings;
    request_postings.sort_by_key(|posting| posting.number);
    let mut postings = Vec::<session::SessionPosting>::new();
    for posting in request_postings {
        let message_id = nntp::canonical_message_id(&posting.message_id)?.to_owned();
        if postings
            .last()
            .is_some_and(|existing| existing.number == posting.number)
        {
            postings
                .last_mut()
                .expect("checked duplicate posting")
                .fallback_postings
                .push(session::SessionFallbackPosting {
                    declared_encoded_bytes: posting.bytes,
                    message_id,
                });
            continue;
        }
        if posting.number
            != u64::try_from(postings.len() + 1).map_err(|_| "invalid_materialization")?
        {
            return Err("invalid_materialization");
        }
        postings.push(session::SessionPosting {
            number: posting.number,
            declared_encoded_bytes: posting.bytes,
            message_id,
            fallback_postings: Vec::new(),
        });
    }
    Ok((
        SessionSource {
            provider_set,
            group: request.group,
            scheduler_session: String::new(),
            work_class,
            use_memory_cache: true,
            use_disk_cache: true,
            admit_disk_cache: true,
        },
        postings,
    ))
}

fn posting_acquisition_keys<'a>(
    source: &'a SessionSource,
    posting: &'a session::SessionPosting,
) -> impl Iterator<Item = cache::SegmentCacheKey> + 'a {
    source.provider_set.servers.iter().flat_map(move |server| {
        std::iter::once(posting.message_id.as_str())
            .chain(
                posting
                    .fallback_postings
                    .iter()
                    .map(|fallback| fallback.message_id.as_str()),
            )
            .map(move |message_id| {
                cache::SegmentCacheKey::acquisition(
                    source.provider_set.account_partition,
                    &source.provider_set.generation,
                    &server.provider_configuration_id,
                    message_id,
                )
            })
    })
}

fn missing_slice_count(
    postings: &[session::SessionPosting],
    unavailable: impl IntoIterator<Item = bool>,
    slice_size: u64,
) -> Result<usize, &'static str> {
    let mut slices = BTreeSet::new();
    let mut offset = 0u64;
    for (posting, unavailable) in postings.iter().zip(unavailable) {
        let end = offset
            .checked_add(posting.declared_encoded_bytes)
            .ok_or("repair_scope_exceeds_budget")?;
        if unavailable {
            slices.extend(offset / slice_size..=(end - 1) / slice_size);
        }
        offset = end;
    }
    Ok(slices.len())
}

struct RequestBodyBudget {
    maximum_bytes: usize,
    reserved_bytes: AtomicUsize,
    busy_rejections: AtomicU64,
}

struct RequestBodyPermit<'a> {
    budget: &'a RequestBodyBudget,
    bytes: usize,
}

impl RequestBodyBudget {
    fn new(maximum_bytes: usize) -> Self {
        Self {
            maximum_bytes,
            reserved_bytes: AtomicUsize::new(0),
            busy_rejections: AtomicU64::new(0),
        }
    }

    fn reserve(&self, bytes: usize) -> Result<RequestBodyPermit<'_>, &'static str> {
        let mut reserved = self.reserved_bytes.load(Ordering::Acquire);
        loop {
            let Some(next) = reserved
                .checked_add(bytes)
                .filter(|next| *next <= self.maximum_bytes)
            else {
                self.busy_rejections.fetch_add(1, Ordering::Relaxed);
                return Err("native_busy");
            };
            match self.reserved_bytes.compare_exchange_weak(
                reserved,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Ok(RequestBodyPermit {
                        budget: self,
                        bytes,
                    });
                }
                Err(current) => reserved = current,
            }
        }
    }
}

impl Drop for RequestBodyPermit<'_> {
    fn drop(&mut self) {
        self.budget
            .reserved_bytes
            .fetch_sub(self.bytes, Ordering::AcqRel);
    }
}

struct EngineState {
    archive_runtime: Option<archive_runtime::Runtime>,
    par2_tool: Option<par2_tool::Tool>,
    segment_cache: Mutex<cache::VerifiedSegmentCache>,
    disk_cache: Option<Mutex<cache::DiskSegmentCache>>,
    session_checkpoints: Mutex<cache::SessionCheckpointStore>,
    negative_cache: Mutex<cache::NegativeSegmentCache>,
    network_singleflight: cache::NetworkSingleflight,
    verified_materializations: Mutex<VerifiedMaterializationCache>,
    nntp_pools: Arc<nntp::PoolRegistry>,
    provider_sets: Mutex<provider::Registry>,
    raw_composites: Mutex<raw_composite::RawCompositeRegistry>,
    resources: resources::NativeResources,
    sessions: Mutex<session::SessionRegistry<SessionSource>>,
    request_bodies: RequestBodyBudget,
    active_requests: AtomicUsize,
    active_materializations: AtomicUsize,
    request_queue_busy_rejections: AtomicU64,
    prefetch_window_bytes: u64,
    maximum_background_prefetches: usize,
    background_prefetches: AtomicUsize,
    maximum_active_hedges: usize,
    active_hedges: AtomicUsize,
    hedges_started: AtomicU64,
    hedges_won: AtomicU64,
    salvage_holes_total: AtomicU64,
    salvage_bytes_total: AtomicU64,
    draining: AtomicBool,
}

#[derive(Default)]
struct VerifiedMaterializationCache {
    entries: BTreeMap<String, (materialization::ImmutableFileIdentity, u64)>,
    sequence: u64,
}

impl VerifiedMaterializationCache {
    fn get(&mut self, identity: &str) -> Option<materialization::ImmutableFileIdentity> {
        let (file_identity, used) = self.entries.get_mut(identity)?;
        self.sequence = self.sequence.wrapping_add(1);
        *used = self.sequence;
        Some(*file_identity)
    }

    fn insert(&mut self, identity: String, file_identity: materialization::ImmutableFileIdentity) {
        self.sequence = self.sequence.wrapping_add(1);
        if !self.entries.contains_key(&identity)
            && self.entries.len() >= nzb::MAX_FILES
            && let Some(oldest) = self
                .entries
                .iter()
                .min_by_key(|(_identity, (_file_identity, used))| *used)
                .map(|(identity, _entry)| identity.clone())
        {
            self.entries.remove(&oldest);
        }
        self.entries
            .insert(identity, (file_identity, self.sequence));
    }
}

enum PostingAttempt {
    Complete(Result<cache::SegmentLease, &'static str>),
    Pending {
        flight: cache::NetworkFlightWaiter,
        cache_key: cache::SegmentCacheKey,
        server_index: usize,
        admit_memory: bool,
        admit_disk: bool,
    },
}

struct ActiveCounterPermit<'a> {
    active: &'a AtomicUsize,
}

impl Drop for ActiveCounterPermit<'_> {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::AcqRel);
    }
}

struct NativeBudgets {
    memory_cache_bytes: usize,
    disk_cache_bytes: u64,
    minimum_free_disk_bytes: u64,
    maximum_nntp_connections: usize,
    maximum_spool_bytes: u64,
    maximum_archive_jobs: usize,
    maximum_repair_jobs: usize,
}

impl EngineState {
    fn new(
        local_data: &Path,
        memory_cache_bytes: usize,
        disk_cache_bytes: u64,
        minimum_free_disk_bytes: u64,
        maximum_nntp_connections: usize,
    ) -> Result<Self, &'static str> {
        Self::new_with_par2(
            local_data,
            NativeBudgets {
                memory_cache_bytes,
                disk_cache_bytes,
                minimum_free_disk_bytes,
                maximum_nntp_connections,
                maximum_spool_bytes: DEFAULT_SPOOL_BYTES,
                maximum_archive_jobs: DEFAULT_ARCHIVE_JOBS,
                maximum_repair_jobs: DEFAULT_REPAIR_JOBS,
            },
            None,
            None,
        )
    }

    fn new_with_par2(
        local_data: &Path,
        budgets: NativeBudgets,
        par2_binary: Option<&Path>,
        libarchive_library: Option<&Path>,
    ) -> Result<Self, &'static str> {
        if !(16 * 1024 * 1024..=2 * 1024 * 1024 * 1024).contains(&budgets.memory_cache_bytes) {
            return Err("invalid_segment_cache_budget");
        }
        let resources = resources::NativeResources::new(
            local_data,
            budgets.maximum_spool_bytes,
            budgets.minimum_free_disk_bytes,
            budgets.maximum_archive_jobs,
            budgets.maximum_repair_jobs,
        )?;
        let nntp_pools = Arc::new(nntp::PoolRegistry::new_with_decoded_budget(
            budgets.maximum_nntp_connections,
            u64::try_from(budgets.memory_cache_bytes)
                .map_err(|_| "invalid_segment_cache_budget")?
                // Preserve one background article plus both sides of an
                // interactive provider hedge at the minimum cache setting.
                .max(session::MAX_DECLARED_POSTING_BYTES * 3),
        )?);
        let par2_tool = par2_binary
            .map(|binary| {
                let tool = par2_tool::Tool::validate(binary)?;
                tool.verify_repair_engine(local_data)?;
                Ok(tool)
            })
            .transpose()?;
        let archive_runtime = libarchive_library
            .map(|library| {
                let runtime = archive_runtime::Runtime::validate(library)?;
                runtime.verify_extraction_engine(local_data)?;
                Ok(runtime)
            })
            .transpose()?;
        let maximum_background_prefetches = nntp_pools
            .preparation_slots()
            .min(MAX_CONCURRENT_PREFETCH_SESSIONS);
        let maximum_active_hedges = (nntp_pools.preparation_slots() / 2).min(MAX_ACTIVE_HEDGES);
        Ok(Self {
            archive_runtime,
            par2_tool,
            segment_cache: Mutex::new(cache::VerifiedSegmentCache::new(
                budgets.memory_cache_bytes,
            )?),
            disk_cache: if budgets.disk_cache_bytes == 0 {
                None
            } else {
                Some(Mutex::new(cache::DiskSegmentCache::open(
                    local_data,
                    budgets.disk_cache_bytes,
                    budgets.minimum_free_disk_bytes,
                )?))
            },
            session_checkpoints: Mutex::new(cache::SessionCheckpointStore::open(local_data)?),
            negative_cache: Mutex::new(cache::NegativeSegmentCache::with_default_capacity()),
            network_singleflight: cache::NetworkSingleflight::new(),
            verified_materializations: Mutex::new(VerifiedMaterializationCache::default()),
            nntp_pools: Arc::clone(&nntp_pools),
            provider_sets: Mutex::new(provider::Registry::new(nntp_pools)),
            raw_composites: Mutex::new(raw_composite::RawCompositeRegistry::new(
                (budgets.memory_cache_bytes / 16)
                    .max(raw_composite::MIN_COMPOSITE_METADATA_BUDGET_BYTES),
            )),
            resources,
            sessions: Mutex::new(session::SessionRegistry::new(
                budgets
                    .memory_cache_bytes
                    .max(session::MIN_SESSION_METADATA_BUDGET_BYTES),
            )),
            // Preserve admission for one maximum-sized metadata request even when
            // the configured memory cache is smaller.
            request_bodies: RequestBodyBudget::new(
                budgets.memory_cache_bytes.max(MAX_NZB_METADATA_BYTES),
            ),
            active_requests: AtomicUsize::new(0),
            active_materializations: AtomicUsize::new(0),
            request_queue_busy_rejections: AtomicU64::new(0),
            prefetch_window_bytes: u64::try_from(
                budgets.memory_cache_bytes / MAX_CONCURRENT_PREFETCH_SESSIONS,
            )
            .map_err(|_| "invalid_segment_cache_budget")?,
            // One prefetch session can already fill every NNTP lane. More
            // sessions improve fairness, but cannot improve throughput once
            // their count reaches the connection ceiling.
            maximum_background_prefetches,
            background_prefetches: AtomicUsize::new(0),
            maximum_active_hedges,
            active_hedges: AtomicUsize::new(0),
            hedges_started: AtomicU64::new(0),
            hedges_won: AtomicU64::new(0),
            salvage_holes_total: AtomicU64::new(0),
            salvage_bytes_total: AtomicU64::new(0),
            draining: AtomicBool::new(false),
        })
    }

    fn start_session_prefetch(
        self: &Arc<Self>,
        session: Arc<Mutex<session::RandomAccessSession>>,
        recreation_key: Arc<str>,
        context: Arc<SessionSource>,
        generation: reader_lease::ReaderGeneration,
        posting_index: usize,
    ) {
        if self.draining.load(Ordering::Acquire) {
            return;
        }
        let Some(generation_permit) = generation.request_prefetch(posting_index) else {
            return;
        };
        if self
            .background_prefetches
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |active| {
                (active < self.maximum_background_prefetches).then_some(active + 1)
            })
            .is_err()
        {
            return;
        }
        if self.draining.load(Ordering::Acquire) {
            self.background_prefetches.fetch_sub(1, Ordering::AcqRel);
            return;
        }
        let state = Arc::clone(self);
        let mut source = (*context).clone();
        source.work_class = nntp::WorkClass::Background;
        let spawn = thread::Builder::new()
            .name("usenet-prefetch".to_owned())
            .spawn(move || {
                let _task_permit = BackgroundPrefetchPermit {
                    state: Arc::clone(&state),
                };
                if let Err(code) = state.run_session_prefetch(
                    session,
                    recreation_key,
                    source,
                    generation,
                    generation_permit,
                ) {
                    observability::emit(
                        observability::Detail::Normal,
                        observability::Level::Error,
                        "nntp.prefetch.failed",
                        "NNTP session prefetch failed",
                        None,
                        &[observability::Field::token("error_code", code)],
                    );
                }
            });
        if spawn.is_err() {
            self.background_prefetches.fetch_sub(1, Ordering::AcqRel);
            observability::emit(
                observability::Detail::Normal,
                observability::Level::Error,
                "nntp.prefetch.failed",
                "NNTP session prefetch failed",
                None,
                &[observability::Field::token(
                    "error_code",
                    "prefetch_thread_unavailable",
                )],
            );
        }
    }

    fn run_session_prefetch(
        &self,
        session: Arc<Mutex<session::RandomAccessSession>>,
        recreation_key: Arc<str>,
        source: SessionSource,
        generation: reader_lease::ReaderGeneration,
        mut generation_permit: reader_lease::ReaderPrefetchPermit,
    ) -> Result<(), &'static str> {
        let started = Instant::now();
        let diagnostics = observability::enabled(observability::Detail::Normal)
            .then(|| source.provider_set.diagnostics());
        loop {
            let active_prefetches = self.background_prefetches.load(Ordering::Acquire).max(1);
            let parallelism = self
                .nntp_pools
                .preparation_slots()
                .div_ceil(active_prefetches);
            let (revision, posting_index) = generation_permit.request();
            let (postings, posting_indexes) = session
                .lock()
                .expect("random access session lock")
                .postings_for_prefetch(posting_index, self.prefetch_window_bytes);
            let next = AtomicUsize::new(0);
            let failed = AtomicBool::new(false);
            thread::scope(|scope| {
                let (completed, completions) = mpsc::channel();
                for _ in 0..parallelism.min(posting_indexes.len()) {
                    let source = &source;
                    let generation = &generation;
                    let postings = &postings;
                    let posting_indexes = &posting_indexes;
                    let next = &next;
                    let failed = &failed;
                    let completed = completed.clone();
                    scope.spawn(move || {
                        while !failed.load(Ordering::Acquire)
                            && !generation.is_obsolete()
                            && !self.draining.load(Ordering::Acquire)
                        {
                            let index = next.fetch_add(1, Ordering::AcqRel);
                            let Some(posting_index) = posting_indexes.get(index).copied() else {
                                break;
                            };
                            let result =
                                self.fetch_posting(source, &postings[posting_index], &|| {
                                    generation.is_obsolete()
                                        || self.draining.load(Ordering::Acquire)
                                });
                            if result.is_err() {
                                failed.store(true, Ordering::Release);
                            }
                            let _ = completed.send((index, posting_index, result));
                        }
                    });
                }
                drop(completed);
                let mut pending = BTreeMap::new();
                let mut next_commit = 0;
                for (index, posting_index, result) in completions {
                    pending.insert(index, (posting_index, result));
                    while let Some((posting_index, result)) = pending.remove(&next_commit) {
                        next_commit += 1;
                        let Ok(segment) = result else {
                            return Ok(());
                        };
                        session
                            .lock()
                            .expect("random access session lock")
                            .record_prefetched(posting_index, segment.segment())?;
                    }
                }
                Ok(())
            })?;
            if generation.is_obsolete()
                || self.draining.load(Ordering::Acquire)
                || generation_permit.finish(revision)
            {
                break;
            }
        }
        self.persist_session_checkpoints(&recreation_key, &session)?;
        if let Some(before) = diagnostics {
            let after = source.provider_set.diagnostics();
            let duration_ms = u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX);
            for (index, (before, after)) in before.into_iter().zip(after).enumerate() {
                let suppliers = after.suppliers.saturating_sub(before.suppliers);
                let failures = after.failures.saturating_sub(before.failures);
                let authentication_failures = after
                    .authentication_failures
                    .saturating_sub(before.authentication_failures);
                let missing = after.missing.saturating_sub(before.missing);
                if suppliers == 0 && failures == 0 && missing == 0 {
                    continue;
                }
                let server = &source.provider_set.servers[index];
                observability::emit(
                    observability::Detail::Normal,
                    observability::Level::Info,
                    "nntp.provider.completed",
                    "NNTP provider prefetch completed",
                    None,
                    &[
                        observability::Field::token(
                            "provider_name",
                            &server.provider_configuration_id,
                        ),
                        observability::Field::token("provider_host", &server.request.host),
                        observability::Field::unsigned("article_count", suppliers),
                        observability::Field::unsigned("failure_count", failures),
                        observability::Field::unsigned(
                            "authentication_failure_count",
                            authentication_failures,
                        ),
                        observability::Field::unsigned("missing_count", missing),
                        observability::Field::unsigned("duration_ms", duration_ms),
                    ],
                );
            }
        }
        Ok(())
    }

    fn persist_session_checkpoints(
        &self,
        recreation_key: &str,
        session: &Arc<Mutex<session::RandomAccessSession>>,
    ) -> Result<(), &'static str> {
        let mut store = self
            .session_checkpoints
            .lock()
            .expect("session checkpoint store lock");
        let checkpoints = session
            .lock()
            .expect("random access session lock")
            .pending_checkpoints()
            .to_vec();
        store.merge(recreation_key, &checkpoints)?;
        session
            .lock()
            .expect("random access session lock")
            .commit_pending_checkpoints(checkpoints.len());
        Ok(())
    }

    fn begin_posting_attempt<F>(
        &self,
        source: &SessionSource,
        server_index: usize,
        message_id: &str,
        declared_encoded_bytes: u64,
        cancelled: &F,
    ) -> PostingAttempt
    where
        F: Fn() -> bool,
    {
        if cancelled() {
            return PostingAttempt::Complete(Err("nntp_cancelled"));
        }
        let template = &source.provider_set.servers[server_index];
        let cache_key = cache::SegmentCacheKey::acquisition(
            source.provider_set.account_partition,
            &source.provider_set.generation,
            &template.provider_configuration_id,
            message_id,
        );
        let now = Instant::now();
        if let Some(kind) = self
            .negative_cache
            .lock()
            .expect("negative cache lock")
            .get(cache_key, now)
        {
            return PostingAttempt::Complete(Err(match kind {
                cache::NegativeKind::Missing => "nntp_article_missing",
                cache::NegativeKind::Corrupt => "yenc_crc_mismatch",
            }));
        }
        if let Some(hit) = self
            .segment_cache
            .lock()
            .expect("segment cache lock")
            .get(cache_key)
        {
            return PostingAttempt::Complete(Ok(hit));
        }
        let disk_hit = if source.use_disk_cache {
            self.disk_cache.as_ref().and_then(|disk_cache| {
                match disk_cache.lock().expect("disk cache lock").get(cache_key) {
                    Ok(hit) => hit,
                    Err(code) => {
                        Self::report_disk_cache_failure("read", code);
                        None
                    }
                }
            })
        } else {
            None
        };
        if let Some(hit) = disk_hit {
            let shared = Arc::new(hit.clone());
            if !source.use_memory_cache {
                return PostingAttempt::Complete(Ok(cache::SegmentLease::detached(shared)));
            }
            let mut memory_cache = self.segment_cache.lock().expect("segment cache lock");
            memory_cache.insert(cache_key, hit);
            return PostingAttempt::Complete(Ok(memory_cache
                .get(cache_key)
                .unwrap_or_else(|| cache::SegmentLease::detached(shared))));
        }
        let mut server = template.request.clone();
        server.message_id = message_id.to_owned();
        let nntp_pools = Arc::clone(&self.nntp_pools);
        let pool_reference = source.provider_set.pool_references[server_index].clone();
        let group = source.group.clone();
        let scheduling = nntp::SchedulingContext {
            work_class: source.work_class,
            configuration_partition: source.provider_set.account_partition,
            session: source.scheduler_session.clone(),
            declared_encoded_bytes,
        };
        let priority = match source.work_class {
            nntp::WorkClass::Interactive => cache::FlightPriority::Interactive,
            nntp::WorkClass::Preparation => cache::FlightPriority::Preparation,
            nntp::WorkClass::Background => cache::FlightPriority::Background,
        };
        match self.network_singleflight.acquire_prioritized(
            cache_key,
            priority,
            move |cancellation| {
                cancellation.checkpoint()?;
                let value = nntp_pools.verified_part_scheduled(
                    pool_reference,
                    server,
                    group,
                    MAX_ARTICLE_BYTES,
                    cancellation,
                    scheduling,
                )?;
                cache::VerifiedSegment::from_decoded(value)
            },
        ) {
            Ok(flight) => PostingAttempt::Pending {
                flight,
                cache_key,
                server_index,
                admit_memory: source.use_memory_cache,
                admit_disk: source.admit_disk_cache,
            },
            Err(code) => PostingAttempt::Complete(Err(code)),
        }
    }

    fn finish_network_attempt(
        &self,
        cache_key: cache::SegmentCacheKey,
        server_index: usize,
        admit_memory: bool,
        admit_disk: bool,
        publish_result: bool,
        result: Result<Arc<cache::VerifiedSegment>, &'static str>,
    ) -> Result<cache::SegmentLease, &'static str> {
        match result {
            Ok(verified) => {
                if publish_result {
                    self.negative_cache
                        .lock()
                        .expect("negative cache lock")
                        .record_success(cache_key);
                    if admit_disk
                        && let Some(disk_cache) = &self.disk_cache
                        && let Err(code) = disk_cache
                            .lock()
                            .expect("disk cache lock")
                            .insert(cache_key, verified.as_ref())
                    {
                        Self::report_disk_cache_failure("write", code);
                    }
                }
                if server_index > 0 && publish_result {
                    self.nntp_pools.record_provider_failover();
                }
                if admit_memory {
                    let mut memory_cache = self.segment_cache.lock().expect("segment cache lock");
                    memory_cache.insert(cache_key, verified.as_ref().clone());
                    return Ok(memory_cache
                        .get(cache_key)
                        .unwrap_or_else(|| cache::SegmentLease::detached(verified)));
                }
                Ok(cache::SegmentLease::detached(verified))
            }
            Err(code) => {
                let negative = if code == "nntp_article_missing" {
                    Some(cache::NegativeKind::Missing)
                } else if yenc::integrity_failure(code) {
                    Some(cache::NegativeKind::Corrupt)
                } else {
                    None
                };
                if let Some(kind) = negative
                    && publish_result
                {
                    self.negative_cache
                        .lock()
                        .expect("negative cache lock")
                        .insert(cache_key, kind, Instant::now());
                }
                Err(code)
            }
        }
    }

    fn finish_posting_attempt<F>(
        &self,
        attempt: PostingAttempt,
        cancelled: &F,
    ) -> Result<cache::SegmentLease, &'static str>
    where
        F: Fn() -> bool,
    {
        match attempt {
            PostingAttempt::Complete(result) => result,
            PostingAttempt::Pending {
                flight,
                cache_key,
                server_index,
                admit_memory,
                admit_disk,
            } => {
                let result = flight.wait_cancellable(cancelled);
                let publish_result = flight.claim_result();
                self.finish_network_attempt(
                    cache_key,
                    server_index,
                    admit_memory,
                    admit_disk,
                    publish_result,
                    result,
                )
            }
        }
    }

    fn finish_ready_attempt(
        &self,
        attempt: PostingAttempt,
        result: Result<Arc<cache::VerifiedSegment>, &'static str>,
    ) -> Result<cache::SegmentLease, &'static str> {
        match attempt {
            PostingAttempt::Pending {
                flight,
                cache_key,
                server_index,
                admit_memory,
                admit_disk,
            } => {
                let publish_result = flight.claim_result();
                self.finish_network_attempt(
                    cache_key,
                    server_index,
                    admit_memory,
                    admit_disk,
                    publish_result,
                    result,
                )
            }
            PostingAttempt::Complete(result) => result,
        }
    }

    fn try_hedge_permit(&self) -> Option<ActiveCounterPermit<'_>> {
        self.active_hedges
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |active| {
                (active < self.maximum_active_hedges).then_some(active + 1)
            })
            .ok()
            .map(|_| ActiveCounterPermit {
                active: &self.active_hedges,
            })
    }

    fn increment(counter: &AtomicU64) {
        Self::increment_by(counter, 1);
    }

    fn increment_by(counter: &AtomicU64, amount: u64) {
        let _ = counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            Some(value.saturating_add(amount))
        });
    }

    fn report_disk_cache_failure(operation: &'static str, code: &'static str) {
        observability::emit(
            observability::Detail::Normal,
            observability::Level::Error,
            "native.disk_cache.failed",
            "Native disk cache operation failed",
            None,
            &[
                observability::Field::token("operation", operation),
                observability::Field::token("error_code", code),
            ],
        );
    }

    fn fetch_posting<F>(
        &self,
        source: &SessionSource,
        posting: &session::SessionPosting,
        cancelled: &F,
    ) -> Result<cache::SegmentLease, &'static str>
    where
        F: Fn() -> bool,
    {
        if cancelled() {
            return Err("nntp_cancelled");
        }
        let mut initial_failures = vec![None; source.provider_set.servers.len()];
        if source.work_class == nntp::WorkClass::Interactive
            && source.provider_set.servers.len() >= 2
        {
            let mut primary = Some(self.begin_posting_attempt(
                source,
                0,
                &posting.message_id,
                posting.declared_encoded_bytes,
                cancelled,
            ));
            let primary_delay =
                source.provider_set.pool_references[0].hedge_delay(posting.declared_encoded_bytes);
            let primary_ready = match primary.as_ref().expect("primary posting attempt") {
                PostingAttempt::Complete(_) => true,
                PostingAttempt::Pending { flight, .. } => flight
                    .wait_cancellable_for(cancelled, primary_delay)
                    .is_some(),
            };
            if primary_ready {
                let result = self.finish_posting_attempt(
                    primary.take().expect("primary posting attempt"),
                    cancelled,
                );
                match result {
                    Ok(segment) => return Ok(segment),
                    Err("nntp_cancelled") => return Err("nntp_cancelled"),
                    Err(code) => initial_failures[0] = Some(code),
                }
            } else if let Some(_hedge_permit) = self.try_hedge_permit() {
                Self::increment(&self.hedges_started);
                let mut backup = Some(self.begin_posting_attempt(
                    source,
                    1,
                    &posting.message_id,
                    posting.declared_encoded_bytes,
                    cancelled,
                ));
                loop {
                    if cancelled() {
                        return Err("nntp_cancelled");
                    }
                    let mut progressed = false;
                    for (server_index, attempt) in [(0, &mut primary), (1, &mut backup)] {
                        let ready = match attempt.as_ref() {
                            Some(PostingAttempt::Complete(_)) => Some(None),
                            Some(PostingAttempt::Pending { flight, .. }) => {
                                flight.try_result().map(Some)
                            }
                            None => None,
                        };
                        let Some(ready) = ready else {
                            continue;
                        };
                        progressed = true;
                        let attempt = attempt.take().expect("ready posting attempt");
                        let result = match ready {
                            Some(result) => self.finish_ready_attempt(attempt, result),
                            None => self.finish_posting_attempt(attempt, cancelled),
                        };
                        match result {
                            Ok(segment) => {
                                if server_index == 1 {
                                    Self::increment(&self.hedges_won);
                                }
                                return Ok(segment);
                            }
                            Err("nntp_cancelled") => return Err("nntp_cancelled"),
                            Err(code) => initial_failures[server_index] = Some(code),
                        }
                    }
                    if primary.is_none() && backup.is_none() {
                        break;
                    }
                    if !progressed {
                        thread::sleep(HEDGE_RESULT_POLL);
                    }
                }
            } else {
                let result = self.finish_posting_attempt(
                    primary.take().expect("primary posting attempt"),
                    cancelled,
                );
                match result {
                    Ok(segment) => return Ok(segment),
                    Err("nntp_cancelled") => return Err("nntp_cancelled"),
                    Err(code) => initial_failures[0] = Some(code),
                }
            }
        }

        let mut failures = PostingFailureAggregate::new(source.provider_set.servers.len());
        #[allow(clippy::needless_range_loop)]
        for server_index in 0..source.provider_set.servers.len() {
            let mut provider_unavailable = false;
            for (posting_index, (message_id, declared_encoded_bytes)) in
                std::iter::once((posting.message_id.as_str(), posting.declared_encoded_bytes))
                    .chain(posting.fallback_postings.iter().map(|fallback| {
                        (
                            fallback.message_id.as_str(),
                            fallback.declared_encoded_bytes,
                        )
                    }))
                    .enumerate()
            {
                let result = if posting_index == 0 {
                    match initial_failures[server_index].take() {
                        Some(code) => Err(code),
                        None => self.finish_posting_attempt(
                            self.begin_posting_attempt(
                                source,
                                server_index,
                                message_id,
                                declared_encoded_bytes,
                                cancelled,
                            ),
                            cancelled,
                        ),
                    }
                } else {
                    self.finish_posting_attempt(
                        self.begin_posting_attempt(
                            source,
                            server_index,
                            message_id,
                            declared_encoded_bytes,
                            cancelled,
                        ),
                        cancelled,
                    )
                };
                match result {
                    Ok(segment) => return Ok(segment),
                    Err("nntp_cancelled") => return Err("nntp_cancelled"),
                    Err("native_busy") => return Err("native_busy"),
                    Err(code) => {
                        failures.observe(code);
                        if code != "nntp_article_missing" && !yenc::integrity_failure(code) {
                            provider_unavailable = true;
                        }
                    }
                }
                if provider_unavailable {
                    break;
                }
            }
        }
        Err(failures.finish())
    }

    fn fetch_postings_batched<F, G>(
        &self,
        source: &SessionSource,
        postings: &[session::SessionPosting],
        allow_holes: bool,
        cancelled: &F,
        mut consume: G,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
        G: FnMut(&cache::VerifiedSegment) -> Result<(), &'static str>,
    {
        let mut next = 0usize;
        let mut pending = VecDeque::new();
        while next < postings.len() || !pending.is_empty() {
            while let Some(posting) = postings.get(next) {
                if cancelled() {
                    return Err("nntp_cancelled");
                }
                let attempt = self.begin_posting_attempt(
                    source,
                    0,
                    &posting.message_id,
                    posting.declared_encoded_bytes,
                    cancelled,
                );
                match attempt {
                    PostingAttempt::Complete(Err("native_busy" | "nntp_singleflight_capacity")) => {
                        break;
                    }
                    PostingAttempt::Complete(result) => {
                        next += 1;
                        self.consume_staged_posting(
                            source,
                            posting,
                            result,
                            allow_holes,
                            cancelled,
                            &mut consume,
                        )?;
                    }
                    attempt => {
                        pending.push_back((next, attempt));
                        next += 1;
                    }
                }
            }
            let ready = pending
                .iter()
                .enumerate()
                .find_map(|(queue_index, (_, attempt))| {
                    let PostingAttempt::Pending { flight, .. } = attempt else {
                        return None;
                    };
                    flight.try_result().map(|result| (queue_index, result))
                });
            if let Some((index, attempt, ready)) = ready
                .map(|(queue_index, result)| {
                    let (index, attempt) = pending
                        .remove(queue_index)
                        .expect("ready staged posting remains queued");
                    (index, attempt, Some(result))
                })
                .or_else(|| {
                    pending
                        .pop_front()
                        .map(|(index, attempt)| (index, attempt, None))
                })
            {
                let result = match ready {
                    Some(result) => self.finish_ready_attempt(attempt, result),
                    None => self.finish_posting_attempt(attempt, cancelled),
                };
                self.consume_staged_posting(
                    source,
                    &postings[index],
                    result,
                    allow_holes,
                    cancelled,
                    &mut consume,
                )?;
            } else if next < postings.len() {
                thread::sleep(ADMISSION_RETRY_POLL);
            }
        }
        Ok(())
    }

    fn consume_staged_posting<F, G>(
        &self,
        source: &SessionSource,
        posting: &session::SessionPosting,
        initial: Result<cache::SegmentLease, &'static str>,
        allow_holes: bool,
        cancelled: &F,
        consume: &mut G,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
        G: FnMut(&cache::VerifiedSegment) -> Result<(), &'static str>,
    {
        let result = match initial {
            Err(code)
                if code != "nntp_cancelled"
                    && (!posting.fallback_postings.is_empty()
                        || source.provider_set.servers.len() > 1) =>
            {
                self.fetch_posting(source, posting, cancelled)
            }
            result => result,
        };
        match result {
            Ok(segment) => consume(segment.segment()),
            Err(code)
                if allow_holes
                    && (code == "nntp_article_missing" || yenc::integrity_failure(code)) =>
            {
                Ok(())
            }
            Err(code) => Err(code),
        }
    }

    fn cached_posting_states(
        &self,
        source: &SessionSource,
        postings: &[session::SessionPosting],
    ) -> Vec<Option<bool>> {
        let mut states = vec![None; postings.len()];
        let mut memory = self.segment_cache.lock().expect("segment cache lock");
        for (state, posting) in states.iter_mut().zip(postings) {
            if posting_acquisition_keys(source, posting).any(|key| memory.get(key).is_some()) {
                *state = Some(true);
            }
        }
        drop(memory);
        let now = Instant::now();
        let mut negative = self.negative_cache.lock().expect("negative cache lock");
        for (state, posting) in states.iter_mut().zip(postings) {
            if state.is_none()
                && posting_acquisition_keys(source, posting)
                    .all(|key| negative.get(key, now).is_some())
            {
                *state = Some(false);
            }
        }
        states
    }

    fn survey_missing_slices<F>(
        &self,
        source: &SessionSource,
        postings: &[session::SessionPosting],
        cached: &[Option<bool>],
        slice_size: u64,
        cancelled: &F,
    ) -> Result<Option<usize>, &'static str>
    where
        F: Fn() -> bool,
    {
        let mut unavailable = cached
            .iter()
            .map(|state| *state == Some(false))
            .collect::<Vec<_>>();
        let mut unresolved = cached
            .iter()
            .enumerate()
            .filter_map(|(index, state)| state.is_none().then_some(index))
            .collect::<Vec<_>>();
        let mut available = vec![false; postings.len()];
        for (server_index, server) in source.provider_set.servers.iter().enumerate() {
            if unresolved.is_empty() {
                break;
            }
            let alternatives = postings
                .iter()
                .map(|posting| posting.fallback_postings.len() + 1)
                .max()
                .unwrap_or(0);
            for alternative in 0..alternatives {
                let candidates = unresolved
                    .iter()
                    .filter_map(|index| {
                        let posting = &postings[*index];
                        let message_id = if alternative == 0 {
                            Some(posting.message_id.as_str())
                        } else {
                            posting
                                .fallback_postings
                                .get(alternative - 1)
                                .map(|fallback| fallback.message_id.as_str())
                        }?;
                        Some((*index, message_id))
                    })
                    .collect::<Vec<_>>();
                for batch in candidates.chunks(server.pipeline) {
                    if cancelled() {
                        return Err("nntp_cancelled");
                    }
                    let message_ids = batch
                        .iter()
                        .map(|(_index, message_id)| *message_id)
                        .collect::<Vec<_>>();
                    let present = match self.nntp_pools.stat_batch(
                        &source.provider_set.pool_references[server_index],
                        &server.request,
                        source.group.as_deref(),
                        &message_ids,
                        cancelled,
                    ) {
                        Ok(present) => present,
                        Err("nntp_cancelled") => return Err("nntp_cancelled"),
                        Err(_) => return Ok(None),
                    };
                    for ((index, _message_id), present) in batch.iter().zip(present) {
                        if present {
                            available[*index] = true;
                        }
                    }
                }
                unresolved.retain(|index| !available[*index]);
            }
        }
        for index in unresolved {
            unavailable[index] = true;
        }
        missing_slice_count(postings, unavailable, slice_size).map(Some)
    }

    fn survey_repair_closure<F>(
        &self,
        sources: &[(SessionSource, Vec<session::SessionPosting>)],
        slice_size: u64,
        cancelled: &F,
    ) -> Result<Option<usize>, &'static str>
    where
        F: Fn() -> bool + Sync,
    {
        let cached = sources
            .iter()
            .map(|(source, postings)| self.cached_posting_states(source, postings))
            .collect::<Vec<_>>();
        let started = Instant::now();
        let article_count = sources
            .iter()
            .map(|(_source, postings)| postings.len())
            .sum::<usize>();
        let observe = observability::enabled(observability::Detail::Normal);
        if observe {
            observability::emit(
                observability::Detail::Normal,
                observability::Level::Info,
                "nntp.repair_survey.started",
                "NNTP repair availability survey started",
                None,
                &[
                    observability::Field::unsigned("article_count", article_count as u64),
                    observability::Field::unsigned("item_count", sources.len() as u64),
                ],
            );
        }
        let next = AtomicUsize::new(0);
        let missing = AtomicUsize::new(0);
        let stop = AtomicBool::new(false);
        let unsupported = AtomicBool::new(false);
        let failure = Mutex::new(None);
        thread::scope(|scope| {
            for _ in 0..self.nntp_pools.preparation_slots().min(sources.len()) {
                scope.spawn(|| {
                    loop {
                        if stop.load(Ordering::Acquire) || cancelled() {
                            break;
                        }
                        let index = next.fetch_add(1, Ordering::AcqRel);
                        let Some((source, postings)) = sources.get(index) else {
                            break;
                        };
                        match self.survey_missing_slices(
                            source,
                            postings,
                            &cached[index],
                            slice_size,
                            &|| stop.load(Ordering::Acquire) || cancelled(),
                        ) {
                            Ok(Some(count)) => {
                                missing.fetch_add(count, Ordering::AcqRel);
                            }
                            Ok(None) => {
                                unsupported.store(true, Ordering::Release);
                                stop.store(true, Ordering::Release);
                            }
                            Err("nntp_cancelled") if stop.load(Ordering::Acquire) => {}
                            Err(code) => {
                                *failure.lock().expect("repair survey failure lock") = Some(code);
                                stop.store(true, Ordering::Release);
                            }
                        }
                    }
                });
            }
        });
        let result = if cancelled() {
            Err("nntp_cancelled")
        } else if let Some(code) = *failure.lock().expect("repair survey failure lock") {
            Err(code)
        } else if unsupported.load(Ordering::Acquire) {
            Ok(None)
        } else {
            Ok(Some(missing.load(Ordering::Acquire)))
        };
        let outcome = match result {
            Ok(Some(_)) => "ok",
            Ok(None) => "skipped",
            Err("nntp_cancelled") => "cancelled",
            Err(_) => "failed",
        };
        if observe {
            observability::emit(
                observability::Detail::Normal,
                observability::Level::Info,
                "nntp.repair_survey.completed",
                "NNTP repair availability survey completed",
                None,
                &[
                    observability::Field::token("outcome", outcome),
                    observability::Field::unsigned("article_count", article_count as u64),
                    observability::Field::unsigned(
                        "missing_count",
                        missing.load(Ordering::Acquire) as u64,
                    ),
                    observability::Field::unsigned(
                        "duration_ms",
                        u64::try_from(started.elapsed().as_millis()).unwrap_or(u64::MAX),
                    ),
                ],
            );
        }
        result
    }

    fn try_materialization_permit(&self) -> Option<ActiveCounterPermit<'_>> {
        // Preserve whole-request deduplication even at the minimum memory budget;
        // unrelated excess work still fails on first-segment admission.
        let maximum = self
            .nntp_pools
            .preparation_slots()
            .max(2)
            .min(ENGINE_REQUEST_WORKERS.saturating_sub(ENGINE_INTERACTIVE_WORKER_RESERVE));
        self.active_materializations
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |active| {
                (active < maximum).then_some(active + 1)
            })
            .ok()
            .map(|_| ActiveCounterPermit {
                active: &self.active_materializations,
            })
    }

    fn materialization_cache_share(&self, capacity: u64) -> u64 {
        let active = u64::try_from(self.active_materializations.load(Ordering::Acquire).max(1))
            .unwrap_or(u64::MAX);
        capacity / active
    }

    fn preparation_fits_memory_cache(&self, total_size: u64) -> bool {
        let capacity = u64::try_from(
            self.segment_cache
                .lock()
                .expect("segment cache lock")
                .budget(),
        )
        .unwrap_or(u64::MAX);
        total_size <= self.materialization_cache_share(capacity)
    }

    fn preparation_fits_disk_cache(&self, total_size: u64) -> bool {
        let Some(disk_cache) = &self.disk_cache else {
            return false;
        };
        let capacity = disk_cache.lock().expect("disk cache lock").budget();
        total_size <= self.materialization_cache_share(capacity)
    }

    fn read_session_range<F>(
        &self,
        identity: &str,
        revision: &str,
        expected_size: u64,
        start: u64,
        end: u64,
        cancelled: &F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        self.read_session_range_with_class(
            identity,
            revision,
            expected_size,
            start,
            end,
            nntp::WorkClass::Interactive,
            cancelled,
        )
    }

    #[allow(clippy::too_many_arguments)]
    fn read_session_range_with_class<F>(
        &self,
        identity: &str,
        revision: &str,
        expected_size: u64,
        start: u64,
        end: u64,
        work_class: nntp::WorkClass,
        cancelled: &F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        let lease = self
            .sessions
            .lock()
            .expect("session registry lock")
            .get(identity, Instant::now())?;
        if lease.recreation_key.as_ref() != revision {
            return Err("session_revision_conflict");
        }
        if lease
            .session
            .lock()
            .expect("random access session lock")
            .size()
            != expected_size
        {
            return Err("session_size_conflict");
        }
        let length = end
            .checked_sub(start)
            .and_then(|value| value.checked_add(1))
            .ok_or("invalid_session_range")?;
        let mut source = (*lease.context).clone();
        source.work_class = work_class;
        let bytes =
            session::RandomAccessSession::read_at(&lease.session, start, length, |posting| {
                self.fetch_posting(&source, posting, cancelled)
            })?;
        self.persist_session_checkpoints(&lease.recreation_key, &lease.session)?;
        Ok(bytes)
    }

    fn read_composite_part<F>(
        self: &Arc<Self>,
        artifact_data: &Path,
        part: &raw_composite::RawCompositePart,
        start: u64,
        end: u64,
        prefetch_generation: Option<&reader_lease::ReaderGeneration>,
        cancelled: &F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        match &part.backing {
            raw_composite::RawCompositeBacking::Materialization(file_identity) => {
                materialization::read_immutable_range_cancellable(
                    artifact_data,
                    &part.content_identity,
                    file_identity.size,
                    start,
                    end,
                    *file_identity,
                    cancelled,
                )
            }
            raw_composite::RawCompositeBacking::Session {
                identity,
                revision,
                exact_size,
                ..
            } => {
                let bytes = self.read_session_range(
                    identity,
                    revision,
                    *exact_size,
                    start,
                    end,
                    cancelled,
                )?;
                if let Some(generation) = prefetch_generation {
                    let lease = self
                        .sessions
                        .lock()
                        .expect("session registry lock")
                        .get(identity, Instant::now())?;
                    let prefetch_start = lease
                        .session
                        .lock()
                        .expect("random access session lock")
                        .prefetch_start_after(end);
                    if let Some(posting_index) = prefetch_start {
                        self.start_session_prefetch(
                            Arc::clone(&lease.session),
                            Arc::clone(&lease.recreation_key),
                            Arc::clone(&lease.context),
                            generation.clone(),
                            posting_index,
                        );
                    }
                }
                Ok(bytes)
            }
        }
    }
}

struct BackgroundPrefetchPermit {
    state: Arc<EngineState>,
}

struct PostingFailureAggregate {
    last: &'static str,
    auth: Option<&'static str>,
    transient: Option<&'static str>,
    mixed_transient: bool,
    content_evidence: bool,
    all_auth: bool,
    provider_count: usize,
}

impl PostingFailureAggregate {
    fn new(provider_count: usize) -> Self {
        Self {
            last: "nntp_materialization_failed",
            auth: None,
            transient: None,
            mixed_transient: false,
            content_evidence: false,
            all_auth: true,
            provider_count,
        }
    }

    fn observe(&mut self, code: &'static str) {
        self.last = code;
        if matches!(code, "nntp_auth_failed" | "nntp_auth_required") {
            self.auth.get_or_insert(code);
        } else {
            self.all_auth = false;
        }
        if code == "nntp_article_missing" || yenc::integrity_failure(code) {
            self.content_evidence = true;
        } else if let Some(transient) = self.transient {
            self.mixed_transient |= transient != code;
        } else {
            self.transient = Some(code);
        }
    }

    fn finish(self) -> &'static str {
        let Some(transient) = self.transient else {
            return self.last;
        };
        if self.all_auth {
            self.auth
                .expect("inconclusive all-auth aggregate contains an authentication failure")
        } else if self.provider_count == 1
            && !self.content_evidence
            && !self.mixed_transient
            && nntp::retryable_provider_failure(transient)
        {
            transient
        } else {
            "nntp_availability_unknown"
        }
    }
}

impl Drop for BackgroundPrefetchPermit {
    fn drop(&mut self) {
        self.state
            .background_prefetches
            .fetch_sub(1, Ordering::AcqRel);
        release_allocator_slack();
    }
}

impl Drop for EngineState {
    fn drop(&mut self) {
        self.segment_cache
            .get_mut()
            .expect("segment cache lock")
            .purge();
    }
}

fn response(status: &str, body: &str) -> String {
    format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn response_bytes(status: &str, body: &str) -> Vec<u8> {
    use std::io::Write as _;
    let mut out = Vec::with_capacity(status.len() + body.len() + 80);
    let _ = write!(
        out,
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    out.extend_from_slice(body.as_bytes());
    out
}

fn archive_failure_response(code: &str) -> Vec<u8> {
    let retryable = native_failure_retryable(code)
        || matches!(
            code,
            "archive_cancelled"
                | "archive_busy"
                | "archive_runtime_unavailable"
                | "archive_worker_failed"
                | "repair_busy"
                | "repair_cancelled"
                | "repair_disk_exhausted"
                | "repair_io_failed"
                | "repair_memory_exhausted"
                | "repair_runtime_unavailable"
                | "repair_worker_failed"
                | "raw_composite_busy"
                | "raw_composite_cancelled"
                | "raw_composite_capacity"
                | "raw_composite_random_unavailable"
                | "raw_composite_reader_busy"
                | "raw_composite_reader_capacity"
                | "raw_composite_reader_unavailable"
                | "raw_composite_unavailable"
        );
    let status = match code {
        "archive_timed_out" | "repair_timed_out" => "504 Gateway Timeout",
        _ if retryable => "503 Service Unavailable",
        _ => "422 Unprocessable Content",
    };
    response_bytes(status, &failure_body(code, retryable))
}

fn par2_insufficient_response(required_recovery_blocks: usize) -> Vec<u8> {
    response_bytes(
        "422 Unprocessable Content",
        &format!(
            r#"{{"version":1,"code":"repair_insufficient","retryable":false,"required_recovery_blocks":{required_recovery_blocks}}}"#,
        ),
    )
}

fn native_work_failure_response(code: &str) -> Vec<u8> {
    let retryable = native_failure_retryable(code);
    response_bytes(
        if retryable {
            "503 Service Unavailable"
        } else {
            "422 Unprocessable Content"
        },
        &failure_body(code, retryable),
    )
}

fn native_failure_retryable(code: &str) -> bool {
    nntp::retryable_provider_failure(code)
        || matches!(
            code,
            "materialization_cancelled"
                | "materialization_unavailable"
                | "native_busy"
                | "native_disk_pressure"
                | "native_resource_unavailable"
                | "native_spool_full"
                | "nntp_availability_unknown"
                | "nntp_cancelled"
                | "provider_set_busy"
                | "provider_set_capacity"
                | "provider_set_random_unavailable"
                | "provider_set_unavailable"
                | "session_busy"
                | "session_capacity"
                | "session_random_unavailable"
                | "session_reader_capacity"
                | "session_unavailable"
        )
}

fn failure_body(code: &str, retryable: bool) -> String {
    let r = if retryable { "true" } else { "false" };
    format!(r#"{{"version":1,"code":"{code}","retryable":{r}}}"#)
}

fn range_response(
    body: Vec<u8>,
    start: u64,
    end: u64,
    expected_size: u64,
    salvage: Option<(bool, u64, usize)>,
    sink: &mut Option<Vec<u8>>,
) -> Vec<u8> {
    use std::io::Write as _;
    let mut response = Vec::with_capacity(180);
    let _ = write!(
        response,
        "HTTP/1.1 206 Partial Content\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nContent-Range: bytes {start}-{end}/{expected_size}\r\n",
        body.len()
    );
    if let Some((degraded, salvaged_bytes, salvaged_holes)) = salvage {
        let state = if degraded { "zero-fill" } else { "none" };
        let _ = write!(
            response,
            "X-Comet-Usenet-Salvage: {state}\r\nX-Comet-Usenet-Salvaged-Bytes: {salvaged_bytes}\r\nX-Comet-Usenet-Salvaged-Holes: {salvaged_holes}\r\n"
        );
    }
    response.extend_from_slice(b"Connection: close\r\n\r\n");
    *sink = Some(body);
    response
}

fn peer_is_current_user(stream: &UnixStream) -> bool {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut credentials as *mut libc::ucred).cast(),
            &mut length,
        )
    };
    result == 0 && credentials.uid == unsafe { libc::getuid() }
}

fn peer_disconnected(stream: &UnixStream) -> bool {
    let mut descriptor = libc::pollfd {
        fd: stream.as_raw_fd(),
        events: libc::POLLOUT,
        revents: 0,
    };
    let result = unsafe { libc::poll(std::ptr::addr_of_mut!(descriptor), 1, 0) };
    result < 0
        || (result > 0
            && descriptor.revents & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0)
}

fn parse_nzb_with_metadata_limit(
    body: &[u8],
    maximum_metadata_bytes: usize,
) -> Result<String, &'static str> {
    if body.len() > MAX_NZB_BYTES {
        return Err("nzb_too_large");
    }
    let manifest = nzb::parse(body)?;
    let segments: usize = manifest.files.iter().map(|file| file.postings.len()).sum();
    let payload = serde_json::to_string(&NzbParseResponse {
        version: 2u32,
        files: manifest.files.len(),
        segments,
        nh1: &manifest.nh1,
        nm1: &manifest.nm1,
        metadata: &manifest.metadata,
        manifest: &manifest.files,
    })
    .map_err(|_| "nzb_serialization_failed")?;
    if payload.len() > maximum_metadata_bytes {
        return Err("nzb_metadata_too_large");
    }
    Ok(payload)
}

fn parse_nzb(body: &[u8]) -> Result<String, &'static str> {
    parse_nzb_with_metadata_limit(body, MAX_NZB_METADATA_BYTES)
}

fn is_artifact_parse_request(request: &str) -> bool {
    let Some(artifact) = request
        .strip_prefix("POST /v1/artifacts/")
        .and_then(|value| value.strip_suffix("/parse HTTP/1.1"))
    else {
        return false;
    };
    valid_sha256(artifact)
}

fn artifact_native_inspect_identity(request: &str) -> Option<&str> {
    let artifact = request
        .strip_prefix("POST /v1/artifacts/")?
        .strip_suffix("/native-inspect HTTP/1.1")?;
    valid_sha256(artifact).then_some(artifact)
}

fn artifact_native_catalog_identity(request: &str) -> Option<&str> {
    let artifact = request
        .strip_prefix("POST /v1/artifacts/")?
        .strip_suffix("/native-catalog HTTP/1.1")?;
    valid_sha256(artifact).then_some(artifact)
}

fn materialization_native_inspect_identity(request: &str) -> Option<&str> {
    let identity = request
        .strip_prefix("POST /v1/materializations/")?
        .strip_suffix("/native-inspect HTTP/1.1")?;
    valid_sha256(identity).then_some(identity)
}

fn is_materialization_request(request: &str) -> bool {
    request == "POST /v1/materializations HTTP/1.1"
}

fn is_archive_plan_request(request: &str) -> bool {
    request == "POST /v1/archive-plan HTTP/1.1"
}

fn is_archive_nested_catalog_request(request: &str) -> bool {
    request == "POST /v1/archive-nested/catalog HTTP/1.1"
}

fn is_archive_nested_extraction_request(request: &str) -> bool {
    request == "POST /v1/archive-nested/extract HTTP/1.1"
}

fn is_archive_direct_catalog_request(request: &str) -> bool {
    request == "POST /v1/archive-direct/catalog HTTP/1.1"
}

fn is_archive_direct_open_request(request: &str) -> bool {
    request == "POST /v1/archive-direct/open HTTP/1.1"
}

fn is_session_archive_catalog_request(request: &str) -> bool {
    request == "POST /v1/session-archives/catalog HTTP/1.1"
}

fn is_session_archive_open_request(request: &str) -> bool {
    request == "POST /v1/session-archives/open HTTP/1.1"
}

fn is_par2_discovery_request(request: &str) -> bool {
    request == "POST /v1/par2/discover HTTP/1.1"
}

fn is_par2_source_map_request(request: &str) -> bool {
    request == "POST /v1/par2/map-sources HTTP/1.1"
}

fn is_par2_repair_request(request: &str) -> bool {
    request == "POST /v1/par2/repair HTTP/1.1"
}

fn is_raw_composite_open_request(request: &str) -> bool {
    request == "POST /v1/raw-composites HTTP/1.1"
}

fn is_session_request(request: &str) -> bool {
    request == "POST /v1/sessions HTTP/1.1"
}

fn provider_set_put_generation(request: &str) -> Option<&str> {
    let generation = request
        .strip_prefix("PUT /v1/provider-sets/")?
        .strip_suffix(" HTTP/1.1")?;
    provider::valid_generation(generation).then_some(generation)
}

fn is_native_work_request(request: &str) -> bool {
    is_materialization_request(request)
        || is_archive_plan_request(request)
        || is_archive_nested_catalog_request(request)
        || is_archive_nested_extraction_request(request)
        || is_archive_direct_catalog_request(request)
        || is_archive_direct_open_request(request)
        || is_session_archive_catalog_request(request)
        || is_session_archive_open_request(request)
        || is_par2_discovery_request(request)
        || is_par2_source_map_request(request)
        || is_par2_repair_request(request)
        || is_raw_composite_open_request(request)
        || raw_composite_reader_open_identity(request).is_some()
        || raw_composite_reader_close_identities(request).is_some()
        || raw_composite_read_identity(request).is_some()
        || raw_composite_native_inspect_identity(request).is_some()
        || materialization_native_inspect_identity(request).is_some()
        || artifact_native_inspect_identity(request).is_some()
        || is_session_request(request)
        || session_reader_open_identity(request).is_some()
        || session_reader_close_identities(request).is_some()
        || session_read_identity(request).is_some()
        || provider_set_put_generation(request).is_some()
}

fn raw_composite_read_identity(request: &str) -> Option<&str> {
    let identity = request
        .strip_prefix("POST /v1/raw-composites/")?
        .strip_suffix("/read HTTP/1.1")?;
    valid_sha256(identity).then_some(identity)
}

fn raw_composite_reader_open_identity(request: &str) -> Option<&str> {
    let identity = request
        .strip_prefix("POST /v1/raw-composites/")?
        .strip_suffix("/readers HTTP/1.1")?;
    valid_sha256(identity).then_some(identity)
}

fn raw_composite_reader_close_identities(request: &str) -> Option<(&str, &str)> {
    let path = request
        .strip_prefix("DELETE /v1/raw-composites/")?
        .strip_suffix(" HTTP/1.1")?;
    let (identity, lease_id) = path.split_once("/readers/")?;
    (valid_sha256(identity) && session::valid_session_id(lease_id)).then_some((identity, lease_id))
}

fn raw_composite_native_inspect_identity(request: &str) -> Option<&str> {
    let identity = request
        .strip_prefix("POST /v1/raw-composites/")?
        .strip_suffix("/native-inspect HTTP/1.1")?;
    valid_sha256(identity).then_some(identity)
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn lower_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = vec![0u8; bytes.len() * 2];
    for (i, byte) in bytes.iter().enumerate() {
        out[2 * i] = HEX[(byte >> 4) as usize];
        out[2 * i + 1] = HEX[(byte & 0x0f) as usize];
    }
    String::from_utf8(out).expect("hex encoding is valid UTF-8")
}

fn validate_par2_files(files: &mut Vec<ArchivePlanVolumeRequest>) -> Result<(), &'static str> {
    if !(1..=nzb::MAX_FILES).contains(&files.len()) {
        return Err("par2_input_invalid");
    }
    files.sort_by_cached_key(|file| {
        (
            file.relative_path.to_lowercase(),
            file.relative_path.clone(),
            file.content_identity.clone(),
        )
    });
    let mut identities = BTreeSet::new();
    let mut paths = BTreeSet::new();
    let mut total = 0u64;
    for file in files {
        let normalized = archive::normalize_archive_path(&file.relative_path)
            .map_err(|_| "par2_file_name_invalid")?;
        if normalized != file.relative_path
            || !valid_sha256(&file.content_identity)
            || file.expected_size == 0
            || !identities.insert(file.content_identity.clone())
            || !paths.insert(normalized.to_lowercase())
        {
            return Err("par2_input_invalid");
        }
        total = total
            .checked_add(file.expected_size)
            .filter(|value| *value <= archive_group::MAX_LOGICAL_BYTES)
            .ok_or("par2_input_invalid")?;
    }
    Ok(())
}

fn canonical_par2_volume_identities(
    files: &[ArchivePlanVolumeRequest],
    input_indices: Vec<usize>,
) -> Result<Vec<String>, &'static str> {
    let mut identities = input_indices
        .into_iter()
        .map(|index| {
            files
                .get(index)
                .map(|file| file.content_identity.clone())
                .ok_or("par2_catalog_invalid")
        })
        .collect::<Result<Vec<_>, _>>()?;
    identities.sort_unstable();
    Ok(identities)
}

fn validate_par2_sources(
    sources: &mut Vec<ArchivePlanVolumeRequest>,
    allow_empty: bool,
) -> Result<(), &'static str> {
    if sources.len() > nzb::MAX_FILES || (!allow_empty && sources.is_empty()) {
        return Err("par2_source_invalid");
    }
    sources.sort_by(|left, right| {
        left.content_identity
            .cmp(&right.content_identity)
            .then_with(|| left.relative_path.cmp(&right.relative_path))
    });
    let mut identities = BTreeSet::new();
    let mut paths = BTreeSet::new();
    let mut total = 0u64;
    for source in sources {
        let normalized = archive::normalize_archive_path(&source.relative_path)
            .map_err(|_| "par2_source_invalid")?;
        if normalized != source.relative_path
            || !valid_sha256(&source.content_identity)
            || source.expected_size == 0
            || !identities.insert(source.content_identity.clone())
            || !paths.insert(normalized.to_lowercase())
        {
            return Err("par2_source_invalid");
        }
        total = total
            .checked_add(source.expected_size)
            .ok_or("par2_source_invalid")?;
        if total > archive_group::MAX_LOGICAL_BYTES {
            return Err("par2_source_invalid");
        }
    }
    Ok(())
}

struct SelectedPar2Inputs {
    readers: Vec<fs::File>,
    ranges: par2::RecoveryPacketRanges,
    exact_size: u64,
}

fn read_par2_selected_set<F>(
    files: &[ArchivePlanVolumeRequest],
    local_data: &Path,
    selected_set_id: Option<par2::RecoverySetId>,
    cancelled: &F,
) -> Result<(par2::RecoverySet, SelectedPar2Inputs), &'static str>
where
    F: Fn() -> bool,
{
    let mut inputs = files
        .iter()
        .map(|file| {
            materialization::verified_reader_cancellable(
                local_data,
                &file.content_identity,
                file.expected_size,
                cancelled,
            )
        })
        .collect::<Result<Vec<_>, &'static str>>()?;
    let set_id = match selected_set_id {
        Some(set_id) => set_id,
        None => {
            let discovered = par2::discover_recovery_sets_from_readers(&mut inputs, cancelled)?;
            if discovered.len() != 1 {
                return Err("par2_recovery_set_missing");
            }
            discovered[0].set.set_id
        }
    };
    let (set, ranges) = par2::parse_recovery_readers_for_set(&mut inputs, set_id, cancelled)?;
    let exact_size = ranges
        .iter()
        .flatten()
        .try_fold(0u64, |total, (_offset, length)| {
            total
                .checked_add(*length)
                .ok_or("repair_scope_exceeds_budget")
        })?;
    Ok((
        set,
        SelectedPar2Inputs {
            readers: inputs,
            ranges,
            exact_size,
        },
    ))
}

fn read_par2_sets<F>(
    files: &[ArchivePlanVolumeRequest],
    local_data: &Path,
    cancelled: &F,
) -> Result<Vec<par2::DiscoveredRecoverySet>, &'static str>
where
    F: Fn() -> bool,
{
    let mut inputs = files
        .iter()
        .map(|file| {
            materialization::verified_reader_cancellable(
                local_data,
                &file.content_identity,
                file.expected_size,
                cancelled,
            )
        })
        .collect::<Result<Vec<_>, &'static str>>()?;
    par2::discover_recovery_sets_from_readers(&mut inputs, cancelled)
}

fn par2_catalog_object(
    set: &par2::RecoverySet,
) -> Result<serde_json::Map<String, serde_json::Value>, &'static str> {
    let files = set
        .source_file_ids
        .iter()
        .map(|file_id| {
            let description = set
                .files
                .get(file_id)
                .ok_or("par2_file_description_missing")?;
            let slice_count = set.checksums.get(file_id).ok_or("par2_ifsc_missing")?.len();
            Ok(serde_json::json!({
                "file_id": lower_hex(file_id),
                "relative_path": description.relative_path,
                "exact_size": description.byte_size,
                "full_md5": lower_hex(&description.full_md5),
                "first_16k_md5": lower_hex(&description.first_16k_md5),
                "slice_count": slice_count,
            }))
        })
        .collect::<Result<Vec<_>, &'static str>>()?;
    Ok(serde_json::Map::from_iter([
        (
            "set_id".to_owned(),
            serde_json::json!(lower_hex(&set.set_id)),
        ),
        ("slice_size".to_owned(), serde_json::json!(set.slice_size)),
        ("files".to_owned(), serde_json::json!(files)),
        (
            "recovery_exponents".to_owned(),
            serde_json::json!(set.recovery_exponents),
        ),
    ]))
}

fn par2_file_id(value: &str) -> Result<par2::FileId, &'static str> {
    parse_par2_id(value).map_err(|_| "par2_file_not_in_recovery_set")
}

fn par2_set_id(value: &str) -> Result<par2::RecoverySetId, &'static str> {
    parse_par2_id(value).map_err(|_| "par2_recovery_set_missing")
}

fn parse_par2_id(value: &str) -> Result<[u8; 16], ()> {
    if value.len() != 32
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(());
    }
    let mut decoded = [0u8; 16];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        let digit = |byte: u8| -> u8 {
            match byte {
                b'0'..=b'9' => byte - b'0',
                b'a'..=b'f' => byte - b'a' + 10,
                _ => unreachable!("validated lowercase hexadecimal"),
            }
        };
        decoded[index] = digit(pair[0]) << 4 | digit(pair[1]);
    }
    Ok(decoded)
}

fn session_read_identity(request: &str) -> Option<&str> {
    let identity = request
        .strip_prefix("POST /v1/sessions/")?
        .strip_suffix("/read HTTP/1.1")?;
    session::valid_session_id(identity).then_some(identity)
}

fn session_reader_open_identity(request: &str) -> Option<&str> {
    let identity = request
        .strip_prefix("POST /v1/sessions/")?
        .strip_suffix("/readers HTTP/1.1")?;
    session::valid_session_id(identity).then_some(identity)
}

fn session_reader_close_identities(request: &str) -> Option<(&str, &str)> {
    let path = request
        .strip_prefix("DELETE /v1/sessions/")?
        .strip_suffix(" HTTP/1.1")?;
    let (identity, lease_id) = path.split_once("/readers/")?;
    (session::valid_session_id(identity) && session::valid_session_id(lease_id))
        .then_some((identity, lease_id))
}

fn maximum_request_body(request: &str) -> usize {
    if provider_set_put_generation(request).is_some() {
        MAX_PROVIDER_SET_REQUEST_BYTES
    } else if artifact_native_catalog_identity(request).is_some()
        || artifact_native_inspect_identity(request).is_some()
        || is_materialization_request(request)
        || is_session_request(request)
        || is_archive_plan_request(request)
        || is_archive_nested_catalog_request(request)
        || is_archive_nested_extraction_request(request)
        || is_archive_direct_catalog_request(request)
        || is_archive_direct_open_request(request)
        || is_session_archive_catalog_request(request)
        || is_session_archive_open_request(request)
        || is_par2_discovery_request(request)
        || is_par2_source_map_request(request)
        || is_par2_repair_request(request)
        || is_raw_composite_open_request(request)
    {
        MAX_NZB_METADATA_BYTES
    } else if raw_composite_reader_open_identity(request).is_some()
        || raw_composite_reader_close_identities(request).is_some()
        || raw_composite_read_identity(request).is_some()
        || raw_composite_native_inspect_identity(request).is_some()
        || materialization_native_inspect_identity(request).is_some()
        || session_reader_open_identity(request).is_some()
        || session_reader_close_identities(request).is_some()
        || session_read_identity(request).is_some()
    {
        MAX_CONTROL_REQUEST_BYTES
    } else if is_artifact_parse_request(request) {
        MAX_NZB_BYTES
    } else {
        0
    }
}

fn request_releases_allocator_slack(request: &str, content_length: usize) -> bool {
    is_materialization_request(request) || content_length >= MAX_CONTROL_REQUEST_BYTES
}

#[cfg(test)]
fn connection_test_checks(failed_phase: Option<&str>) -> serde_json::Value {
    let failed_index = failed_phase.and_then(|failed| {
        CONNECTION_TEST_PHASES
            .iter()
            .position(|phase| *phase == failed)
    });
    serde_json::Value::Array(
        CONNECTION_TEST_PHASES
            .iter()
            .enumerate()
            .map(|(index, phase)| {
                let status = match failed_index {
                    None => "passed",
                    Some(failed) if index < failed => "passed",
                    Some(failed) if index == failed => "failed",
                    Some(_) => "not_run",
                };
                serde_json::json!({"name": phase, "status": status})
            })
            .collect(),
    )
}

#[cfg(test)]
fn provider_test_deadline_result(provider_configuration_id: &str) -> serde_json::Value {
    serde_json::json!({
        "provider_configuration_id": provider_configuration_id,
        "ok": false,
        "checks": CONNECTION_TEST_PHASES
            .iter()
            .map(|phase| serde_json::json!({"name": phase, "status": "not_run"}))
            .collect::<Vec<_>>(),
        "code": "nntp_provider_test_deadline",
    })
}

#[cfg(test)]
fn provider_test_results(
    pools: &Arc<nntp::PoolRegistry>,
    provider_set: &Arc<provider::ProviderSet>,
) -> Vec<serde_json::Value> {
    provider_test_results_until(pools, provider_set, Instant::now() + PROVIDER_TEST_DEADLINE)
}

#[cfg(test)]
fn provider_test_results_until(
    pools: &Arc<nntp::PoolRegistry>,
    provider_set: &Arc<provider::ProviderSet>,
    deadline: Instant,
) -> Vec<serde_json::Value> {
    use std::sync::atomic::{AtomicUsize, Ordering};

    let next = AtomicUsize::new(0);
    let results = Mutex::new(vec![None; provider_set.servers.len()]);
    thread::scope(|scope| {
        for _ in 0..provider_set.servers.len().min(PROVIDER_TEST_WORKERS) {
            scope.spawn(|| {
                loop {
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    let Some((server, reference)) = provider_set
                        .servers
                        .get(index)
                        .zip(provider_set.pool_references.get(index))
                    else {
                        break;
                    };
                    let result = if Instant::now() >= deadline {
                        provider_test_deadline_result(&server.provider_configuration_id)
                    } else {
                        match pools.test_reference_cancellable(reference, &server.request, &|| {
                            Instant::now() >= deadline
                        }) {
                            Ok(date) => serde_json::json!({
                                "provider_configuration_id": server.provider_configuration_id,
                                "ok": true,
                                "checks": connection_test_checks(None),
                                "date": date,
                            }),
                            Err(error) if error.code == "nntp_cancelled" => {
                                provider_test_deadline_result(&server.provider_configuration_id)
                            }
                            Err(error) => serde_json::json!({
                                "provider_configuration_id": server.provider_configuration_id,
                                "ok": false,
                                "checks": connection_test_checks(Some(error.phase)),
                                "code": error.code,
                            }),
                        }
                    };
                    results.lock().expect("provider test results lock")[index] = Some(result);
                }
            });
        }
    });
    results
        .into_inner()
        .expect("provider test results lock")
        .into_iter()
        .map(|result| result.expect("provider test worker result"))
        .collect()
}

fn plan_archive_request<F>(
    mut request: ArchivePlanRequest,
    local_data: &Path,
    verification_cache: Option<&Mutex<VerifiedMaterializationCache>>,
    cancelled: &F,
) -> Result<
    (
        archive_group::VolumePlan,
        BTreeMap<String, materialization::ImmutableFileIdentity>,
    ),
    &'static str,
>
where
    F: Fn() -> bool,
{
    if request.volumes.is_empty() || request.volumes.len() > archive_group::MAX_VOLUMES {
        return Err("archive_volume_budget");
    }
    let mut exact_size = 0_u64;
    let mut identities = BTreeSet::new();
    let mut paths = BTreeSet::new();
    for volume in &mut request.volumes {
        exact_size = exact_size
            .checked_add(volume.expected_size)
            .ok_or("archive_volume_budget")?;
        if exact_size > archive_group::MAX_LOGICAL_BYTES {
            return Err("archive_volume_budget");
        }
        if volume.expected_size == 0 || !valid_sha256(&volume.content_identity) {
            return Err("archive_volume_invalid");
        }
        let normalized = archive::normalize_archive_path(&volume.relative_path)?;
        if !identities.insert(volume.content_identity.clone())
            || !paths.insert(normalized.to_lowercase())
        {
            return Err("archive_volume_conflict");
        }
        volume.relative_path = normalized;
    }
    let mut samples = Vec::with_capacity(request.volumes.len());
    let mut file_identities = BTreeMap::new();
    let sample_bytes = archive_group::sample_bytes_per_volume(request.volumes.len());
    for volume in request.volumes {
        let trusted_identity = verification_cache.and_then(|cache| {
            cache
                .lock()
                .expect("verified materialization cache lock")
                .get(&volume.content_identity)
        });
        let (head, tail, file_identity) = materialization::verified_samples_trusted_cancellable(
            local_data,
            &volume.content_identity,
            volume.expected_size,
            sample_bytes,
            trusted_identity,
            cancelled,
        )?;
        if let Some(cache) = verification_cache {
            cache
                .lock()
                .expect("verified materialization cache lock")
                .insert(volume.content_identity.clone(), file_identity);
        }
        file_identities.insert(volume.content_identity.clone(), file_identity);
        samples.push((volume, head, tail));
    }
    let inputs = samples
        .iter()
        .map(|(volume, head, tail)| archive_group::VolumeInput {
            content_identity: &volume.content_identity,
            relative_path: &volume.relative_path,
            exact_size: volume.expected_size,
            head,
            tail,
        })
        .collect::<Vec<_>>();
    archive_group::plan_volumes(&inputs).map(|plan| (plan, file_identities))
}

fn validated_archive_passphrase(
    passphrase: Option<String>,
) -> Result<Option<Zeroizing<String>>, &'static str> {
    match passphrase {
        None => Ok(None),
        Some(passphrase)
            if passphrase.len() <= archive_runtime::MAX_ARCHIVE_PASSPHRASE_BYTES
                && !passphrase.contains('\0') =>
        {
            Ok(Some(Zeroizing::new(passphrase)))
        }
        Some(_) => Err("archive_passphrase_invalid"),
    }
}

fn verified_archive_parts<'a>(
    plan: &'a archive_group::VolumePlan,
    file_identities: &'a BTreeMap<String, materialization::ImmutableFileIdentity>,
) -> Vec<materialization::VerifiedMaterializationPart<'a>> {
    plan.volumes
        .iter()
        .map(|volume| materialization::VerifiedMaterializationPart {
            content_identity: &volume.content_identity,
            exact_size: volume.exact_size,
            file_identity: *file_identities
                .get(&volume.content_identity)
                .expect("planned materialization identity"),
        })
        .collect()
}

fn parse_stored_direct_members<F>(
    plan: &archive_group::VolumePlan,
    file_identities: &BTreeMap<String, materialization::ImmutableFileIdentity>,
    local_data: &Path,
    cancelled: &F,
) -> Result<Vec<rar_stored::StoredMember>, &'static str>
where
    F: Fn() -> bool,
{
    let format = match plan.kind {
        archive_group::VolumePlanKind::SingleArchive(format)
        | archive_group::VolumePlanKind::MultiVolumeArchive(format)
            if matches!(
                format,
                archive::ArchiveFormat::Rar4 | archive::ArchiveFormat::Rar5
            ) =>
        {
            format
        }
        _ => return Err("archive_direct_unsupported"),
    };
    let volume_sizes = plan
        .volumes
        .iter()
        .map(|volume| volume.exact_size)
        .collect::<Vec<_>>();
    let mut readers = plan
        .volumes
        .iter()
        .map(|volume| {
            materialization::ImmutableRangeReader::open(
                local_data,
                &volume.content_identity,
                volume.exact_size,
                *file_identities
                    .get(&volume.content_identity)
                    .expect("planned materialization identity"),
            )
        })
        .collect::<Result<Vec<_>, &'static str>>()?;
    let read_range = |volume_index: usize, offset: u64, length: usize| {
        let end = offset
            .checked_add(u64::try_from(length).map_err(|_| "archive_header_invalid")?)
            .and_then(|end| end.checked_sub(1))
            .ok_or("archive_header_invalid")?;
        readers
            .get_mut(volume_index)
            .ok_or("archive_volume_conflict")?
            .read_range_cancellable(offset, end, cancelled)
    };
    let members = match format {
        archive::ArchiveFormat::Rar4 => {
            rar_stored::parse_rar4_stored_members(&volume_sizes, read_range)?
        }
        archive::ArchiveFormat::Rar5 => {
            rar_stored::parse_rar5_stored_members(&volume_sizes, read_range)?
        }
        _ => unreachable!("RAR format checked above"),
    };
    for reader in &readers {
        reader.revalidate()?;
    }
    Ok(members)
}

struct SessionArchiveVolume {
    session_id: String,
    revision: String,
    relative_path: String,
    exact_size: u64,
    retention: session::SessionRetention,
    evidence: archive::ArchiveEvidence,
}

struct SessionArchiveProbeVolume {
    session_id: String,
    revision: String,
    relative_path: String,
    exact_size: u64,
    retention: session::SessionRetention,
}

fn observe_archive_probe_failure(observed: &mut Option<&'static str>, code: &'static str) {
    if observed.is_none()
        || observed.is_some_and(|current| current == "nntp_cancelled" && code != "nntp_cancelled")
    {
        *observed = Some(code);
    }
}

fn probe_session_archive_volume<F>(
    volume: &SessionArchiveProbeVolume,
    state: &EngineState,
    cancelled: &F,
) -> Result<archive::ArchiveEvidence, &'static str>
where
    F: Fn() -> bool,
{
    const INITIAL_HEADER_BYTES: u64 = 4 * 1024;
    const MAX_HEADER_BYTES: u64 = 2 * 1024 * 1024;

    let maximum = volume.exact_size.min(MAX_HEADER_BYTES);
    let mut length = maximum.min(INITIAL_HEADER_BYTES);
    let evidence = loop {
        let head = state.read_session_range_with_class(
            &volume.session_id,
            &volume.revision,
            volume.exact_size,
            0,
            length.checked_sub(1).ok_or("archive_header_incomplete")?,
            nntp::WorkClass::Preparation,
            cancelled,
        )?;
        match archive::detect_archive(&head) {
            Ok(Some(evidence)) => break evidence,
            Ok(None) => return Err("archive_direct_unsupported"),
            Err("archive_header_incomplete") if length < maximum => {
                length = (length * 2).min(maximum);
            }
            Err(code) => return Err(code),
        }
    };
    Ok(evidence)
}

fn plan_session_archive<F>(
    request: Vec<SessionArchiveVolumeRequest>,
    state: &EngineState,
    cancelled: &F,
) -> Result<(archive_group::VolumePlan, Vec<SessionArchiveVolume>), &'static str>
where
    F: Fn() -> bool + Sync,
{
    if request.is_empty() || request.len() > archive_group::MAX_VOLUMES {
        return Err("archive_volume_budget");
    }
    let mut total_size = 0_u64;
    let mut session_ids = BTreeSet::new();
    let mut revisions = BTreeSet::new();
    let mut paths = BTreeSet::new();
    let mut probes = Vec::with_capacity(request.len());
    for volume in request {
        total_size = total_size
            .checked_add(volume.expected_size)
            .ok_or("archive_volume_budget")?;
        let relative_path = archive::normalize_archive_path(&volume.relative_path)?;
        if volume.expected_size == 0
            || total_size > archive_group::MAX_LOGICAL_BYTES
            || !session::valid_session_id(&volume.session_id)
            || !valid_sha256(&volume.revision)
            || !session_ids.insert(volume.session_id.clone())
            || !revisions.insert(volume.revision.clone())
            || !paths.insert(relative_path.to_lowercase())
        {
            return Err("archive_volume_conflict");
        }
        let lease = state
            .sessions
            .lock()
            .expect("session registry lock")
            .get(&volume.session_id, Instant::now())?;
        let session_size = lease
            .session
            .lock()
            .expect("random access session lock")
            .size();
        if lease.recreation_key.as_ref() != volume.revision || session_size != volume.expected_size
        {
            return Err("archive_volume_conflict");
        }
        let retention = lease.retention.clone();
        drop(lease);

        probes.push(SessionArchiveProbeVolume {
            session_id: volume.session_id,
            revision: volume.revision,
            relative_path,
            exact_size: volume.expected_size,
            retention,
        });
    }
    let next = AtomicUsize::new(0);
    let failed = AtomicBool::new(false);
    let failure = Mutex::new(None);
    let completed = Mutex::new(Vec::with_capacity(probes.len()));
    thread::scope(|scope| {
        for _ in 0..probes.len().min(state.nntp_pools.preparation_slots()) {
            scope.spawn(|| {
                loop {
                    if failed.load(Ordering::Acquire) {
                        return;
                    }
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    let Some(probe) = probes.get(index) else {
                        return;
                    };
                    match probe_session_archive_volume(probe, state, cancelled) {
                        Ok(evidence) => completed
                            .lock()
                            .expect("archive header completion lock")
                            .push((index, evidence)),
                        Err(code) => {
                            let mut observed = failure.lock().expect("archive header failure lock");
                            observe_archive_probe_failure(&mut observed, code);
                            failed.store(true, Ordering::Release);
                            return;
                        }
                    }
                }
            });
        }
    });
    if let Some(code) = *failure.lock().expect("archive header failure lock") {
        return Err(code);
    }
    let mut completed = completed
        .into_inner()
        .expect("archive header completion lock");
    debug_assert_eq!(completed.len(), probes.len());
    completed.sort_unstable_by_key(|(index, _evidence)| *index);
    let volumes = probes
        .into_iter()
        .zip(completed)
        .enumerate()
        .map(|(expected_index, (probe, (index, evidence)))| {
            debug_assert_eq!(index, expected_index);
            SessionArchiveVolume {
                session_id: probe.session_id,
                revision: probe.revision,
                relative_path: probe.relative_path,
                exact_size: probe.exact_size,
                retention: probe.retention,
                evidence,
            }
        })
        .collect::<Vec<_>>();
    let inputs = volumes
        .iter()
        .map(|volume| archive_group::RarHeaderVolumeInput {
            content_identity: &volume.revision,
            relative_path: &volume.relative_path,
            exact_size: volume.exact_size,
            evidence: volume.evidence,
        })
        .collect::<Vec<_>>();
    let plan = archive_group::plan_rar_headers(&inputs)?;
    let mut by_revision = volumes
        .into_iter()
        .map(|volume| (volume.revision.clone(), volume))
        .collect::<BTreeMap<_, _>>();
    let ordered = plan
        .volumes
        .iter()
        .map(|volume| {
            by_revision
                .remove(&volume.content_identity)
                .ok_or("archive_volume_conflict")
        })
        .collect::<Result<Vec<_>, _>>()?;
    if !by_revision.is_empty() {
        return Err("archive_volume_conflict");
    }
    Ok((plan, ordered))
}

fn parse_session_stored_members<F>(
    plan: &archive_group::VolumePlan,
    volumes: &[SessionArchiveVolume],
    state: &EngineState,
    cancelled: &F,
) -> Result<Vec<rar_stored::StoredMember>, &'static str>
where
    F: Fn() -> bool + Sync,
{
    let format = match plan.kind {
        archive_group::VolumePlanKind::SingleArchive(format)
        | archive_group::VolumePlanKind::MultiVolumeArchive(format)
            if matches!(
                format,
                archive::ArchiveFormat::Rar4 | archive::ArchiveFormat::Rar5
            ) =>
        {
            format
        }
        _ => return Err("archive_direct_unsupported"),
    };
    let sizes = volumes
        .iter()
        .map(|volume| volume.exact_size)
        .collect::<Vec<_>>();
    let read = |volume_index: usize, offset: u64, length: usize| {
        let volume = volumes.get(volume_index).ok_or("archive_volume_conflict")?;
        let end = offset
            .checked_add(u64::try_from(length).map_err(|_| "archive_header_invalid")?)
            .and_then(|value| value.checked_sub(1))
            .ok_or("archive_header_invalid")?;
        state.read_session_range_with_class(
            &volume.session_id,
            &volume.revision,
            volume.exact_size,
            offset,
            end,
            nntp::WorkClass::Preparation,
            cancelled,
        )
    };
    match format {
        archive::ArchiveFormat::Rar4 => rar_stored::parse_rar4_stored_members(&sizes, read),
        archive::ArchiveFormat::Rar5 => rar_stored::parse_rar5_stored_members(&sizes, read),
        _ => unreachable!("RAR format checked above"),
    }
}

fn stored_archive_catalog(
    plan: &archive_group::VolumePlan,
    members: Vec<rar_stored::StoredMember>,
) -> Result<Vec<ArchiveCatalogMember>, &'static str> {
    let mut members = members
        .into_iter()
        .filter_map(|member| {
            let kind = inspect::classify_path(&member.relative_path)?;
            Some((member, kind))
        })
        .collect::<Vec<_>>();
    members.sort_by_cached_key(|(member, _)| {
        (
            member.relative_path.to_lowercase(),
            member.relative_path.clone(),
        )
    });
    members
        .into_iter()
        .map(|(member, kind)| {
            Ok(ArchiveCatalogMember {
                member_id: archive_group::member_identity(
                    &plan.set_identity,
                    &member.relative_path,
                    member.exact_size,
                )?,
                relative_path: member.relative_path,
                exact_size: member.exact_size,
                kind,
            })
        })
        .collect()
}

#[derive(serde::Serialize)]
struct RuntimeStatsSnapshot {
    version: u32,
    draining: bool,
    requests_active: usize,
    request_workers: usize,
    request_body_reserved_bytes: usize,
    request_body_limit_bytes: usize,
    request_body_busy_rejections_total: u64,
    request_queue_busy_rejections_total: u64,
    sessions: usize,
    session_prefetches_active: usize,
    nntp_hedges_active: usize,
    nntp_hedges_started_total: u64,
    nntp_hedges_won_total: u64,
    nntp_salvage_holes_total: u64,
    nntp_salvage_bytes_total: u64,
    raw_composites: usize,
    provider_sets: usize,
    segment_cache_entries: usize,
    segment_cache_bytes: usize,
    disk_cache_mappings: usize,
    disk_cache_blobs: usize,
    disk_cache_bytes: u64,
    disk_cache_stats_available: bool,
    spool_stats_available: bool,
    spool_resident_bytes: u64,
    spool_reserved_bytes: u64,
    archive_jobs_active: usize,
    repair_jobs_active: usize,
    archive_busy_rejections_total: u64,
    repair_busy_rejections_total: u64,
    spool_rejections_total: u64,
    negative_cache_entries: usize,
    network_singleflight_active: usize,
    nntp_pools: usize,
    nntp_connections_open: usize,
    nntp_connections_active: usize,
    nntp_connections_idle: usize,
    nntp_queue_interactive: usize,
    nntp_queue_preparation: usize,
    nntp_queue_background: usize,
    nntp_preparation_slots: u64,
    nntp_reserved_commands: usize,
    nntp_reserved_encoded_bytes: u64,
    nntp_reserved_decoded_bytes: u64,
    nntp_scheduler_busy_rejections_total: u64,
    nntp_connections_poisoned: u64,
    nntp_provider_attempts_total: u64,
    nntp_provider_suppliers_total: u64,
    nntp_provider_hits_total: u64,
    nntp_provider_missing_total: u64,
    nntp_provider_corrupt_total: u64,
    nntp_provider_failures_total: u64,
    nntp_provider_cancellations_total: u64,
    nntp_provider_failovers_total: u64,
}

fn runtime_stats_body(state: &EngineState) -> String {
    let segment_stats = state
        .segment_cache
        .lock()
        .expect("segment cache lock")
        .stats();
    let empty_disk_stats = || cache::DiskCacheStats {
        mappings: 0,
        blobs: 0,
        used_bytes: 0,
    };
    let (disk_cache_stats_available, disk_stats) = match &state.disk_cache {
        None => (true, empty_disk_stats()),
        Some(cache) => match cache.lock().expect("disk cache lock").stats() {
            Ok(stats) => (true, stats),
            Err(_) => (false, empty_disk_stats()),
        },
    };
    let pool_stats = state.nntp_pools.stats();
    let resource_stats = state.resources.stats();
    let resource_stats_available = resource_stats.is_ok();
    let resource_stats = resource_stats.unwrap_or(resources::Stats {
        resident_bytes: 0,
        reserved_bytes: 0,
        archive_jobs_active: 0,
        repair_jobs_active: 0,
        archive_busy_rejections: 0,
        repair_busy_rejections: 0,
        spool_rejections: 0,
    });
    let now = Instant::now();
    let sessions = state
        .sessions
        .lock()
        .expect("session registry lock")
        .len(now);
    let raw_composites = state
        .raw_composites
        .lock()
        .expect("raw composite registry lock")
        .len();
    let provider_sets = state
        .provider_sets
        .lock()
        .expect("provider set registry lock")
        .len(now);
    serde_json::to_string(&RuntimeStatsSnapshot {
        version: 1,
        draining: state.draining.load(Ordering::Acquire),
        requests_active: state.active_requests.load(Ordering::Acquire),
        request_workers: ENGINE_REQUEST_WORKERS,
        request_body_reserved_bytes: state.request_bodies.reserved_bytes.load(Ordering::Acquire),
        request_body_limit_bytes: state.request_bodies.maximum_bytes,
        request_body_busy_rejections_total: state
            .request_bodies
            .busy_rejections
            .load(Ordering::Relaxed),
        request_queue_busy_rejections_total: state
            .request_queue_busy_rejections
            .load(Ordering::Relaxed),
        sessions,
        session_prefetches_active: state.background_prefetches.load(Ordering::Acquire),
        nntp_hedges_active: state.active_hedges.load(Ordering::Acquire),
        nntp_hedges_started_total: state.hedges_started.load(Ordering::Relaxed),
        nntp_hedges_won_total: state.hedges_won.load(Ordering::Relaxed),
        nntp_salvage_holes_total: state.salvage_holes_total.load(Ordering::Relaxed),
        nntp_salvage_bytes_total: state.salvage_bytes_total.load(Ordering::Relaxed),
        raw_composites,
        provider_sets,
        segment_cache_entries: segment_stats.entries,
        segment_cache_bytes: segment_stats.used_bytes,
        disk_cache_mappings: disk_stats.mappings,
        disk_cache_blobs: disk_stats.blobs,
        disk_cache_bytes: disk_stats.used_bytes,
        disk_cache_stats_available,
        spool_stats_available: resource_stats_available,
        spool_resident_bytes: resource_stats.resident_bytes,
        spool_reserved_bytes: resource_stats.reserved_bytes,
        archive_jobs_active: resource_stats.archive_jobs_active,
        repair_jobs_active: resource_stats.repair_jobs_active,
        archive_busy_rejections_total: resource_stats.archive_busy_rejections,
        repair_busy_rejections_total: resource_stats.repair_busy_rejections,
        spool_rejections_total: resource_stats.spool_rejections,
        negative_cache_entries: state
            .negative_cache
            .lock()
            .expect("negative cache lock")
            .len(),
        network_singleflight_active: state.network_singleflight.active(),
        nntp_pools: pool_stats.pools,
        nntp_connections_open: pool_stats.open,
        nntp_connections_active: pool_stats.active,
        nntp_connections_idle: pool_stats.idle,
        nntp_queue_interactive: pool_stats.queued_interactive,
        nntp_queue_preparation: pool_stats.queued_preparation,
        nntp_queue_background: pool_stats.queued_background,
        nntp_preparation_slots: u64::try_from(state.nntp_pools.preparation_slots())
            .expect("NNTP preparation slot count fits u64"),
        nntp_reserved_commands: pool_stats.reserved_commands,
        nntp_reserved_encoded_bytes: pool_stats.reserved_encoded_bytes,
        nntp_reserved_decoded_bytes: pool_stats.reserved_decoded_bytes,
        nntp_scheduler_busy_rejections_total: pool_stats.scheduler_busy_rejections,
        nntp_connections_poisoned: pool_stats.poisoned,
        nntp_provider_attempts_total: pool_stats.provider_attempts,
        nntp_provider_suppliers_total: pool_stats.provider_suppliers,
        nntp_provider_hits_total: pool_stats.provider_hits,
        nntp_provider_missing_total: pool_stats.provider_missing,
        nntp_provider_corrupt_total: pool_stats.provider_corrupt,
        nntp_provider_failures_total: pool_stats.provider_failures,
        nntp_provider_cancellations_total: pool_stats.provider_cancellations,
        nntp_provider_failovers_total: pool_stats.provider_failovers,
    })
    .expect("serialize runtime statistics")
}

fn handle(
    mut stream: UnixStream,
    local_data: &Path,
    artifact_data: &Path,
    parser_only: bool,
    state: &Arc<EngineState>,
) {
    if !peer_is_current_user(&stream) {
        return;
    }
    if stream
        .set_read_timeout(Some(ENGINE_REQUEST_HEADER_TIMEOUT))
        .is_err()
        || stream
            .set_write_timeout(Some(ENGINE_RESPONSE_WRITE_TIMEOUT))
            .is_err()
    {
        return;
    }
    let mut request = Vec::new();
    let mut chunk = [0u8; 4096];
    while !request.windows(4).any(|bytes| bytes == b"\r\n\r\n")
        && request.len() < MAX_REQUEST_HEADER_BYTES
    {
        let Ok(length) = stream.read(&mut chunk) else {
            return;
        };
        if length == 0 {
            return;
        }
        request.extend_from_slice(&chunk[..length]);
    }
    let Some(header_end) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") else {
        if request.len() >= MAX_REQUEST_HEADER_BYTES {
            let _ = stream.write_all(
                response(
                    "431 Request Header Fields Too Large",
                    r#"{"version":1,"code":"request_header_too_large","retryable":false}"#,
                )
                .as_bytes(),
            );
        }
        return;
    };
    if header_end + 4 > MAX_REQUEST_HEADER_BYTES {
        let _ = stream.write_all(
            response(
                "431 Request Header Fields Too Large",
                r#"{"version":1,"code":"request_header_too_large","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    }
    let Ok(headers) = std::str::from_utf8(&request[..header_end]) else {
        let _ = stream.write_all(
            response(
                "400 Bad Request",
                r#"{"version":1,"code":"invalid_request","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    };
    let header_bytes = headers.as_bytes();
    if header_bytes.iter().enumerate().any(|(index, byte)| {
        (*byte == b'\r' && header_bytes.get(index + 1) != Some(&b'\n'))
            || (*byte == b'\n'
                && index
                    .checked_sub(1)
                    .and_then(|previous| header_bytes.get(previous))
                    != Some(&b'\r'))
    }) {
        let _ = stream.write_all(
            response(
                "400 Bad Request",
                r#"{"version":1,"code":"invalid_request","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    }
    let Some(first) = headers.lines().next().filter(|line| !line.is_empty()) else {
        let _ = stream.write_all(
            response(
                "400 Bad Request",
                r#"{"version":1,"code":"invalid_request","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    };
    let first = first.to_owned();
    let header_fields = headers
        .lines()
        .skip(1)
        .map(|line| line.split_once(':'))
        .collect::<Vec<_>>();
    let malformed_headers = header_fields.iter().any(|field| {
        field.is_none_or(|(name, value)| {
            name.is_empty()
                || !name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                || value
                    .bytes()
                    .any(|byte| byte.is_ascii_control() && byte != b'\t')
        })
    });
    let versions = header_fields
        .iter()
        .filter_map(|field| field.as_ref())
        .filter(|(name, _)| name.eq_ignore_ascii_case("x-comet-engine-version"))
        .map(|(_, value)| value.trim())
        .collect::<Vec<_>>();
    let content_lengths = header_fields
        .iter()
        .filter_map(|field| field.as_ref())
        .filter(|(name, _)| name.eq_ignore_ascii_case("content-length"))
        .map(|(_, value)| value.trim())
        .collect::<Vec<_>>();
    if versions.len() != 1
        || content_lengths.len() != 1
        || malformed_headers
        || header_fields
            .iter()
            .filter_map(|field| field.as_ref())
            .any(|(name, _)| name.eq_ignore_ascii_case("transfer-encoding"))
    {
        let _ = stream.write_all(
            response(
                "400 Bad Request",
                r#"{"version":1,"code":"invalid_request","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    }
    if content_lengths[0].is_empty()
        || !content_lengths[0].bytes().all(|byte| byte.is_ascii_digit())
    {
        let _ = stream.write_all(
            response(
                "400 Bad Request",
                r#"{"version":1,"code":"invalid_request","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    }
    let Ok(content_length) = content_lengths[0].parse::<usize>() else {
        let _ = stream.write_all(
            response(
                "400 Bad Request",
                r#"{"version":1,"code":"invalid_request","retryable":false}"#,
            )
            .as_bytes(),
        );
        return;
    };
    let version = versions[0].to_owned();
    let maximum_body = maximum_request_body(&first);
    if content_length > maximum_body {
        let _ = stream.write_all(
            response(
                "413 Payload Too Large",
                if artifact_native_catalog_identity(&first).is_some() {
                    r#"{"version":1,"code":"asset_catalog_too_large","retryable":false}"#
                } else if provider_set_put_generation(&first).is_some() {
                    r#"{"version":1,"code":"provider_set_too_large","retryable":false}"#
                } else if maximum_body == MAX_NZB_METADATA_BYTES {
                    r#"{"version":1,"code":"native_metadata_too_large","retryable":false}"#
                } else if maximum_body == MAX_CONTROL_REQUEST_BYTES {
                    r#"{"version":1,"code":"control_request_too_large","retryable":false}"#
                } else {
                    r#"{"version":1,"code":"nzb_too_large","retryable":false}"#
                },
            )
            .as_bytes(),
        );
        return;
    }
    if request_releases_allocator_slack(&first, content_length) {
        mark_allocator_collect();
    }
    let Ok(_body_permit) = state.request_bodies.reserve(content_length) else {
        let _ = stream.write_all(
            response(
                "503 Service Unavailable",
                r#"{"version":1,"code":"native_busy","retryable":true}"#,
            )
            .as_bytes(),
        );
        return;
    };
    let body_start = header_end + 4;
    let Some(request_length) = body_start.checked_add(content_length) else {
        return;
    };
    if request
        .try_reserve_exact(request_length.saturating_sub(request.len()))
        .is_err()
    {
        let _ = stream.write_all(
            response(
                "503 Service Unavailable",
                r#"{"version":1,"code":"native_busy","retryable":true}"#,
            )
            .as_bytes(),
        );
        return;
    }
    if stream
        .set_read_timeout(Some(ENGINE_REQUEST_BODY_TIMEOUT))
        .is_err()
    {
        return;
    }
    while request.len() - body_start < content_length {
        let remaining = content_length - (request.len() - body_start);
        let read_length = remaining.min(chunk.len());
        let Ok(length) = stream.read(&mut chunk[..read_length]) else {
            return;
        };
        if length == 0 {
            return;
        }
        request.extend_from_slice(&chunk[..length]);
    }
    let body = &request[body_start..body_start + content_length];
    let Ok(peer) = stream.try_clone() else {
        return;
    };
    let cancelled = || peer_disconnected(&peer);
    let mut binary_body: Option<Vec<u8>> = None;
    let payload = match (first.as_str(), Some(version.as_str())) {
        ("POST /v1/drain HTTP/1.1", Some(API_VERSION)) if body.is_empty() => {
            state.draining.store(true, Ordering::Release);
            response(
                "202 Accepted",
                r#"{"version":1,"draining":true}"#,
            )
            .into_bytes()
        }
        ("POST /v1/resume HTTP/1.1", Some(API_VERSION)) if body.is_empty() => {
            state.draining.store(false, Ordering::Release);
            response("200 OK", r#"{"version":1,"draining":false}"#).into_bytes()
        }
        ("GET /v1/health HTTP/1.1", Some(API_VERSION)) => response(
            "200 OK",
            if parser_only {
                r#"{"version":1,"mode":"parser"}"#
            } else if state.par2_tool.is_some() && state.archive_runtime.is_some() {
                r#"{"version":1,"mode":"native","par2":"ready","archive":"ready"}"#
            } else {
                r#"{"version":1,"mode":"native","par2":"unavailable","archive":"unavailable"}"#
            },
        )
        .into_bytes(),
        ("GET /v1/stats HTTP/1.1", Some(API_VERSION)) => {
            response_bytes("200 OK", &runtime_stats_body(state))
        }
        (_, Some(API_VERSION)) if state.draining.load(Ordering::Acquire) => response(
            "503 Service Unavailable",
            r#"{"version":1,"code":"engine_draining","retryable":true}"#,
        )
        .into_bytes(),
        (first, Some(API_VERSION)) if is_artifact_parse_request(first) => match parse_nzb(body) {
            Ok(payload) => response_bytes("200 OK", &payload),
            Err(code) => response(
                "422 Unprocessable Content",
                &failure_body(code, false),
            )
            .into_bytes(),
        },
        (first, Some(API_VERSION)) if parser_only && is_native_work_request(first) => response(
            "404 Not Found",
            r#"{"version":1,"code":"native_engine_disabled","retryable":false}"#,
        )
        .into_bytes(),
        (first, Some(API_VERSION))
            if !parser_only && provider_set_put_generation(first).is_some() =>
        {
            let generation =
                provider_set_put_generation(first).expect("checked provider set generation");
            let now = std::time::Instant::now();
            state
                .sessions
                .lock()
                .expect("session registry lock")
                .len(now);
            match serde_json::from_slice::<provider::Registration>(body)
                .map_err(|_| "invalid_provider_set")
                .and_then(|registration| {
                    state
                        .provider_sets
                        .lock()
                        .expect("provider set registry lock")
                        .register(generation, registration, now)
                }) {
                Ok(provider_set) => response_bytes(
                    "200 OK",
                    &serde_json::to_string(&ProviderSetRegistrationResponse {
                        version: 1u32,
                        provider_set_id: &provider_set.identity,
                        generation: &provider_set.generation,
                    })
                    .expect("serialize provider set registration"),
                ),
                Err(code) => response_bytes(
                    if code == "invalid_provider_set" {
                        "422 Unprocessable Content"
                    } else if code == "provider_set_random_unavailable" {
                        "503 Service Unavailable"
                    } else {
                        "409 Conflict"
                    },
                    &failure_body(code, code == "provider_set_random_unavailable"),
                ),
            }
        }
        (first, Some(API_VERSION)) if artifact_native_catalog_identity(first).is_some() =>
        {
            let artifact_sha256 =
                artifact_native_catalog_identity(first).expect("checked artifact identity");
            match serde_json::from_slice::<NativeCatalogRequest>(body)
                .map_err(|_| "asset_catalog_invalid")
                .and_then(|request| {
                    let selection_hint = request
                        .selection_hint
                        .as_ref()
                        .map(|hint| (hint.relative_path.as_str(), hint.exact_size));
                    inspect::catalog_manifest(
                        artifact_sha256,
                        &request.manifest_identity,
                        &request.metadata,
                        &request.manifest,
                        selection_hint,
                    )
                })
                .and_then(|assets| {
                    let payload = serde_json::to_string(&AssetCatalogResponse {
                        version: 1u32,
                        artifact_sha256,
                        assets: &assets,
                    })
                    .map_err(|_| "asset_catalog_invalid")?;
                    (payload.len() <= MAX_NATIVE_CATALOG_BYTES)
                        .then_some(payload)
                        .ok_or("asset_catalog_too_large")
                }) {
                Ok(payload) => response_bytes("200 OK", &payload),
                Err(code) => response(
                    "422 Unprocessable Content",
                    &failure_body(code, false),
                )
                .into_bytes(),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && artifact_native_inspect_identity(first).is_some() =>
        {
            let artifact_sha256 =
                artifact_native_inspect_identity(first).expect("checked artifact identity");
            match serde_json::from_slice::<NativeInspectRequest>(body)
                .map_err(|_| "invalid_native_inspection")
                .and_then(|request| {
                    normalize_materialization_request(
                        MaterializationRequest {
                            postings: request.postings,
                            group: request.group,
                            account_partition: request.account_partition,
                            provider_set_id: request.provider_set_id,
                        },
                        &state.provider_sets,
                        nntp::WorkClass::Background,
                    )
                })
                .and_then(|(mut source, postings)| {
                    let identity = session::random_session_id()?;
                    source.scheduler_session = identity.clone();
                    let first = state.fetch_posting(&source, &postings[0], &cancelled)?;
                    let session = Arc::new(Mutex::new(session::RandomAccessSession::new(
                        identity,
                        postings,
                        first.segment(),
                    )?));
                    let size = session.lock().expect("random access session lock").size();
                    let budget = u64::try_from(inspect::MAX_STRUCTURAL_END_BYTES)
                        .map_err(|_| "container_probe_budget")?;
                    let head_length = size.min(budget);
                    let head = session::RandomAccessSession::read_at(
                        &session,
                        0,
                        head_length,
                        |posting| state.fetch_posting(&source, posting, &cancelled),
                    )?;
                    let tail = if size > budget {
                        session::RandomAccessSession::read_at(
                            &session,
                            size - budget,
                            budget,
                            |posting| state.fetch_posting(&source, posting, &cancelled),
                        )?
                    } else {
                        Vec::new()
                    };
                    inspect::probe_container(&head, &tail)
                }) {
                Ok(evidence) => response_bytes(
                    "200 OK",
                    &serde_json::to_string(&ArtifactInspectResponse {
                        version: 1u32,
                        artifact_sha256,
                        inspection_state: "provisionally_streamable",
                        container: evidence.kind.as_str(),
                        duration_millis: evidence.duration_millis,
                        inspected_head_bytes: evidence.inspected_head_bytes,
                        inspected_tail_bytes: evidence.inspected_tail_bytes,
                    })
                    .expect("serialize artifact inspection"),
                ),
                Err(code) => native_work_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && materialization_native_inspect_identity(first).is_some() =>
        {
            let identity = materialization_native_inspect_identity(first)
                .expect("checked materialization identity");
            match serde_json::from_slice::<MaterializationInspectRequest>(body)
                .map_err(|_| "invalid_native_inspection")
                .and_then(|request| {
                    let (head, mut tail, file_identity) =
                        materialization::verified_samples_cancellable(
                        artifact_data,
                        identity,
                        request.expected_size,
                        inspect::MAX_STRUCTURAL_END_BYTES,
                        &cancelled,
                    )?;
                    if request.expected_size
                        <= u64::try_from(inspect::MAX_STRUCTURAL_END_BYTES)
                            .map_err(|_| "container_probe_budget")?
                    {
                        tail.clear();
                    }
                    let evidence = inspect::probe_container(&head, &tail)?;
                    let source = raw_composite::RawCompositeSource::from_materialization(
                        identity.to_owned(),
                        request.expected_size,
                        file_identity,
                    )?;
                    let (source_identity, _) = state
                        .raw_composites
                        .lock()
                        .expect("immutable source registry lock")
                        .insert(source, std::time::Instant::now())?;
                    Ok((evidence, source_identity))
                }) {
                Ok((evidence, source_identity)) => response(
                    "200 OK",
                    &serde_json::json!({
                        "version": 1,
                        "materialization_identity": identity,
                        "source_identity": source_identity,
                        "inspection_state": "provisionally_streamable",
                        "container": evidence.kind.as_str(),
                        "duration_millis": evidence.duration_millis,
                        "inspected_head_bytes": evidence.inspected_head_bytes,
                        "inspected_tail_bytes": evidence.inspected_tail_bytes,
                    })
                    .to_string(),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_session_request(first) => {
            match serde_json::from_slice::<SessionCreateRequest>(body)
                .map_err(|_| "invalid_random_access_session")
                .and_then(|request| {
                    let work_class = if request.preparation {
                        nntp::WorkClass::Preparation
                    } else {
                        nntp::WorkClass::Interactive
                    };
                    normalize_materialization_request(
                        request.source,
                        &state.provider_sets,
                        work_class,
                    )
                    .map(|normalized| (normalized, request.allow_degraded_playback))
                })
                .and_then(|((mut source, postings), allow_degraded_playback)| {
                    let recreation_key =
                        session::session_recreation_key_with_degraded_playback(
                            source.provider_set.account_partition,
                            &source.provider_set.generation,
                            source.group.as_deref(),
                            &postings,
                            allow_degraded_playback,
                        );
                    if let Some((identity, size, asset_revision)) = state
                        .sessions
                        .lock()
                        .expect("session registry lock")
                        .describe_by_recreation_key(
                            &recreation_key,
                            std::time::Instant::now(),
                        )
                    {
                        return Ok((
                            identity,
                            size,
                            recreation_key,
                            asset_revision,
                        ));
                    }
                    let identity = session::random_session_id()?;
                    source.scheduler_session = identity.clone();
                    let first =
                        state.fetch_posting(&source, &postings[0], &cancelled)?;
                    source.work_class = nntp::WorkClass::Interactive;
                    let posting_count = postings.len();
                    let mut session = session::RandomAccessSession::new_with_degraded_playback(
                        identity,
                        postings,
                        first.segment(),
                        allow_degraded_playback,
                    )?;
                    let checkpoints = state
                        .session_checkpoints
                        .lock()
                        .expect("session checkpoint store lock")
                        .load(&recreation_key, posting_count, session.size())?;
                    session.restore_checkpoints(&checkpoints)?;
                    let checkpoint_count = session.pending_checkpoints().len();
                    state
                        .session_checkpoints
                        .lock()
                        .expect("session checkpoint store lock")
                        .merge(&recreation_key, session.pending_checkpoints())?;
                    session.commit_pending_checkpoints(checkpoint_count);
                    let (identity, size, asset_revision) = state
                        .sessions
                        .lock()
                        .expect("session registry lock")
                        .insert(
                            session,
                            source,
                            recreation_key.clone(),
                            std::time::Instant::now(),
                        )?;
                    Ok((
                        identity,
                        size,
                        recreation_key,
                        asset_revision,
                    ))
                }) {
                Ok((identity, byte_size, revision, asset_revision)) => {
                    response_bytes(
                        "200 OK",
                        &serde_json::to_string(&SessionOpenResponse {
                            version: 1,
                            identity,
                            byte_size,
                            revision,
                            asset_revision,
                        })
                        .expect("serialize random access session"),
                    )
                }
                Err(code) => native_work_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_materialization_request(first) => {
            match serde_json::from_slice::<MaterializationRequest>(body)
                .map_err(|_| "invalid_materialization")
                .and_then(|request| {
                    normalize_materialization_request(
                        request,
                        &state.provider_sets,
                        nntp::WorkClass::Preparation,
                    )
                })
                .and_then(|(mut source, postings)| {
                    let _permit = state
                        .try_materialization_permit()
                        .ok_or("native_busy")?;
                    source.scheduler_session = session::session_recreation_key(
                        source.provider_set.account_partition,
                        &source.provider_set.generation,
                        source.group.as_deref(),
                        &postings,
                    );
                    let first = state.fetch_posting(&source, &postings[0], &cancelled)?;
                    // A rolling cache tail does not make a sequential download
                    // resumable. Retain only sources that fit in a fair share.
                    source.use_memory_cache =
                        state.preparation_fits_memory_cache(first.segment().total_size);
                    source.use_disk_cache =
                        state.preparation_fits_disk_cache(first.segment().total_size);
                    source.admit_disk_cache = source.use_disk_cache;
                    let _reservation = state
                        .resources
                        .reserve_materialization(first.segment().total_size, artifact_data)?;
                    let mut stage = materialization::StagedMaterialization::new_with_publication(
                        local_data,
                        artifact_data,
                    )?;
                    stage.push_segment(first.segment())?;
                    state.fetch_postings_batched(
                        &source,
                        &postings[1..],
                        false,
                        &cancelled,
                        |segment| stage.push_segment(segment),
                    )?;
                    let published = stage.publish()?;
                    let file_identity =
                        materialization::sealed_identity(&published.0, published.2)?;
                    state
                        .verified_materializations
                        .lock()
                        .expect("verified materialization cache lock")
                        .insert(published.1.clone(), file_identity);
                    Ok(published)
                }) {
                Ok((_path, identity, byte_size, asset_revision)) => response(
                    "200 OK",
                    &format!(
                        r#"{{"version":1,"identity":"{}","byte_size":{},"asset_revision":"{}"}}"#,
                        identity, byte_size, asset_revision
                    ),
                )
                .into_bytes(),
                Err(code) => native_work_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_archive_plan_request(first) => {
            match serde_json::from_slice::<ArchivePlanRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    let _reservation = state.resources.reserve_archive_job()?;
                    plan_archive_request(
                        request,
                        artifact_data,
                        Some(&state.verified_materializations),
                        &cancelled,
                    )
                })
                .map(|(plan, _)| plan)
            {
                Ok(plan) => response(
                    "200 OK",
                    &format!(
                        r#"{{"version":1,"plan":{}}}"#,
                        serde_json::to_string(&plan).expect("serialize archive volume plan")
                    ),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && is_session_archive_catalog_request(first) =>
        {
            match serde_json::from_slice::<SessionArchiveCatalogRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    let _reservation = state.resources.reserve_archive_job()?;
                    let (plan, volumes) =
                        plan_session_archive(request.volumes, state, &cancelled)?;
                    let members = stored_archive_catalog(
                        &plan,
                        parse_session_stored_members(&plan, &volumes, state, &cancelled)?,
                    )?;
                    let payload = serde_json::to_string(&ArchiveCatalogResponse {
                        version: 1,
                        plan: &plan,
                        members,
                    })
                    .map_err(|_| "archive_catalog_invalid")?;
                    (payload.len() <= MAX_NATIVE_CATALOG_BYTES)
                        .then_some(payload)
                        .ok_or("archive_catalog_too_large")
                }) {
                Ok(payload) => response_bytes("200 OK", &payload),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_session_archive_open_request(first) => {
            match serde_json::from_slice::<SessionArchiveOpenRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    if request.expected_output_size == 0
                        || request.expected_output_size > archive_group::MAX_LOGICAL_BYTES
                    {
                        return Err("archive_output_size_invalid");
                    }
                    let selected_path =
                        archive::normalize_archive_path(&request.selected_path)?;
                    let _reservation = state.resources.reserve_archive_job()?;
                    let (plan, volumes) =
                        plan_session_archive(request.volumes, state, &cancelled)?;
                    let member = parse_session_stored_members(
                        &plan,
                        &volumes,
                        state,
                        &cancelled,
                    )?
                    .into_iter()
                    .find(|member| {
                        member.relative_path == selected_path
                            && member.exact_size == request.expected_output_size
                    })
                    .ok_or("archive_member_not_found")?;
                    let member_id = archive_group::member_identity(
                        &plan.set_identity,
                        &member.relative_path,
                        member.exact_size,
                    )?;
                    let parts = member
                        .ranges
                        .into_iter()
                        .map(|range| {
                            let volume = volumes
                                .get(range.volume_index)
                                .ok_or("archive_volume_conflict")?;
                            Ok(raw_composite::RawCompositePart {
                                content_identity: volume.revision.clone(),
                                source_offset: range.offset,
                                exact_size: range.length,
                                backing: raw_composite::RawCompositeBacking::Session {
                                    identity: volume.session_id.clone(),
                                    revision: volume.revision.clone(),
                                    exact_size: volume.exact_size,
                                    _retention: volume.retention.clone(),
                                },
                            })
                        })
                        .collect::<Result<Vec<_>, &'static str>>()?;
                    let source =
                        raw_composite::RawCompositeSource::from_ranges(member_id.clone(), parts)?;
                    let (identity, exact_size) = state
                        .raw_composites
                        .lock()
                        .expect("raw composite registry lock")
                        .insert(source, Instant::now())?;
                    Ok((plan, identity, exact_size, member.relative_path))
                }) {
                Ok((plan, identity, exact_size, relative_path)) => response(
                    "200 OK",
                    &serde_json::json!({
                        "version": 1,
                        "identity": identity,
                        "exact_size": exact_size,
                        "etag": identity,
                        "relative_path": relative_path,
                        "plan": plan,
                    })
                    .to_string(),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && is_archive_direct_catalog_request(first) =>
        {
            match serde_json::from_slice::<ArchivePlanRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    let _reservation = state.resources.reserve_archive_job()?;
                    let (plan, file_identities) = plan_archive_request(
                        request,
                        artifact_data,
                        Some(&state.verified_materializations),
                        &cancelled,
                    )?;
                    let members = stored_archive_catalog(
                        &plan,
                        parse_stored_direct_members(
                            &plan,
                            &file_identities,
                            artifact_data,
                            &cancelled,
                        )?,
                    )?;
                    let payload = serde_json::to_string(&ArchiveCatalogResponse {
                        version: 1u32,
                        plan: &plan,
                        members,
                    })
                    .map_err(|_| "archive_catalog_invalid")?;
                    (payload.len() <= MAX_NATIVE_CATALOG_BYTES)
                        .then_some(payload)
                        .ok_or("archive_catalog_too_large")
                }) {
                Ok(payload) => response_bytes("200 OK", &payload),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_archive_direct_open_request(first) => {
            match serde_json::from_slice::<ArchiveSetExtractionRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    if request.expected_output_size == 0
                        || request.expected_output_size > archive_group::MAX_LOGICAL_BYTES
                    {
                        return Err("archive_output_size_invalid");
                    }
                    let selected_path =
                        archive::normalize_archive_path(&request.selected_path)?;
                    let _reservation = state.resources.reserve_archive_job()?;
                    let (plan, file_identities) = plan_archive_request(
                        ArchivePlanRequest {
                            volumes: request.volumes,
                        },
                        artifact_data,
                        Some(&state.verified_materializations),
                        &cancelled,
                    )?;
                    let member = parse_stored_direct_members(
                        &plan,
                        &file_identities,
                        artifact_data,
                        &cancelled,
                    )?
                    .into_iter()
                    .find(|member| {
                        member.relative_path == selected_path
                            && member.exact_size == request.expected_output_size
                    })
                    .ok_or("archive_member_not_found")?;
                    let member_id = archive_group::member_identity(
                        &plan.set_identity,
                        &member.relative_path,
                        member.exact_size,
                    )?;
                    let parts = member
                        .ranges
                        .into_iter()
                        .map(|range| {
                            let volume = plan
                                .volumes
                                .get(range.volume_index)
                                .ok_or("archive_volume_conflict")?;
                            Ok(raw_composite::RawCompositePart {
                                content_identity: volume.content_identity.clone(),
                                source_offset: range.offset,
                                exact_size: range.length,
                                backing: raw_composite::RawCompositeBacking::Materialization(
                                    *file_identities
                                        .get(&volume.content_identity)
                                        .ok_or("materialization_conflict")?,
                                ),
                            })
                        })
                        .collect::<Result<Vec<_>, &'static str>>()?;
                    let source =
                        raw_composite::RawCompositeSource::from_ranges(member_id.clone(), parts)?;
                    let (identity, exact_size) = state
                        .raw_composites
                        .lock()
                        .expect("raw composite registry lock")
                        .insert(source, std::time::Instant::now())?;
                    Ok((
                        plan,
                        identity,
                        exact_size,
                        member.relative_path,
                    ))
                }) {
                Ok((plan, identity, exact_size, relative_path)) => response(
                    "200 OK",
                    &serde_json::json!({
                        "version": 1,
                        "identity": identity,
                        "exact_size": exact_size,
                        "etag": identity,
                        "relative_path": relative_path,
                        "plan": plan,
                    })
                    .to_string(),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && is_archive_nested_catalog_request(first) =>
        {
            match serde_json::from_slice::<ArchiveNestedCatalogRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    let passphrase = validated_archive_passphrase(request.passphrase)?;
                    let runtime = state
                        .archive_runtime
                        .as_ref()
                        .ok_or("archive_runtime_unavailable")?;
                    let (plan, file_identities) = {
                        let _reservation = state.resources.reserve_archive_job()?;
                        plan_archive_request(
                            ArchivePlanRequest {
                                volumes: request.volumes,
                            },
                            artifact_data,
                            Some(&state.verified_materializations),
                            &cancelled,
                        )?
                    };
                    if !matches!(
                        plan.kind,
                        archive_group::VolumePlanKind::SingleArchive(_)
                            | archive_group::VolumePlanKind::MultiVolumeArchive(
                                archive::ArchiveFormat::Rar4
                                    | archive::ArchiveFormat::Rar5
                                    | archive::ArchiveFormat::SevenZip
                            )
                    ) {
                        return Err("archive_layout_unsupported");
                    }
                    let reserved_bytes = plan
                        .exact_size
                        .checked_add(archive_runtime::MAX_ARCHIVE_CATALOG_BYTES)
                        .ok_or("native_spool_full")?;
                    let mut reservation = state.resources.reserve_archive(reserved_bytes)?;
                    let stage =
                        materialization::ArchiveExtractionStage::new_with_publication(
                            local_data,
                            artifact_data,
                        )?;
                    let parts = verified_archive_parts(&plan, &file_identities);
                    let input =
                        stage.concatenate_verified(&parts, plan.exact_size, &cancelled)?;
                    let members = archive_nested::catalog(
                        runtime,
                        input,
                        &plan.set_identity,
                        plan.exact_size,
                        passphrase.as_ref().map(|value| value.as_str()),
                        local_data,
                        &mut reservation,
                        &cancelled,
                    )?;
                    let payload = serde_json::to_string(&NestedCatalogResponse {
                        version: 1u32,
                        plan: &plan,
                        members,
                    })
                    .map_err(|_| "archive_catalog_invalid")?;
                    (payload.len() <= MAX_NATIVE_CATALOG_BYTES)
                        .then_some(payload)
                        .ok_or("archive_catalog_too_large")
                }) {
                Ok(payload) => response_bytes("200 OK", &payload),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && is_archive_nested_extraction_request(first) =>
        {
            match serde_json::from_slice::<ArchiveNestedExtractionRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    let passphrase = validated_archive_passphrase(request.passphrase)?;
                    if request.expected_output_size == 0
                        || request.expected_output_size
                            > archive_runtime::MAX_ARCHIVE_OUTPUT_BYTES
                    {
                        return Err("archive_output_size_invalid");
                    }
                    let runtime = state
                        .archive_runtime
                        .as_ref()
                        .ok_or("archive_runtime_unavailable")?;
                    let (plan, file_identities) = {
                        let _reservation = state.resources.reserve_archive_job()?;
                        plan_archive_request(
                            ArchivePlanRequest {
                                volumes: request.volumes,
                            },
                            artifact_data,
                            Some(&state.verified_materializations),
                            &cancelled,
                        )?
                    };
                    if !matches!(
                        plan.kind,
                        archive_group::VolumePlanKind::SingleArchive(_)
                            | archive_group::VolumePlanKind::MultiVolumeArchive(
                                archive::ArchiveFormat::Rar4
                                    | archive::ArchiveFormat::Rar5
                                    | archive::ArchiveFormat::SevenZip
                            )
                    ) {
                        return Err("archive_layout_unsupported");
                    }
                    // The final local extraction remains present while a
                    // cross-filesystem publication is copied, and every
                    // layer has one bounded catalog sidecar.
                    let reserved_bytes = plan
                        .exact_size
                        .checked_add(archive_runtime::MAX_ARCHIVE_CATALOG_BYTES)
                        .and_then(|bytes| bytes.checked_add(request.expected_output_size))
                        .ok_or("native_spool_full")?;
                    let mut reservation = state.resources.reserve_archive(reserved_bytes)?;
                    let stage =
                        materialization::ArchiveExtractionStage::new_with_publication(
                            local_data,
                            artifact_data,
                        )?;
                    let parts = verified_archive_parts(&plan, &file_identities);
                    let input =
                        stage.concatenate_verified(&parts, plan.exact_size, &cancelled)?;
                    archive_nested::extract(
                        runtime,
                        input,
                        plan.exact_size,
                        &request.selected_paths,
                        request.expected_output_size,
                        passphrase.as_ref().map(|value| value.as_str()),
                        local_data,
                        artifact_data,
                        &mut reservation,
                        &cancelled,
                    )
                }) {
                Ok((_path, identity, byte_size, asset_revision)) => response(
                    "200 OK",
                    &format!(
                        r#"{{"version":1,"identity":"{identity}","byte_size":{byte_size},"asset_revision":"{asset_revision}"}}"#
                    ),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_par2_discovery_request(first) => {
            match serde_json::from_slice::<Par2CatalogRequest>(body)
                .map_err(|_| "par2_input_invalid")
                .and_then(|mut request| {
                    validate_par2_files(&mut request.files)?;
                    let _reservation = state.resources.reserve_archive_job()?;
                    let discovered =
                        read_par2_sets(&request.files, artifact_data, &cancelled)?;
                    let sets = discovered
                        .into_iter()
                        .map(|discovered| {
                            let mut catalog = par2_catalog_object(&discovered.set)?;
                            let volume_content_identities =
                                canonical_par2_volume_identities(
                                    &request.files,
                                    discovered.input_indices,
                                )?;
                            catalog.insert(
                                "volume_content_identities".to_owned(),
                                serde_json::json!(volume_content_identities),
                            );
                            Ok(serde_json::Value::Object(catalog))
                        })
                        .collect::<Result<Vec<_>, &'static str>>()?;
                    let payload = serde_json::to_string(&Par2DiscoveryResponse {
                        version: 1u32,
                        sets,
                    })
                    .map_err(|_| "par2_catalog_invalid")?;
                    (payload.len() <= MAX_NATIVE_CATALOG_BYTES)
                        .then_some(payload)
                        .ok_or("par2_catalog_too_large")
                }) {
                Ok(payload) => response_bytes("200 OK", &payload),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_par2_source_map_request(first) => {
            match serde_json::from_slice::<Par2SourceMapRequest>(body)
                .map_err(|_| "par2_source_invalid")
                .and_then(|mut request| {
                    validate_par2_files(&mut request.files)?;
                    validate_par2_sources(&mut request.sources, false)?;
                    let selected_set_id = request
                        .set_id
                        .as_deref()
                        .map(par2_set_id)
                        .transpose()?;
                    let _reservation = state.resources.reserve_archive_job()?;
                    let (set, _inputs) = read_par2_selected_set(
                        &request.files,
                        artifact_data,
                        selected_set_id,
                        &cancelled,
                    )?;
                    let mut mapped_file_ids = BTreeSet::new();
                    let mappings = request
                        .sources
                        .iter()
                        .map(|source| {
                            let (file_id, file_identity) =
                                materialization::inspect_verified_with_identity_cancellable(
                                    artifact_data,
                                    &source.content_identity,
                                    source.expected_size,
                                    &cancelled,
                                    |input| {
                                        par2::identify_complete_source(
                                            &set,
                                            input,
                                            source.expected_size,
                                            &cancelled,
                                        )
                                    },
                                )?;
                            state
                                .verified_materializations
                                .lock()
                                .expect("verified materialization cache lock")
                                .insert(source.content_identity.clone(), file_identity);
                            if !mapped_file_ids.insert(file_id) {
                                return Err("par2_source_ambiguous");
                            }
                            let description = set
                                .files
                                .get(&file_id)
                                .ok_or("par2_file_description_missing")?;
                            let slice_count = set
                                .checksums
                                .get(&file_id)
                                .ok_or("par2_ifsc_missing")?
                                .len();
                            Ok(serde_json::json!({
                                "content_identity": source.content_identity,
                                "file_id": lower_hex(&file_id),
                                "relative_path": description.relative_path,
                                "exact_size": description.byte_size,
                                "slice_count": slice_count,
                            }))
                        })
                        .collect::<Result<Vec<_>, &'static str>>()?;
                    let payload = serde_json::to_string(&Par2SourceMapResponse {
                        version: 1u32,
                        set_id: lower_hex(&set.set_id),
                        slice_size: set.slice_size,
                        mappings,
                    })
                    .map_err(|_| "par2_source_invalid")?;
                    (payload.len() <= MAX_NATIVE_CATALOG_BYTES)
                        .then_some(payload)
                        .ok_or("par2_catalog_too_large")
                }) {
                Ok(payload) => response_bytes("200 OK", &payload),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_par2_repair_request(first) => {
            let mut required_recovery_blocks = None;
            match serde_json::from_slice::<Par2RepairRequest>(body)
                .map_err(|_| "par2_source_invalid")
                .and_then(|mut request| {
                    validate_par2_files(&mut request.files)?;
                    validate_par2_sources(&mut request.sources, true)?;
                    if request.partial_sources.len() > nzb::MAX_FILES
                        || request
                            .partial_sources
                            .iter()
                            .try_fold(0usize, |total, source| {
                                total.checked_add(source.postings.len())
                            })
                            .is_none_or(|total| total > nzb::MAX_SEGMENTS)
                    {
                        return Err("repair_scope_exceeds_budget");
                    }
                    let selected_file_id = par2_file_id(&request.selected_file_id)?;
                    let selected_set_id = request
                        .set_id
                        .as_deref()
                        .map(par2_set_id)
                        .transpose()?;
                    let (mut set, mut inputs) = {
                        let _parser = state.resources.reserve_archive_job()?;
                        read_par2_selected_set(
                            &request.files,
                            artifact_data,
                            selected_set_id,
                            &cancelled,
                        )?
                    };
                    let selected = set
                        .files
                        .get(&selected_file_id)
                        .filter(|_| set.source_file_ids.contains(&selected_file_id))
                        .ok_or("par2_file_not_in_recovery_set")?;
                    let mut reservation = state.resources.reserve_repair(0)?;
                    let mut mapped_sources = BTreeMap::new();
                    let mut known_valid = BTreeMap::new();
                    for source in &request.sources {
                        let (file_id, asset_revision) =
                            materialization::inspect_verified_with_revision_cancellable(
                                artifact_data,
                                &source.content_identity,
                                source.expected_size,
                                &cancelled,
                                |input| {
                                    par2::identify_complete_source(
                                        &set,
                                        input,
                                        source.expected_size,
                                        &cancelled,
                                    )
                                },
                            )?;
                        if mapped_sources
                            .insert(file_id, (source, asset_revision))
                            .is_some()
                        {
                            return Err("par2_source_ambiguous");
                        }
                        let slice_count = set
                            .checksums
                            .get(&file_id)
                            .ok_or("par2_ifsc_missing")?
                            .len();
                        known_valid.insert(file_id, vec![true; slice_count]);
                    }
                    if let Some((source, asset_revision)) =
                        mapped_sources.get(&selected_file_id)
                    {
                        return Ok((
                            lower_hex(&set.set_id),
                            request.selected_file_id,
                            selected.relative_path.clone(),
                            source.content_identity.clone(),
                            selected.byte_size,
                            asset_revision.clone(),
                            false,
                        ));
                    }
                    let stage_bytes = set
                        .source_file_ids
                        .iter()
                        .try_fold(0u64, |total, file_id| {
                            total
                                .checked_add(
                                    set.files
                                        .get(file_id)
                                        .ok_or("par2_file_description_missing")?
                                        .byte_size,
                                )
                                .ok_or("repair_scope_exceeds_budget")
                        })?
                        .checked_add(inputs.exact_size)
                        .filter(|bytes| {
                            (1..=archive_group::MAX_LOGICAL_BYTES).contains(bytes)
                        })
                        .ok_or("repair_scope_exceeds_budget")?;
                    let required_spool = stage_bytes
                        .checked_add(selected.byte_size)
                        .ok_or("repair_scope_exceeds_budget")?;
                    state.resources.ensure_spool_available(required_spool)?;
                    let mut partial_sources = Vec::with_capacity(request.partial_sources.len());
                    for partial_request in std::mem::take(&mut request.partial_sources) {
                        let (mut source, postings) = normalize_materialization_request(
                            partial_request,
                            &state.provider_sets,
                            nntp::WorkClass::Preparation,
                        )?;
                        source.scheduler_session = session::session_recreation_key(
                            source.provider_set.account_partition,
                            &source.provider_set.generation,
                            source.group.as_deref(),
                            &postings,
                        );
                        partial_sources.push((source, postings));
                    }
                    let mut closure_sizes = mapped_sources
                        .keys()
                        .map(|file_id| {
                            set.files
                                .get(file_id)
                                .map(|file| file.byte_size)
                                .ok_or("par2_file_description_missing")
                        })
                        .collect::<Result<Vec<_>, _>>()?;
                    for (_source, postings) in &partial_sources {
                        closure_sizes.push(postings.iter().try_fold(0u64, |total, posting| {
                            total
                                .checked_add(posting.declared_encoded_bytes)
                                .ok_or("repair_scope_exceeds_budget")
                        })?);
                    }
                    closure_sizes.sort_unstable();
                    let mut recovery_sizes = set
                        .source_file_ids
                        .iter()
                        .map(|file_id| {
                            set.files
                                .get(file_id)
                                .map(|file| file.byte_size)
                                .ok_or("par2_file_description_missing")
                        })
                        .collect::<Result<Vec<_>, _>>()?;
                    recovery_sizes.sort_unstable();
                    if !partial_sources.is_empty() && closure_sizes == recovery_sizes {
                        let missing = state.survey_repair_closure(
                            &partial_sources,
                            set.slice_size,
                            &cancelled,
                        )?;
                        if missing.is_some_and(|missing| {
                            missing > set.recovery_exponents.len()
                        }) {
                            required_recovery_blocks = missing;
                            return Err("repair_insufficient");
                        }
                    }
                    let tool = state
                        .par2_tool
                        .as_ref()
                        .ok_or("repair_runtime_unavailable")?;
                    // The repaired local source remains present while a
                    // cross-filesystem publication is copied.
                    reservation.grow(required_spool)?;
                    let mut partial_mappings = BTreeMap::<
                        par2::FileId,
                        Vec<(materialization::PartialSourceStage, Vec<bool>)>,
                    >::new();
                    let mut partial_source_mapped = false;
                    let partial_source_bytes = partial_sources.iter().try_fold(
                        0u64,
                        |total, (_source, postings)| {
                            postings.iter().try_fold(total, |total, posting| {
                                total
                                    .checked_add(posting.declared_encoded_bytes)
                                    .ok_or("repair_scope_exceeds_budget")
                            })
                        },
                    )?;
                    let cache_partial_sources_in_memory =
                        state.preparation_fits_memory_cache(partial_source_bytes);
                    let cache_partial_sources_on_disk =
                        state.preparation_fits_disk_cache(partial_source_bytes);
                    let staging_started = Instant::now();
                    let mut processed_source_bytes = 0u64;
                    let progress_step = partial_source_bytes.div_ceil(20).max(1);
                    let mut next_progress = progress_step;
                    let observe_staging =
                        observability::enabled(observability::Detail::Normal);
                    if observe_staging {
                        observability::emit(
                            observability::Detail::Normal,
                            observability::Level::Info,
                            "par2.staging.started",
                            "PAR2 source staging started",
                            None,
                            &[observability::Field::unsigned(
                                "item_count",
                                partial_sources.len() as u64,
                            )],
                        );
                    }
                    'partial_sources: for (mut source, postings) in partial_sources {
                        // A complete repair closure is resumable; a rolling cache tail is not.
                        source.use_memory_cache = cache_partial_sources_in_memory;
                        source.admit_disk_cache = cache_partial_sources_on_disk;
                        let mut partial_stage = None;
                        let staged = state.fetch_postings_batched(
                            &source,
                            &postings,
                            true,
                            &cancelled,
                            |segment| {
                                processed_source_bytes = processed_source_bytes
                                    .saturating_add(segment.bytes().len() as u64);
                                if observe_staging && processed_source_bytes >= next_progress {
                                    observability::emit(
                                        observability::Detail::Normal,
                                        observability::Level::Info,
                                        "par2.staging.progress",
                                        "PAR2 source staging progressed",
                                        None,
                                        &[observability::Field::unsigned(
                                            "transferred_bytes",
                                            processed_source_bytes,
                                        )],
                                    );
                                    next_progress = processed_source_bytes
                                        .checked_div(progress_step)
                                        .unwrap_or(0)
                                        .saturating_add(1)
                                        .saturating_mul(progress_step);
                                }
                                if partial_stage.is_none() {
                                    let exact_size = segment.total_size;
                                    if !set.source_file_ids.iter().any(|file_id| {
                                        set.files
                                            .get(file_id)
                                            .is_some_and(|file| file.byte_size == exact_size)
                                    }) {
                                        return Err("par2_source_unmatched");
                                    }
                                    partial_stage = Some(
                                        materialization::PartialSourceStage::new(
                                            local_data,
                                            exact_size,
                                        )?,
                                    );
                                }
                                partial_stage
                                    .as_mut()
                                    .expect("created partial source stage")
                                    .push_segment(segment)
                            },
                        );
                        match staged {
                            Ok(()) => {}
                            Err("par2_source_unmatched") => continue 'partial_sources,
                            Err(code) => return Err(code),
                        }
                        let Some(mut partial_stage) = partial_stage else {
                            continue;
                        };
                        let evidence = partial_stage.evidence(set.slice_size, &cancelled)?;
                        if evidence.checksums.iter().all(Option::is_none) {
                            continue;
                        }
                        let mapping = match par2::identify_partial_source_evidence(&set, &evidence)
                        {
                            Ok(mapping) => mapping,
                            Err("par2_source_unmatched" | "par2_source_ambiguous") => continue,
                            Err(code) => return Err(code),
                        };
                        partial_source_mapped = true;
                        match known_valid.entry(mapping.file_id) {
                            std::collections::btree_map::Entry::Vacant(entry) => {
                                entry.insert(mapping.valid_slices.clone());
                            }
                            std::collections::btree_map::Entry::Occupied(mut entry) => {
                                if mapped_sources.contains_key(&mapping.file_id) {
                                    continue;
                                }
                                let known = entry.get_mut();
                                if known.len() != mapping.valid_slices.len() {
                                    return Err("par2_source_evidence_invalid");
                                }
                                for (known, valid) in
                                    known.iter_mut().zip(&mapping.valid_slices)
                                {
                                    *known |= *valid;
                                }
                            }
                        }
                        partial_mappings
                            .entry(mapping.file_id)
                            .or_default()
                            .push((partial_stage, mapping.valid_slices));
                    }
                    if observe_staging {
                        observability::emit(
                            observability::Detail::Normal,
                            observability::Level::Info,
                            "par2.staging.completed",
                            "PAR2 source staging completed",
                            None,
                            &[
                                observability::Field::unsigned(
                                    "transferred_bytes",
                                    processed_source_bytes,
                                ),
                                observability::Field::unsigned(
                                    "duration_ms",
                                    u64::try_from(staging_started.elapsed().as_millis())
                                        .unwrap_or(u64::MAX),
                                ),
                            ],
                        );
                    }
                    par2::plan_repair(&set, selected_file_id, &known_valid)?;
                    set.recovery_exponents.clear();
                    let mut stage =
                        materialization::Par2RepairStage::new_with_publication(
                            local_data,
                            artifact_data,
                        )?;
                    for file_id in &set.source_file_ids {
                        let description = set
                            .files
                            .get(file_id)
                            .ok_or("par2_file_description_missing")?;
                        if let Some((source, _asset_revision)) = mapped_sources.get(file_id) {
                            stage
                                .create_source(&description.relative_path, description.byte_size)?;
                            stage.copy_complete_source(
                                &source.content_identity,
                                source.expected_size,
                                &description.relative_path,
                                &cancelled,
                            )?;
                        } else if let Some(partials) = partial_mappings.remove(file_id) {
                            let mut partials = partials.into_iter();
                            let (partial, _valid_slices) =
                                partials.next().expect("non-empty partial source mappings");
                            stage.adopt_partial_source(&partial, &description.relative_path)?;
                            for (mut partial, valid_slices) in partials {
                                stage.copy_partial_source(
                                    &mut partial,
                                    &description.relative_path,
                                    &valid_slices,
                                    set.slice_size,
                                    &cancelled,
                                )?;
                            }
                        } else {
                            stage
                                .create_source(&description.relative_path, description.byte_size)?;
                        }
                    }
                    stage.write_index_ranges(
                        &mut inputs.readers,
                        &inputs.ranges,
                        &cancelled,
                    )?;
                    if stage.logical_bytes() != stage_bytes {
                        return Err("repair_scope_exceeds_budget");
                    }
                    drop(inputs);
                    tool.repair(stage.directory(), stage_bytes, &cancelled)?;
                    let (_path, identity, byte_size, asset_revision, ()) =
                        stage.publish_verified_selected(
                            &selected.relative_path,
                            selected.byte_size,
                            |input| {
                                par2::verify_complete_source(
                                    &set,
                                    selected_file_id,
                                    input,
                                    selected.byte_size,
                                    &cancelled,
                                )
                            },
                        )?;
                    Ok((
                        lower_hex(&set.set_id),
                        request.selected_file_id,
                        selected.relative_path.clone(),
                        identity,
                        byte_size,
                        asset_revision,
                        partial_source_mapped,
                    ))
                }) {
                Ok((
                    set_id,
                    file_id,
                    relative_path,
                    identity,
                    byte_size,
                    asset_revision,
                    partial_source_mapped,
                )) => response(
                    "200 OK",
                    &serde_json::to_string(&Par2RepairResponse {
                        version: 1u32,
                        set_id,
                        file_id,
                        relative_path,
                        identity,
                        byte_size,
                        asset_revision,
                        partial_source_mapped,
                    })
                    .expect("serialize PAR2 repair response"),
                )
                .into_bytes(),
                Err("repair_insufficient") => required_recovery_blocks.map_or_else(
                    || archive_failure_response("repair_insufficient"),
                    par2_insufficient_response,
                ),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION)) if !parser_only && is_raw_composite_open_request(first) => {
            match serde_json::from_slice::<ArchivePlanRequest>(body)
                .map_err(|_| "archive_volume_invalid")
                .and_then(|request| {
                    let _reservation = state.resources.reserve_archive_job()?;
                    plan_archive_request(
                        request,
                        artifact_data,
                        Some(&state.verified_materializations),
                        &cancelled,
                    )
                })
                .and_then(|(plan, file_identities)| {
                    let source = raw_composite::RawCompositeSource::from_plan(
                        plan.clone(),
                        file_identities,
                    )?;
                    let (identity, exact_size) = state
                        .raw_composites
                        .lock()
                        .expect("raw composite registry lock")
                        .insert(source, std::time::Instant::now())?;
                    Ok((plan, identity, exact_size))
                }) {
                Ok((plan, identity, exact_size)) => response(
                    "200 OK",
                    &format!(
                        r#"{{"version":1,"identity":"{identity}","exact_size":{exact_size},"etag":"{identity}","plan":{}}}"#,
                        serde_json::to_string(&plan).expect("serialize raw composite plan")
                    ),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && raw_composite_reader_open_identity(first).is_some() =>
        {
            let identity = raw_composite_reader_open_identity(first)
                .expect("checked raw composite identity");
            if !body.is_empty() {
                response(
                    "422 Unprocessable Content",
                    r#"{"version":1,"code":"invalid_raw_composite_reader","retryable":false}"#,
                )
                .into_bytes()
            } else {
                match state
                    .raw_composites
                    .lock()
                    .expect("raw composite registry lock")
                    .open_reader(identity, std::time::Instant::now())
                {
                    Ok(lease_id) => response(
                        "201 Created",
                        &serde_json::json!({
                            "version": 1,
                            "source_identity": identity,
                            "reader_lease_id": lease_id,
                        })
                        .to_string(),
                    )
                    .into_bytes(),
                    Err(code) => response(
                        "409 Conflict",
                        &format!(r#"{{"version":1,"code":"{code}","retryable":true}}"#),
                    )
                    .into_bytes(),
                }
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && raw_composite_reader_close_identities(first).is_some() =>
        {
            let (identity, lease_id) = raw_composite_reader_close_identities(first)
                .expect("checked raw composite reader");
            if !body.is_empty() {
                response(
                    "422 Unprocessable Content",
                    r#"{"version":1,"code":"invalid_raw_composite_reader","retryable":false}"#,
                )
                .into_bytes()
            } else {
                match state
                    .raw_composites
                    .lock()
                    .expect("raw composite registry lock")
                    .close_reader(identity, lease_id, std::time::Instant::now())
                {
                    Ok(()) => response_bytes("204 No Content", ""),
                    Err(code) => response(
                        "409 Conflict",
                        &format!(r#"{{"version":1,"code":"{code}","retryable":true}}"#),
                    )
                    .into_bytes(),
                }
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && raw_composite_native_inspect_identity(first).is_some() =>
        {
            let identity =
                raw_composite_native_inspect_identity(first).expect("checked source identity");
            match serde_json::from_slice::<MaterializationInspectRequest>(body)
                .map_err(|_| "invalid_native_inspection")
                .and_then(|request| {
                    let lease = state
                        .raw_composites
                        .lock()
                        .expect("raw composite registry lock")
                        .get(identity, std::time::Instant::now())?;
                    if request.expected_size != lease.source.exact_size() {
                        return Err("invalid_raw_composite_range");
                    }
                    let budget = u64::try_from(inspect::MAX_STRUCTURAL_END_BYTES)
                        .map_err(|_| "container_probe_budget")?;
                    let read = |offset, length| {
                        lease.source.read_at(
                            offset,
                            length,
                            &cancelled,
                            |part, start, end| {
                                state.read_composite_part(
                                    artifact_data,
                                    part,
                                    start,
                                    end,
                                    None,
                                    &cancelled,
                                )
                            },
                        )
                    };
                    let head_length = request.expected_size.min(budget);
                    let head = read(0, head_length)?;
                    let tail = if request.expected_size > budget {
                        read(request.expected_size - budget, budget)?
                    } else {
                        Vec::new()
                    };
                    inspect::probe_container(&head, &tail)
                }) {
                Ok(evidence) => response(
                    "200 OK",
                    &serde_json::json!({
                        "version": 1,
                        "source_identity": identity,
                        "inspection_state": "provisionally_streamable",
                        "container": evidence.kind.as_str(),
                        "duration_millis": evidence.duration_millis,
                        "inspected_head_bytes": evidence.inspected_head_bytes,
                        "inspected_tail_bytes": evidence.inspected_tail_bytes,
                    })
                    .to_string(),
                )
                .into_bytes(),
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && raw_composite_read_identity(first).is_some() =>
        {
            let identity =
                raw_composite_read_identity(first).expect("checked raw composite identity");
            match serde_json::from_slice::<RawCompositeRangeRequest>(body)
                .map_err(|_| "invalid_raw_composite_range")
                .and_then(|request| {
                    if !session::valid_session_id(&request.reader_lease_id) {
                        return Err("invalid_raw_composite_reader");
                    }
                    let lease = state
                        .raw_composites
                        .lock()
                        .expect("raw composite registry lock")
                        .get_with_reader(
                            identity,
                            &request.reader_lease_id,
                            std::time::Instant::now(),
                        )?;
                    if request.expected_size != lease.source.exact_size() {
                        return Err("invalid_raw_composite_range");
                    }
                    let length = request
                        .end
                        .checked_sub(request.start)
                        .and_then(|value| value.checked_add(1))
                        .ok_or("invalid_raw_composite_range")?;
                    lease
                        .source
                        .read_at(
                            request.start,
                            length,
                            &cancelled,
                            |part, start, end| {
                                state.read_composite_part(
                                    artifact_data,
                                    part,
                                    start,
                                    end,
                                    lease.prefetch_generation.as_ref(),
                                    &cancelled,
                                )
                            },
                        )
                        .map(|bytes| {
                            (bytes, request.start, request.end, request.expected_size)
                        })
                }) {
                Ok((bytes, start, end, expected_size)) => {
                    range_response(bytes, start, end, expected_size, None, &mut binary_body)
                }
                Err(code) => archive_failure_response(code),
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && session_reader_open_identity(first).is_some() =>
        {
            let identity =
                session_reader_open_identity(first).expect("checked session identity");
            if !body.is_empty() {
                response(
                    "422 Unprocessable Content",
                    r#"{"version":1,"code":"invalid_session_reader","retryable":false}"#,
                )
                .into_bytes()
            } else {
                match state
                    .sessions
                    .lock()
                    .expect("session registry lock")
                    .open_reader(identity, std::time::Instant::now())
                {
                    Ok(lease_id) => response(
                        "201 Created",
                        &serde_json::json!({
                            "version": 1,
                            "session_id": identity,
                            "reader_lease_id": lease_id,
                        })
                        .to_string(),
                    )
                    .into_bytes(),
                    Err(code) => response(
                        "409 Conflict",
                        &format!(r#"{{"version":1,"code":"{code}","retryable":true}}"#),
                    )
                    .into_bytes(),
                }
            }
        }
        (first, Some(API_VERSION))
            if !parser_only && session_reader_close_identities(first).is_some() =>
        {
            let (identity, lease_id) =
                session_reader_close_identities(first).expect("checked session reader");
            if !body.is_empty() {
                response(
                    "422 Unprocessable Content",
                    r#"{"version":1,"code":"invalid_session_reader","retryable":false}"#,
                )
                .into_bytes()
            } else {
                match state
                    .sessions
                    .lock()
                    .expect("session registry lock")
                    .close_reader(identity, lease_id, std::time::Instant::now())
                {
                    Ok(()) => response_bytes("204 No Content", ""),
                    Err(code) => response(
                        "409 Conflict",
                        &format!(r#"{{"version":1,"code":"{code}","retryable":true}}"#),
                    )
                    .into_bytes(),
                }
            }
        }
        (first, Some(API_VERSION)) if !parser_only && session_read_identity(first).is_some() => {
            let identity = session_read_identity(first).expect("checked session identity");
            match serde_json::from_slice::<SessionRangeRequest>(body)
                .map_err(|_| "invalid_session_range")
                .and_then(|request| {
                    if !session::valid_session_id(&request.reader_lease_id) {
                        return Err("invalid_session_reader");
                    }
                    let lease = state
                        .sessions
                        .lock()
                        .expect("session registry lock")
                        .get_with_reader(
                            identity,
                            &request.reader_lease_id,
                            std::time::Instant::now(),
                        )
                        ?;
                    {
                        let session = lease
                            .session
                            .lock()
                            .expect("random access session lock");
                        if request.expected_size != session.size() {
                            return Err("invalid_session_range");
                        }
                    }
                    let length = request
                        .end
                        .checked_sub(request.start)
                        .and_then(|value| value.checked_add(1))
                        .ok_or("invalid_session_range")?;
                    let bytes = session::RandomAccessSession::read_at(
                        &lease.session,
                        request.start,
                        length,
                        |posting| {
                            state.fetch_posting(&lease.context, posting, &cancelled)
                        },
                    )?;
                    let mut session = lease
                        .session
                        .lock()
                        .expect("random access session lock");
                    let salvage = session.salvage_state();
                    let (salvaged_bytes, salvaged_holes) = session.salvage_delta();
                    let prefetch_start = session.prefetch_start_after(request.end);
                    drop(session);
                    EngineState::increment_by(
                        &state.salvage_bytes_total,
                        salvaged_bytes,
                    );
                    EngineState::increment_by(
                        &state.salvage_holes_total,
                        u64::try_from(salvaged_holes)
                            .expect("session salvage hole count fits u64"),
                    );
                    state.persist_session_checkpoints(
                        &lease.recreation_key,
                        &lease.session,
                    )?;
                    if let Some(posting_index) = prefetch_start {
                        state.start_session_prefetch(
                            Arc::clone(&lease.session),
                            Arc::clone(&lease.recreation_key),
                            Arc::clone(&lease.context),
                            lease.prefetch_generation.clone(),
                            posting_index,
                        );
                    }
                    Ok((
                        bytes,
                        request.start,
                        request.end,
                        request.expected_size,
                        salvage,
                    ))
                }) {
                Ok((bytes, start, end, expected_size, salvage)) => {
                    range_response(
                        bytes,
                        start,
                        end,
                        expected_size,
                        Some(salvage),
                        &mut binary_body,
                    )
                }
                Err(code) => native_work_failure_response(code),
            }
        }
        (_, Some(API_VERSION)) => response(
            "404 Not Found",
            r#"{"version":1,"code":"not_found","retryable":false}"#,
        )
        .into_bytes(),
        _ => response(
            "400 Bad Request",
            r#"{"version":1,"code":"api_version_mismatch","retryable":false}"#,
        )
        .into_bytes(),
    };
    if stream.write_all(payload.as_ref()).is_ok()
        && let Some(body) = binary_body
    {
        let _ = stream.write_all(&body);
    }
}

fn run_request_worker(
    receiver: &Mutex<mpsc::Receiver<UnixStream>>,
    local_data: &Path,
    artifact_data: &Path,
    parser_only: bool,
    state: &Arc<EngineState>,
) {
    loop {
        if state.draining.load(Ordering::Acquire) {
            return;
        }
        let stream = match receiver.lock().expect("engine request queue lock").recv() {
            Ok(stream) => stream,
            Err(_) => return,
        };
        // A queued socket has not started native work. Once drain is visible,
        // drop it instead of extending shutdown through another request timeout.
        if state.draining.load(Ordering::Acquire) {
            return;
        }
        state.active_requests.fetch_add(1, Ordering::AcqRel);
        let _active_request = ActiveCounterPermit {
            active: &state.active_requests,
        };
        handle(stream, local_data, artifact_data, parser_only, state);
        release_allocator_slack_if_needed();
    }
}

fn admit_request(
    sender: &mpsc::SyncSender<UnixStream>,
    stream: UnixStream,
    busy_rejections: &AtomicU64,
) -> bool {
    match sender.try_send(stream) {
        Ok(()) => true,
        Err(mpsc::TrySendError::Full(mut stream)) => {
            busy_rejections.fetch_add(1, Ordering::Relaxed);
            if peer_is_current_user(&stream) && stream.set_nonblocking(true).is_ok() {
                let _ = stream.write_all(
                    response(
                        "503 Service Unavailable",
                        r#"{"version":1,"code":"native_busy","retryable":true}"#,
                    )
                    .as_bytes(),
                );
            }
            true
        }
        Err(mpsc::TrySendError::Disconnected(_)) => false,
    }
}

fn accept_error_is_transient(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::WouldBlock
            | std::io::ErrorKind::Interrupted
            | std::io::ErrorKind::ConnectionAborted
    )
}

fn close_admission(listener: UnixListener, socket: &Path) {
    drop(listener);
    let _ = fs::remove_file(socket);
}

fn wait_for_detached_work(state: &EngineState) -> bool {
    let deadline = Instant::now() + ENGINE_BACKGROUND_SHUTDOWN_TIMEOUT;
    while (state.background_prefetches.load(Ordering::Acquire) != 0
        || state.network_singleflight.active() != 0)
        && Instant::now() < deadline
    {
        thread::sleep(Duration::from_millis(10));
    }
    state.background_prefetches.load(Ordering::Acquire) == 0
        && state.network_singleflight.active() == 0
}

struct RuntimePaths {
    socket: String,
    local_data: String,
    artifact_data: String,
    memory_cache_bytes: usize,
    disk_cache_bytes: u64,
    minimum_free_disk_bytes: u64,
    maximum_nntp_connections: usize,
    maximum_spool_bytes: u64,
    maximum_archive_jobs: usize,
    maximum_repair_jobs: usize,
    par2_binary: Option<String>,
    libarchive_library: Option<String>,
    parser_only: bool,
}

fn runtime_paths_from(arguments: &[String]) -> Result<RuntimePaths, ()> {
    match arguments {
        [
            socket_flag,
            socket,
            data_flag,
            local_data,
            artifact_flag,
            artifact_data,
            memory_cache_flag,
            memory_cache_bytes,
            disk_cache_flag,
            disk_cache_bytes,
            minimum_free_flag,
            minimum_free_disk_bytes,
            maximum_connections_flag,
            maximum_nntp_connections,
            spool_flag,
            maximum_spool_bytes,
            archive_jobs_flag,
            maximum_archive_jobs,
            repair_jobs_flag,
            maximum_repair_jobs,
            par2_flag,
            par2_binary,
            libarchive_flag,
            libarchive_library,
        ] if socket_flag == "--socket"
            && data_flag == "--local-data-dir"
            && artifact_flag == "--artifact-dir"
            && memory_cache_flag == "--memory-cache-bytes"
            && disk_cache_flag == "--disk-cache-bytes"
            && minimum_free_flag == "--minimum-free-disk-bytes"
            && maximum_connections_flag == "--maximum-nntp-connections"
            && spool_flag == "--spool-max-bytes"
            && archive_jobs_flag == "--archive-jobs"
            && repair_jobs_flag == "--repair-jobs"
            && par2_flag == "--par2-binary"
            && libarchive_flag == "--libarchive-library" =>
        {
            Ok(RuntimePaths {
                socket: socket.clone(),
                local_data: local_data.clone(),
                artifact_data: artifact_data.clone(),
                memory_cache_bytes: memory_cache_bytes.parse().map_err(|_| ())?,
                disk_cache_bytes: disk_cache_bytes.parse().map_err(|_| ())?,
                minimum_free_disk_bytes: minimum_free_disk_bytes.parse().map_err(|_| ())?,
                maximum_nntp_connections: maximum_nntp_connections.parse().map_err(|_| ())?,
                maximum_spool_bytes: maximum_spool_bytes.parse().map_err(|_| ())?,
                maximum_archive_jobs: maximum_archive_jobs.parse().map_err(|_| ())?,
                maximum_repair_jobs: maximum_repair_jobs.parse().map_err(|_| ())?,
                par2_binary: Some(par2_binary.clone()),
                libarchive_library: Some(libarchive_library.clone()),
                parser_only: false,
            })
        }
        [
            socket_flag,
            socket,
            data_flag,
            local_data,
            artifact_flag,
            artifact_data,
            memory_cache_flag,
            memory_cache_bytes,
            disk_cache_flag,
            disk_cache_bytes,
            minimum_free_flag,
            minimum_free_disk_bytes,
            maximum_connections_flag,
            maximum_nntp_connections,
            parser_only,
        ] if socket_flag == "--socket"
            && data_flag == "--local-data-dir"
            && artifact_flag == "--artifact-dir"
            && memory_cache_flag == "--memory-cache-bytes"
            && disk_cache_flag == "--disk-cache-bytes"
            && minimum_free_flag == "--minimum-free-disk-bytes"
            && maximum_connections_flag == "--maximum-nntp-connections"
            && parser_only == "--parser-only" =>
        {
            Ok(RuntimePaths {
                socket: socket.clone(),
                local_data: local_data.clone(),
                artifact_data: artifact_data.clone(),
                memory_cache_bytes: memory_cache_bytes.parse().map_err(|_| ())?,
                disk_cache_bytes: disk_cache_bytes.parse().map_err(|_| ())?,
                minimum_free_disk_bytes: minimum_free_disk_bytes.parse().map_err(|_| ())?,
                maximum_nntp_connections: maximum_nntp_connections.parse().map_err(|_| ())?,
                maximum_spool_bytes: DEFAULT_SPOOL_BYTES,
                maximum_archive_jobs: DEFAULT_ARCHIVE_JOBS,
                maximum_repair_jobs: DEFAULT_REPAIR_JOBS,
                par2_binary: None,
                libarchive_library: None,
                parser_only: true,
            })
        }
        _ => Err(()),
    }
}

fn main() {
    observability::install_panic_hook();
    let arguments_os = env::args_os().skip(1).collect::<Vec<_>>();
    let archive_worker = arguments_os
        .first()
        .is_some_and(|value| value == "--archive-worker" || value == "--archive-catalog-worker");
    if archive_worker {
        observability::install_silent_panic_hook();
        if install_parent_death_signal().is_err() {
            std::process::exit(78);
        }
        let Ok(arguments) = arguments_os
            .into_iter()
            .map(|value| value.into_string())
            .collect::<Result<Vec<_>, _>>()
        else {
            std::process::exit(78);
        };
        std::process::exit(archive_runtime::worker(&arguments));
    }
    let format = observability::environment_format();
    if observability::initialize_from_environment().is_err()
        || install_parent_death_signal().is_err()
    {
        observability::emergency("runtime.bootstrap.failed", format);
        std::process::exit(78);
    }
    let Ok(arguments) = arguments_os
        .into_iter()
        .map(|value| value.into_string())
        .collect::<Result<Vec<_>, _>>()
    else {
        observability::emergency("runtime.bootstrap.failed", format);
        std::process::exit(78);
    };
    let Ok(paths) = runtime_paths_from(&arguments) else {
        observability::emit(
            observability::Detail::Quiet,
            observability::Level::Critical,
            "native.startup.failed",
            "Native engine startup failed",
            None,
            &[observability::Field::token(
                "error_code",
                "invalid_arguments",
            )],
        );
        std::process::exit(78);
    };
    if run_engine(paths).is_err() {
        observability::emit(
            observability::Detail::Quiet,
            observability::Level::Critical,
            "native.startup.failed",
            "Native engine startup failed",
            None,
            &[observability::Field::token(
                "error_code",
                "initialization_failure",
            )],
        );
        std::process::exit(1);
    }
}

fn run_engine(paths: RuntimePaths) -> Result<(), ()> {
    let socket = Path::new(&paths.socket);
    let local_data = Path::new(&paths.local_data);
    let artifact_data = Path::new(&paths.artifact_data);
    fs::create_dir_all(local_data).map_err(|_| ())?;
    fs::set_permissions(local_data, fs::Permissions::from_mode(0o700)).map_err(|_| ())?;
    fs::create_dir_all(artifact_data).map_err(|_| ())?;
    fs::set_permissions(artifact_data, fs::Permissions::from_mode(0o700)).map_err(|_| ())?;
    materialization::cleanup_staging(local_data).map_err(|_| ())?;
    let state = Arc::new(
        if paths.parser_only {
            EngineState::new(
                local_data,
                paths.memory_cache_bytes,
                0,
                paths.minimum_free_disk_bytes,
                paths.maximum_nntp_connections,
            )
        } else {
            EngineState::new_with_par2(
                local_data,
                NativeBudgets {
                    memory_cache_bytes: paths.memory_cache_bytes,
                    disk_cache_bytes: paths.disk_cache_bytes,
                    minimum_free_disk_bytes: paths.minimum_free_disk_bytes,
                    maximum_nntp_connections: paths.maximum_nntp_connections,
                    maximum_spool_bytes: paths.maximum_spool_bytes,
                    maximum_archive_jobs: paths.maximum_archive_jobs,
                    maximum_repair_jobs: paths.maximum_repair_jobs,
                },
                paths.par2_binary.as_deref().map(Path::new),
                paths.libarchive_library.as_deref().map(Path::new),
            )
        }
        .map_err(|_| ())?,
    );
    if socket.exists() {
        fs::remove_file(socket).map_err(|_| ())?;
    }
    let listener = UnixListener::bind(socket).map_err(|_| ())?;
    fs::set_permissions(socket, fs::Permissions::from_mode(0o600)).map_err(|_| ())?;
    listener.set_nonblocking(true).map_err(|_| ())?;
    let (sender, receiver) = mpsc::sync_channel(ENGINE_REQUEST_QUEUE);
    let receiver = Arc::new(Mutex::new(receiver));
    let local_data = Arc::new(local_data.to_path_buf());
    let artifact_data = Arc::new(artifact_data.to_path_buf());
    let mut workers = Vec::with_capacity(ENGINE_REQUEST_WORKERS);
    for _ in 0..ENGINE_REQUEST_WORKERS {
        let receiver = Arc::clone(&receiver);
        let local_data = Arc::clone(&local_data);
        let artifact_data = Arc::clone(&artifact_data);
        let state = Arc::clone(&state);
        let parser_only = paths.parser_only;
        workers.push(
            thread::Builder::new()
                .spawn(move || {
                    run_request_worker(&receiver, &local_data, &artifact_data, parser_only, &state);
                })
                .map_err(|_| ())?,
        );
    }
    // Only workers own the receiving side. If they all fail, admission observes
    // channel disconnection instead of filling an orphaned queue forever.
    drop(receiver);
    while !state.draining.load(Ordering::Acquire) {
        match listener.accept() {
            Ok((stream, _)) => {
                if !admit_request(&sender, stream, &state.request_queue_busy_rejections) {
                    state.draining.store(true, Ordering::Release);
                    break;
                }
            }
            Err(error) if accept_error_is_transient(&error) => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(_) => {
                observability::emit(
                    observability::Detail::Quiet,
                    observability::Level::Error,
                    "native.socket.failed",
                    "Native engine socket failed",
                    None,
                    &[observability::Field::token("error_code", "socket_failure")],
                );
                state.draining.store(true, Ordering::Release);
                break;
            }
        }
    }
    // Unlink and close the listener before waiting: accepted active sockets
    // remain valid, while no new caller can enter the kernel backlog.
    close_admission(listener, socket);
    drop(sender);
    for worker in workers {
        if worker.join().is_err() {
            observability::emit(
                observability::Detail::Quiet,
                observability::Level::Error,
                "native.worker.failed",
                "Native engine worker failed",
                None,
                &[observability::Field::token("error_code", "worker_panic")],
            );
        }
    }
    if !wait_for_detached_work(&state) {
        observability::emit(
            observability::Detail::Quiet,
            observability::Level::Warning,
            "native.shutdown.timeout",
            "Native engine shutdown timed out",
            None,
            &[
                observability::Field::token("error_code", "detached_work_timeout"),
                observability::Field::unsigned(
                    "duration_ms",
                    ENGINE_BACKGROUND_SHUTDOWN_TIMEOUT.as_millis() as u64,
                ),
            ],
        );
    }
    Ok(())
}

fn install_parent_death_signal() -> std::io::Result<()> {
    let parent = unsafe { libc::getppid() };
    if let Ok(expected) = env::var("COMET_SUPERVISOR_PID") {
        validate_supervisor_parent(&expected, parent)?;
    }
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM, 0, 0, 0) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    if unsafe { libc::getppid() } != parent {
        return Err(std::io::Error::from_raw_os_error(libc::ESRCH));
    }
    Ok(())
}

fn validate_supervisor_parent(expected: &str, parent: libc::pid_t) -> std::io::Result<()> {
    let expected = expected
        .parse::<libc::pid_t>()
        .map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
    if expected < 1 || parent != expected {
        return Err(std::io::Error::from_raw_os_error(libc::ESRCH));
    }
    Ok(())
}

#[cfg(test)]
mod parent_death_tests {
    use super::validate_supervisor_parent;

    #[test]
    fn accepts_container_init_as_the_exact_supervisor_parent() {
        assert!(validate_supervisor_parent("1", 1).is_ok());
    }

    #[test]
    fn rejects_invalid_or_mismatched_supervisor_parents() {
        assert_eq!(
            validate_supervisor_parent("0", 0)
                .expect_err("PID zero is not a supervisor")
                .raw_os_error(),
            Some(libc::ESRCH)
        );
        assert_eq!(
            validate_supervisor_parent("41", 42)
                .expect_err("the actual parent must match")
                .raw_os_error(),
            Some(libc::ESRCH)
        );
        assert_eq!(
            validate_supervisor_parent("not-a-pid", 42)
                .expect_err("the expected parent must be numeric")
                .raw_os_error(),
            Some(libc::EINVAL)
        );
    }
}

#[cfg(test)]
mod request_tests {
    use super::{
        ArchivePlanVolumeRequest, MAX_CONTROL_REQUEST_BYTES, MAX_NATIVE_CATALOG_BYTES,
        MAX_NZB_BYTES, MAX_NZB_METADATA_BYTES, MAX_REQUEST_HEADER_BYTES, RequestBodyBudget,
        accept_error_is_transient, admit_request, artifact_native_catalog_identity,
        artifact_native_inspect_identity, canonical_par2_volume_identities, close_admission,
        is_archive_direct_catalog_request, is_archive_direct_open_request, is_archive_plan_request,
        is_artifact_parse_request, is_materialization_request, is_native_work_request,
        is_par2_discovery_request, is_par2_repair_request, is_par2_source_map_request,
        is_raw_composite_open_request, is_session_archive_catalog_request,
        is_session_archive_open_request, materialization_native_inspect_identity,
        maximum_request_body, observe_archive_probe_failure, parse_nzb,
        parse_nzb_with_metadata_limit, raw_composite_native_inspect_identity,
        raw_composite_read_identity, raw_composite_reader_close_identities,
        raw_composite_reader_open_identity, request_releases_allocator_slack, run_request_worker,
        validate_par2_files, validated_archive_passphrase,
    };
    use md5::Md5;
    use sha2::{Digest, Sha256};
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;
    use std::time::{Duration, Instant};

    #[test]
    fn archive_probe_preserves_the_causal_failure_over_cancellation() {
        let mut observed = None;
        observe_archive_probe_failure(&mut observed, "nntp_cancelled");
        observe_archive_probe_failure(&mut observed, "invalid_yenc_crc");
        observe_archive_probe_failure(&mut observed, "nntp_article_missing");
        assert_eq!(observed, Some("invalid_yenc_crc"));
    }

    fn temporary_directory(label: &str) -> PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("comet-{label}-{}-{nonce}", std::process::id()));
        std::fs::create_dir(&root).expect("create engine test directory");
        root
    }

    fn engine_response(
        request: &[u8],
        local_data: PathBuf,
        parser_only: bool,
        state: Arc<super::EngineState>,
    ) -> Vec<u8> {
        let artifact_data = local_data.clone();
        engine_response_with_artifacts(request, local_data, artifact_data, parser_only, state)
    }

    fn engine_response_with_artifacts(
        request: &[u8],
        local_data: PathBuf,
        artifact_data: PathBuf,
        parser_only: bool,
        state: Arc<super::EngineState>,
    ) -> Vec<u8> {
        let (mut client, server) = UnixStream::pair().expect("create engine socket pair");
        let handler = thread::spawn(move || {
            super::handle(server, &local_data, &artifact_data, parser_only, &state);
        });
        client.write_all(request).expect("write engine request");
        client
            .shutdown(std::net::Shutdown::Write)
            .expect("close engine request");
        let mut response = Vec::new();
        client
            .read_to_end(&mut response)
            .expect("read engine response");
        handler.join().expect("join engine request handler");
        response
    }

    fn register_provider_from_request(
        request: &[u8],
        local_data: &Path,
        state: &Arc<super::EngineState>,
    ) -> Vec<u8> {
        let header_end = request
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .expect("find fixture request body");
        let first_end = request
            .windows(2)
            .position(|window| window == b"\r\n")
            .expect("find fixture request line");
        let first =
            std::str::from_utf8(&request[..first_end]).expect("decode fixture request line");
        let mut payload: serde_json::Value =
            serde_json::from_slice(&request[header_end + 4..]).expect("decode fixture request");
        let object = payload.as_object_mut().expect("fixture request object");
        let servers = object.remove("servers").expect("fixture provider servers");
        let account_partition = object["account_partition"].clone();
        let generation = object["provider_set_generation"]
            .as_str()
            .expect("fixture provider generation")
            .to_owned();
        let registration = serde_json::to_vec(&serde_json::json!({
            "servers": servers,
            "account_partition": account_partition,
        }))
        .expect("encode fixture provider registration");
        let mut registration_request = format!(
            "PUT /v1/provider-sets/{generation} HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            registration.len()
        )
        .into_bytes();
        registration_request.extend_from_slice(&registration);
        let registration_response = engine_response(
            &registration_request,
            local_data.to_path_buf(),
            false,
            Arc::clone(state),
        );
        assert!(
            registration_response.starts_with(b"HTTP/1.1 200"),
            "fixture provider registration failed: {}",
            String::from_utf8_lossy(&registration_response)
        );
        let response_body = registration_response
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &registration_response[index + 4..])
            .expect("fixture registration response body");
        let registered: serde_json::Value =
            serde_json::from_slice(response_body).expect("decode fixture registration response");
        object.remove("provider_set_generation");
        object.insert(
            "provider_set_id".to_owned(),
            registered["provider_set_id"].clone(),
        );

        let body = serde_json::to_vec(&payload).expect("encode fixture operation");
        let mut operation = format!(
            "{first}\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        operation.extend_from_slice(&body);
        operation
    }

    fn provider_registration_request(generation: &str, priority: u16) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [{
                "provider_configuration_id": "primary",
                "host": "news.example.test",
                "port": 563,
                "tls_mode": "implicit",
                "allow_private": false,
                "username": "user",
                "password": "secret",
                "connections": 4,
                "pipeline": 2,
                "priority": priority,
                "backup": false
            }],
            "account_partition": "a".repeat(64),
        }))
        .expect("encode provider registration");
        let mut request = format!(
            "PUT /v1/provider-sets/{generation} HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn materialization_request(port: u16, provider_configuration_id: &str) -> Vec<u8> {
        materialization_request_for_generation(port, provider_configuration_id, "b", 2)
    }

    fn materialization_request_for_generation(
        port: u16,
        provider_configuration_id: &str,
        generation: &str,
        connections: usize,
    ) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [{
                "provider_configuration_id": provider_configuration_id,
                "host": "127.0.0.1",
                "port": port,
                "tls_mode": "plaintext",
                "allow_private": true,
                "username": null,
                "password": null,
                "connections": connections,
                "pipeline": 1,
                "priority": 0,
                "backup": false
            }],
            "postings": [{"number": 1, "bytes": 1, "message_id": "article@example.test"}],
            "account_partition": "a".repeat(64),
            "provider_set_generation": generation.repeat(64)
        }))
        .expect("serialize materialization request");
        let mut request = format!(
            "POST /v1/materializations HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn fallback_materialization_request(port: u16) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [{
                "provider_configuration_id": "primary",
                "host": "127.0.0.1",
                "port": port,
                "tls_mode": "plaintext",
                "allow_private": true,
                "username": null,
                "password": null,
                "connections": 1,
                "pipeline": 1,
                "priority": 0,
                "backup": false
            }],
            "postings": [
                {"number": 1, "bytes": 1, "message_id": "missing@example.test"},
                {"number": 1, "bytes": 1, "message_id": "fallback@example.test"}
            ],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "b".repeat(64)
        }))
        .expect("serialize fallback materialization request");
        let mut request = format!(
            "POST /v1/materializations HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn fallback_failover_materialization_request(primary_port: u16, backup_port: u16) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [
                {
                    "provider_configuration_id": "primary",
                    "host": "127.0.0.1",
                    "port": primary_port,
                    "tls_mode": "plaintext",
                    "allow_private": true,
                    "username": null,
                    "password": null,
                    "connections": 1,
                    "pipeline": 1,
                    "priority": 0,
                    "backup": false
                },
                {
                    "provider_configuration_id": "backup",
                    "host": "127.0.0.1",
                    "port": backup_port,
                    "tls_mode": "plaintext",
                    "allow_private": true,
                    "username": null,
                    "password": null,
                    "connections": 1,
                    "pipeline": 1,
                    "priority": 0,
                    "backup": true
                }
            ],
            "postings": [
                {"number": 1, "bytes": 1, "message_id": "missing@example.test"},
                {"number": 1, "bytes": 1, "message_id": "fallback@example.test"}
            ],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "b".repeat(64)
        }))
        .expect("serialize fallback failover request");
        let mut request = format!(
            "POST /v1/materializations HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn failover_materialization_request(primary_port: u16, backup_port: u16) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [
                {
                    "provider_configuration_id": "backup",
                    "host": "127.0.0.1",
                    "port": backup_port,
                    "tls_mode": "plaintext",
                    "allow_private": true,
                    "username": null,
                    "password": null,
                    "connections": 1,
                    "pipeline": 1,
                    "priority": 0,
                    "backup": true
                },
                {
                    "provider_configuration_id": "primary",
                    "host": "127.0.0.1",
                    "port": primary_port,
                    "tls_mode": "plaintext",
                    "allow_private": true,
                    "username": null,
                    "password": null,
                    "connections": 1,
                    "pipeline": 1,
                    "priority": 0,
                    "backup": false
                }
            ],
            "postings": [{"number": 1, "bytes": 1, "message_id": "article@example.test"}],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "b".repeat(64)
        }))
        .expect("serialize failover materialization request");
        let mut request = format!(
            "POST /v1/materializations HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn multipart_session_request(port: u16) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [{
                "provider_configuration_id": "primary",
                "host": "127.0.0.1",
                "port": port,
                "tls_mode": "plaintext",
                "allow_private": true,
                "username": null,
                "password": null,
                "connections": 1,
                "pipeline": 1,
                "priority": 0,
                "backup": false
            }],
            "postings": [
                {"number": 1, "bytes": 10, "message_id": "one@example.test"},
                {"number": 2, "bytes": 10, "message_id": "two@example.test"},
                {"number": 3, "bytes": 10, "message_id": "three@example.test"}
            ],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "b".repeat(64)
        }))
        .expect("serialize random access session request");
        let mut request = format!(
            "POST /v1/sessions HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn salvage_session_request(port: u16) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [{
                "provider_configuration_id": "primary",
                "host": "127.0.0.1",
                "port": port,
                "tls_mode": "plaintext",
                "allow_private": true,
                "username": null,
                "password": null,
                "connections": 1,
                "pipeline": 1,
                "priority": 0,
                "backup": false
            }],
            "postings": [
                {"number": 1, "bytes": 10, "message_id": "one@example.test"},
                {"number": 2, "bytes": 10, "message_id": "missing@example.test"},
                {"number": 3, "bytes": 1200, "message_id": "three@example.test"}
            ],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "d".repeat(64),
            "allow_degraded_playback": true
        }))
        .expect("serialize salvage session request");
        let mut request = format!(
            "POST /v1/sessions HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn hedged_session_request(primary_port: u16, backup_port: u16, preparation: bool) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [
                {
                    "provider_configuration_id": "primary",
                    "host": "127.0.0.1",
                    "port": primary_port,
                    "tls_mode": "plaintext",
                    "allow_private": true,
                    "username": null,
                    "password": null,
                    "connections": 1,
                    "pipeline": 1,
                    "priority": 0,
                    "backup": false
                },
                {
                    "provider_configuration_id": "backup",
                    "host": "127.0.0.1",
                    "port": backup_port,
                    "tls_mode": "plaintext",
                    "allow_private": true,
                    "username": null,
                    "password": null,
                    "connections": 1,
                    "pipeline": 1,
                    "priority": 0,
                    "backup": true
                }
            ],
            "postings": [
                {"number": 1, "bytes": 3, "message_id": "hedged@example.test"}
            ],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "b".repeat(64),
            "preparation": preparation
        }))
        .expect("serialize hedged session request");
        let mut request = format!(
            "POST /v1/sessions HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    fn verified_article_server() -> (u16, thread::JoinHandle<()>) {
        verified_article_server_with_delay(Duration::ZERO)
    }

    fn verified_article_server_with_delay(delay: Duration) -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind cache NNTP server");
        let port = listener.local_addr().expect("read cache NNTP port").port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept cache NNTP connection");
            let mut reader = BufReader::new(stream.try_clone().expect("clone cache NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 cache test server ready\r\n")
                .expect("write cache NNTP greeting");
            for (index, response) in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"222 body follows\r\n=ybegin line=128 size=3 name=test\r\nklm\r\n=yend size=3 crc32=a3830348\r\n.\r\n".as_slice(),
            ]
            .into_iter()
            .enumerate()
            {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read cache NNTP command");
                if index == 3 {
                    thread::sleep(delay);
                }
                writer.write_all(response).expect("write cache NNTP response");
            }
        });
        (port, server)
    }

    fn stalled_article_server() -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind stalled NNTP server");
        let port = listener
            .local_addr()
            .expect("read stalled NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept stalled NNTP connection");
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .expect("bound stalled server read");
            let mut reader = BufReader::new(stream.try_clone().expect("clone stalled NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 stalled test server ready\r\n")
                .expect("write stalled NNTP greeting");
            let mut commands = Vec::new();
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read stalled NNTP setup command");
                commands.push(command);
                writer
                    .write_all(response)
                    .expect("write stalled NNTP setup response");
            }
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read stalled NNTP BODY");
            commands.push(command);
            let mut closed = [0_u8; 1];
            assert_eq!(
                reader
                    .read(&mut closed)
                    .expect("wait for hedge loser close"),
                0
            );
            commands
        });
        (port, server)
    }

    fn synchronized_date_test_server(
        date_barrier: Arc<std::sync::Barrier>,
    ) -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind parallel DATE NNTP server");
        let port = listener
            .local_addr()
            .expect("read parallel DATE NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept parallel DATE NNTP connection");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone parallel DATE NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 parallel DATE test server ready\r\n")
                .expect("write parallel DATE NNTP greeting");
            let mut commands = Vec::new();
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read parallel DATE NNTP command");
                commands.push(command);
                writer
                    .write_all(response)
                    .expect("write parallel DATE NNTP response");
            }
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read parallel DATE command");
            commands.push(command);
            date_barrier.wait();
            writer
                .write_all(b"111 20260727123456\r\n")
                .expect("write parallel DATE response");
            commands
        });
        (port, server)
    }

    fn missing_article_server() -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind missing NNTP server");
        let port = listener
            .local_addr()
            .expect("read missing NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept missing NNTP connection");
            let mut reader = BufReader::new(stream.try_clone().expect("clone missing NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 missing test server ready\r\n")
                .expect("write missing NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"430 no such article\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read missing NNTP command");
                writer
                    .write_all(response)
                    .expect("write missing NNTP response");
            }
        });
        (port, server)
    }

    fn fallback_article_server() -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fallback NNTP server");
        let port = listener
            .local_addr()
            .expect("read fallback NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept fallback NNTP connection");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone fallback NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 fallback test server ready\r\n")
                .expect("write fallback NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read fallback NNTP setup command");
                writer
                    .write_all(response)
                    .expect("write fallback NNTP setup response");
            }
            let mut commands = Vec::new();
            for response in [
                b"430 no such article\r\n".as_slice(),
                b"222 body follows\r\n=ybegin line=128 size=1 name=fallback\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read fallback NNTP BODY");
                commands.push(command);
                writer
                    .write_all(response)
                    .expect("write fallback NNTP BODY response");
            }
            commands
        });
        (port, server)
    }

    fn corrupt_fallback_article_server() -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind corrupt fallback NNTP server");
        let port = listener
            .local_addr()
            .expect("read corrupt fallback NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept corrupt fallback NNTP connection");
            let mut reader = BufReader::new(
                stream
                    .try_clone()
                    .expect("clone corrupt fallback NNTP stream"),
            );
            let mut writer = stream;
            writer
                .write_all(b"200 corrupt fallback test server ready\r\n")
                .expect("write corrupt fallback NNTP greeting");
            for setup_response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read corrupt fallback setup command");
                writer
                    .write_all(setup_response)
                    .expect("write corrupt fallback setup response");
            }
            let mut commands = Vec::new();
            for response in [
                b"222 body follows\r\n=ybegin line=128 size=1 name=corrupt\r\nk\r\n=yend size=1 crc32=00000000\r\n.\r\n".as_slice(),
                b"222 body follows\r\n=ybegin line=128 size=1 name=fallback\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read corrupt fallback BODY");
                commands.push(command);
                writer
                    .write_all(response)
                    .expect("write corrupt fallback BODY response");
            }
            commands
        });
        (port, server)
    }

    fn failed_body_server() -> (u16, thread::JoinHandle<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind failed-BODY NNTP server");
        let port = listener
            .local_addr()
            .expect("read failed-BODY NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept failed-BODY connection");
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(1)))
                .expect("bound failed-BODY reads");
            let mut reader = BufReader::new(stream.try_clone().expect("clone failed-BODY stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 failed-BODY test server ready\r\n")
                .expect("write failed-BODY greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read failed-BODY setup command");
                writer
                    .write_all(response)
                    .expect("write failed-BODY setup response");
            }
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read failed-BODY command");
            writer
                .write_all(b"500 provider failure\r\n")
                .expect("write failed-BODY response");
            let mut byte = [0_u8; 1];
            assert_eq!(
                reader
                    .read(&mut byte)
                    .expect("observe failed provider connection close"),
                0
            );
            command
        });
        (port, server)
    }

    fn blocking_verified_article_server() -> (
        u16,
        std::sync::mpsc::Receiver<()>,
        std::sync::mpsc::SyncSender<()>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind singleflight NNTP server");
        let port = listener
            .local_addr()
            .expect("read singleflight NNTP port")
            .port();
        let (body_seen, receive_body_seen) = std::sync::mpsc::sync_channel(0);
        let (release_body, receive_release_body) = std::sync::mpsc::sync_channel(0);
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept singleflight NNTP connection");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone singleflight NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 singleflight test server ready\r\n")
                .expect("write singleflight NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read singleflight NNTP command");
                writer
                    .write_all(response)
                    .expect("write singleflight NNTP response");
            }
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read singleflight BODY command");
            body_seen
                .send(())
                .expect("report singleflight BODY command");
            receive_release_body
                .recv()
                .expect("release singleflight BODY response");
            writer
                .write_all(
                    b"222 body follows\r\n=ybegin line=128 size=3 name=test\r\nklm\r\n=yend size=3 crc32=a3830348\r\n.\r\n",
                )
                .expect("write singleflight BODY response");
        });
        (port, receive_body_seen, release_body, server)
    }

    fn disconnect_stalled_article_server() -> (
        u16,
        std::sync::mpsc::Receiver<()>,
        std::sync::mpsc::Receiver<()>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind disconnect NNTP server");
        let port = listener
            .local_addr()
            .expect("read disconnect NNTP port")
            .port();
        let (body_seen, receive_body_seen) = std::sync::mpsc::sync_channel(0);
        let (socket_closed, receive_socket_closed) = std::sync::mpsc::sync_channel(0);
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept disconnect NNTP connection");
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(2)))
                .expect("bound disconnect server read");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone disconnect NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 disconnect test server ready\r\n")
                .expect("write disconnect NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read disconnect NNTP command");
                writer
                    .write_all(response)
                    .expect("write disconnect NNTP response");
            }
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read disconnect BODY command");
            writer
                .write_all(b"222 body follows\r\n=ybegin")
                .expect("write stalled NNTP body");
            body_seen.send(()).expect("report stalled NNTP body");
            let mut byte = [0u8; 1];
            assert_eq!(
                reader
                    .read(&mut byte)
                    .expect("observe cancelled NNTP socket"),
                0
            );
            socket_closed
                .send(())
                .expect("report cancelled NNTP socket");
        });
        (port, receive_body_seen, receive_socket_closed, server)
    }

    fn obsolete_prefetch_server() -> (
        u16,
        std::sync::mpsc::Receiver<()>,
        std::sync::mpsc::Receiver<()>,
        thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind prefetch cancel server");
        let port = listener
            .local_addr()
            .expect("read prefetch cancel port")
            .port();
        let (body_seen, receive_body_seen) = std::sync::mpsc::sync_channel(0);
        let (socket_closed, receive_socket_closed) = std::sync::mpsc::sync_channel(0);
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept prefetch cancel connection");
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(2)))
                .expect("bound prefetch cancel server read");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone prefetch cancel stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 prefetch cancel server ready\r\n")
                .expect("write prefetch cancel greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read prefetch cancel command");
                writer
                    .write_all(response)
                    .expect("write prefetch cancel response");
            }
            let mut first = String::new();
            reader
                .read_line(&mut first)
                .expect("read first prefetch cancel BODY");
            assert_eq!(first, "BODY <one@example.test>\r\n");
            writer
                .write_all(
                    b"222 body follows\r\n=ybegin line=128 size=9 name=test\r\n=ypart begin=1 end=3\r\nklm\r\n=yend size=3 pcrc32=a3830348\r\n.\r\n",
                )
                .expect("write first prefetch cancel article");
            let mut second = String::new();
            reader
                .read_line(&mut second)
                .expect("read obsolete prefetch BODY");
            assert!(
                matches!(
                    second.as_str(),
                    "BODY <two@example.test>\r\n" | "BODY <three@example.test>\r\n"
                ),
                "unexpected obsolete prefetch command: {second:?}"
            );
            writer
                .write_all(b"222 body follows\r\n=ybegin")
                .expect("write stalled obsolete prefetch body");
            body_seen.send(()).expect("report obsolete prefetch BODY");
            let mut byte = [0u8; 1];
            assert_eq!(
                reader
                    .read(&mut byte)
                    .expect("observe obsolete prefetch cancellation"),
                0
            );
            socket_closed
                .send(())
                .expect("report obsolete prefetch socket close");
        });
        (port, receive_body_seen, receive_socket_closed, server)
    }

    fn multipart_article_server(expected_bodies: usize) -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind multipart NNTP server");
        let port = listener
            .local_addr()
            .expect("read multipart NNTP port")
            .port();
        let server = thread::spawn(move || {
            let mut fetched = Vec::new();
            let (stream, _) = listener.accept().expect("accept multipart NNTP connection");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone multipart NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 multipart test server ready\r\n")
                .expect("write multipart NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read multipart NNTP command");
                writer
                    .write_all(response)
                    .expect("write multipart NNTP response");
            }
            for _ in 0..expected_bodies {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read multipart BODY command");
                let message_id = command
                    .trim()
                    .strip_prefix("BODY <")
                    .and_then(|value| value.strip_suffix('>'))
                    .expect("canonical multipart BODY command")
                    .to_owned();
                let article = match message_id.as_str() {
                    "one@example.test" => {
                        b"222 body follows\r\n=ybegin line=128 size=9 name=test\r\n=ypart begin=1 end=3\r\nklm\r\n=yend size=3 pcrc32=a3830348\r\n.\r\n".as_slice()
                    }
                    "two@example.test" => {
                        b"222 body follows\r\n=ybegin line=128 size=9 name=test\r\n=ypart begin=4 end=6\r\nnop\r\n=yend size=3 pcrc32=9a63a3eb\r\n.\r\n".as_slice()
                    }
                    "three@example.test" => {
                        b"222 body follows\r\n=ybegin line=128 size=9 name=test\r\n=ypart begin=7 end=9\r\nqrs\r\n=yend size=3 pcrc32=bd347e6e\r\n.\r\n".as_slice()
                    }
                    _ => panic!("unexpected sparse mapping fetch: {message_id}"),
                };
                fetched.push(message_id);
                writer
                    .write_all(article)
                    .expect("write multipart NNTP article");
            }
            fetched
        });
        (port, server)
    }

    fn iso_box(kind: &[u8; 4], payload: &[u8]) -> Vec<u8> {
        let mut result = Vec::with_capacity(payload.len() + 8);
        result.extend_from_slice(
            &u32::try_from(payload.len() + 8)
                .expect("bounded ISO fixture")
                .to_be_bytes(),
        );
        result.extend_from_slice(kind);
        result.extend_from_slice(payload);
        result
    }

    fn inspection_mp4() -> Vec<u8> {
        let mut payload = vec![0_u8; 12];
        payload.extend_from_slice(&1000_u32.to_be_bytes());
        payload.extend_from_slice(&90_500_u32.to_be_bytes());
        let mut media = iso_box(b"ftyp", b"isom\0\0\0\0isommp42");
        media.extend_from_slice(&iso_box(b"moov", &iso_box(b"mvhd", &payload)));
        media
    }

    fn yenc_article(payload: &[u8]) -> Vec<u8> {
        let mut encoded = Vec::with_capacity(payload.len() * 2);
        for byte in payload {
            let transformed = byte.wrapping_add(42);
            if matches!(transformed, 0 | b'\n' | b'\r' | b'=') {
                encoded.push(b'=');
                encoded.push(transformed.wrapping_add(64));
            } else {
                encoded.push(transformed);
            }
        }
        let mut article = format!(
            "222 body follows\r\n=ybegin line=128 size={} name=video.mp4\r\n",
            payload.len()
        )
        .into_bytes();
        article.extend_from_slice(&encoded);
        article.extend_from_slice(
            format!(
                "\r\n=yend size={} crc32={:08x}\r\n.\r\n",
                payload.len(),
                crc32fast::hash(payload)
            )
            .as_bytes(),
        );
        article
    }

    fn par2_packet(set_id: &[u8; 16], kind: &[u8; 16], body: &[u8]) -> Vec<u8> {
        assert!((64 + body.len()).is_multiple_of(4));
        let mut packet = Vec::with_capacity(64 + body.len());
        packet.extend_from_slice(b"PAR2\0PKT");
        packet.extend_from_slice(&u64::try_from(64 + body.len()).unwrap().to_le_bytes());
        packet.extend_from_slice(&[0; 16]);
        packet.extend_from_slice(set_id);
        packet.extend_from_slice(kind);
        packet.extend_from_slice(body);
        let checksum: [u8; 16] = Md5::digest(&packet[32..]).into();
        packet[16..32].copy_from_slice(&checksum);
        packet
    }

    fn yenc_part_article(payload: &[u8], begin: u64, total_size: u64) -> Vec<u8> {
        let mut encoded = Vec::with_capacity(payload.len() * 2);
        for byte in payload {
            let transformed = byte.wrapping_add(42);
            if matches!(transformed, 0 | b'\n' | b'\r' | b'=') {
                encoded.push(b'=');
                encoded.push(transformed.wrapping_add(64));
            } else {
                encoded.push(transformed);
            }
        }
        let end = begin + payload.len() as u64 - 1;
        let mut article = format!(
            "222 body follows\r\n=ybegin line=128 size={total_size} name=Movie.mkv\r\n\
             =ypart begin={begin} end={end}\r\n"
        )
        .into_bytes();
        article.extend_from_slice(&encoded);
        article.extend_from_slice(
            format!(
                "\r\n=yend size={} pcrc32={:08x}\r\n.\r\n",
                payload.len(),
                crc32fast::hash(payload)
            )
            .as_bytes(),
        );
        article
    }

    fn salvage_article_server() -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind salvage NNTP server");
        let port = listener
            .local_addr()
            .expect("read salvage NNTP port")
            .port();
        let server = thread::spawn(move || {
            let mut fetched = Vec::new();
            let (stream, _) = listener.accept().expect("accept salvage NNTP connection");
            let mut reader = BufReader::new(stream.try_clone().expect("clone salvage NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 salvage test server ready\r\n")
                .expect("write salvage NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read salvage NNTP command");
                writer
                    .write_all(response)
                    .expect("write salvage NNTP response");
            }
            for _ in 0..3 {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read salvage BODY command");
                let message_id = command
                    .trim()
                    .strip_prefix("BODY <")
                    .and_then(|value| value.strip_suffix('>'))
                    .expect("canonical salvage BODY command")
                    .to_owned();
                let article = match message_id.as_str() {
                    "one@example.test" => yenc_part_article(b"ABC", 1, 1200),
                    "missing@example.test" => b"430 no such article\r\n".to_vec(),
                    "three@example.test" => yenc_part_article(&vec![b'Z'; 1194], 7, 1200),
                    _ => panic!("unexpected salvage mapping fetch: {message_id}"),
                };
                fetched.push(message_id);
                writer
                    .write_all(&article)
                    .expect("write salvage NNTP article");
            }
            fetched
        });
        (port, server)
    }

    fn partial_repair_article_server() -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind partial repair NNTP server");
        let port = listener
            .local_addr()
            .expect("read partial repair NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept partial repair NNTP connection");
            let mut reader = BufReader::new(
                stream
                    .try_clone()
                    .expect("clone partial repair NNTP stream"),
            );
            let mut writer = stream;
            writer
                .write_all(b"200 partial repair test ready\r\n")
                .expect("write partial repair greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read partial repair setup");
                writer
                    .write_all(response)
                    .expect("write partial repair setup");
            }
            let mut commands = Vec::new();
            for _ in 0..5 {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read partial repair BODY");
                let response = if command.contains("unrelated@example.test") {
                    yenc_part_article(b"JUNK", 1, 20)
                } else if command.contains("known-first@example.test") {
                    yenc_part_article(b"DATA", 1, 12)
                } else if command.contains("known-second@example.test") {
                    yenc_part_article(b"MORE", 5, 12)
                } else if command.contains("missing-") {
                    b"430 no such article\r\n".to_vec()
                } else {
                    panic!("unexpected partial repair command after {commands:?}: {command}");
                };
                commands.push(command);
                writer
                    .write_all(&response)
                    .expect("write partial repair article response");
            }
            commands
        });
        (port, server)
    }

    fn stat_article_server(
        articles: &'static [(&'static str, bool)],
    ) -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind STAT NNTP server");
        let port = listener.local_addr().expect("read STAT NNTP port").port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept STAT NNTP connection");
            let mut reader = BufReader::new(stream.try_clone().expect("clone STAT NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 STAT test server ready\r\n")
                .expect("write STAT NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader.read_line(&mut command).expect("read STAT setup");
                writer.write_all(response).expect("write STAT setup");
            }
            let mut commands = Vec::with_capacity(articles.len());
            for (message_id, present) in articles {
                let mut command = String::new();
                reader.read_line(&mut command).expect("read STAT command");
                assert_eq!(command, format!("STAT <{message_id}>\r\n"));
                commands.push(command);
                writer
                    .write_all(if *present {
                        b"223 1 article exists\r\n"
                    } else {
                        b"430 no such article\r\n"
                    })
                    .expect("write STAT response");
            }
            commands
        });
        (port, server)
    }

    fn inspection_article_server(payload: Vec<u8>) -> (u16, thread::JoinHandle<Vec<String>>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind inspection NNTP server");
        let port = listener
            .local_addr()
            .expect("read inspection NNTP port")
            .port();
        let server = thread::spawn(move || {
            let mut commands = Vec::new();
            let (stream, _) = listener
                .accept()
                .expect("accept inspection NNTP connection");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone inspection NNTP stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 inspection test server ready\r\n")
                .expect("write inspection NNTP greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read inspection NNTP command");
                writer
                    .write_all(response)
                    .expect("write inspection NNTP response");
            }
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read inspection BODY command");
            commands.push(command);
            writer
                .write_all(&yenc_article(&payload))
                .expect("write inspection article");
            commands
        });
        (port, server)
    }

    fn native_inspection_request(port: u16, encoded_bytes: usize) -> Vec<u8> {
        let body = serde_json::to_vec(&serde_json::json!({
            "servers": [{
                "provider_configuration_id": "primary",
                "host": "127.0.0.1",
                "port": port,
                "tls_mode": "plaintext",
                "allow_private": true,
                "username": null,
                "password": null,
                "connections": 2,
                "pipeline": 1,
                "priority": 0,
                "backup": false
            }],
            "postings": [{
                "number": 1,
                "bytes": encoded_bytes,
                "message_id": "inspection@example.test"
            }],
            "account_partition": "a".repeat(64),
            "provider_set_generation": "b".repeat(64)
        }))
        .expect("serialize native inspection request");
        let mut request = format!(
            "POST /v1/artifacts/{}/native-inspect HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            "c".repeat(64),
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        request
    }

    #[test]
    fn par2_volume_identities_are_canonical_independently_of_file_order() {
        let files = vec![
            ArchivePlanVolumeRequest {
                content_identity: "f".repeat(64),
                relative_path: "release.par2".to_owned(),
                expected_size: 10,
            },
            ArchivePlanVolumeRequest {
                content_identity: "a".repeat(64),
                relative_path: "release.vol00+01.par2".to_owned(),
                expected_size: 20,
            },
        ];

        assert_eq!(
            canonical_par2_volume_identities(&files, vec![0, 1]).unwrap(),
            vec!["a".repeat(64), "f".repeat(64)]
        );
    }

    #[test]
    fn par2_validation_has_no_sixty_four_volume_shape_assumption() {
        let mut files = (0..65)
            .map(|index| ArchivePlanVolumeRequest {
                content_identity: format!("{index:064x}"),
                relative_path: format!("release.vol{index:03}+01.par2"),
                expected_size: 1,
            })
            .collect();

        validate_par2_files(&mut files).expect("validate all legitimate PAR2 volumes");
    }

    fn parser_only_response(request: &[u8]) -> Vec<u8> {
        let root = temporary_directory("parser-only");
        let socket = root.join("engine.sock");
        let listener = UnixListener::bind(&socket).expect("bind parser-only test socket");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create parser-only local data directory");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 32)
                .expect("initialize parser-only engine state"),
        );
        let handler = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept parser-only request");
            super::handle(stream, &local_data, &local_data, true, &state);
        });
        let mut client = UnixStream::connect(&socket).expect("connect parser-only test socket");
        client
            .write_all(request)
            .expect("write parser-only request");
        client
            .shutdown(std::net::Shutdown::Write)
            .expect("close parser-only request");
        let mut response = Vec::new();
        client
            .read_to_end(&mut response)
            .expect("read parser-only response");
        handler.join().expect("join parser-only request handler");
        std::fs::remove_dir_all(root).expect("remove parser-only test directory");
        response
    }

    #[test]
    fn accepts_only_canonical_artifact_parse_paths() {
        assert!(is_artifact_parse_request(&format!(
            "POST /v1/artifacts/{}/parse HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(!is_artifact_parse_request(
            "POST /v1/artifacts/not-a-digest/parse HTTP/1.1"
        ));
        assert!(!is_artifact_parse_request(&format!(
            "POST /v1/artifacts/{}/parse/extra HTTP/1.1",
            "a".repeat(64)
        )));
    }

    #[test]
    fn accepts_only_canonical_native_inspection_paths() {
        let artifact = "a".repeat(64);
        assert_eq!(
            artifact_native_inspect_identity(&format!(
                "POST /v1/artifacts/{artifact}/native-inspect HTTP/1.1"
            )),
            Some(artifact.as_str())
        );
        assert!(
            artifact_native_inspect_identity(
                "POST /v1/artifacts/not-a-digest/native-inspect HTTP/1.1"
            )
            .is_none()
        );
        assert!(
            artifact_native_inspect_identity(&format!(
                "GET /v1/artifacts/{artifact}/native-inspect HTTP/1.1"
            ))
            .is_none()
        );
    }

    #[test]
    fn accepts_only_canonical_native_catalog_paths() {
        let artifact = "a".repeat(64);
        assert_eq!(
            artifact_native_catalog_identity(&format!(
                "POST /v1/artifacts/{artifact}/native-catalog HTTP/1.1"
            )),
            Some(artifact.as_str())
        );
        assert!(
            artifact_native_catalog_identity(
                "POST /v1/artifacts/not-a-digest/native-catalog HTTP/1.1"
            )
            .is_none()
        );
        assert!(
            artifact_native_catalog_identity(&format!(
                "GET /v1/artifacts/{artifact}/native-catalog HTTP/1.1"
            ))
            .is_none()
        );
    }

    #[test]
    fn disconnect_detection_does_not_confuse_a_request_half_close() {
        let (client, server) = UnixStream::pair().expect("create disconnect test pair");
        assert!(!super::peer_disconnected(&server));
        client
            .shutdown(std::net::Shutdown::Write)
            .expect("half-close disconnect test request");
        assert!(!super::peer_disconnected(&server));
        drop(client);
        assert!(super::peer_disconnected(&server));
    }

    #[test]
    fn runtime_arguments_require_the_calculator_only_in_native_mode() {
        let common = [
            "--socket",
            "/run/comet/engine.sock",
            "--local-data-dir",
            "/var/lib/comet/usenet",
            "--artifact-dir",
            "/var/lib/comet/artifacts",
            "--memory-cache-bytes",
            "268435456",
            "--disk-cache-bytes",
            "2147483648",
            "--minimum-free-disk-bytes",
            "5368709120",
            "--maximum-nntp-connections",
            "32",
        ]
        .map(str::to_owned);
        let mut native = common.to_vec();
        native.extend([
            "--spool-max-bytes".to_owned(),
            "107374182400".to_owned(),
            "--archive-jobs".to_owned(),
            "2".to_owned(),
            "--repair-jobs".to_owned(),
            "1".to_owned(),
            "--par2-binary".to_owned(),
            "/app/bin/par2".to_owned(),
            "--libarchive-library".to_owned(),
            "/app/lib/libarchive.so.13".to_owned(),
        ]);
        let Ok(native) = super::runtime_paths_from(&native) else {
            panic!("valid native runtime arguments");
        };
        assert_eq!(native.par2_binary.as_deref(), Some("/app/bin/par2"));
        assert_eq!(
            native.libarchive_library.as_deref(),
            Some("/app/lib/libarchive.so.13")
        );
        assert_eq!(native.maximum_spool_bytes, 107_374_182_400);
        assert_eq!(native.maximum_archive_jobs, 2);
        assert_eq!(native.maximum_repair_jobs, 1);
        assert!(!native.parser_only);

        let mut parser = common.to_vec();
        parser.push("--parser-only".to_owned());
        let Ok(parser) = super::runtime_paths_from(&parser) else {
            panic!("valid parser runtime arguments");
        };
        assert_eq!(parser.par2_binary, None);
        assert_eq!(parser.libarchive_library, None);
        assert_eq!(parser.maximum_spool_bytes, super::DEFAULT_SPOOL_BYTES);
        assert_eq!(parser.maximum_archive_jobs, super::DEFAULT_ARCHIVE_JOBS);
        assert_eq!(parser.maximum_repair_jobs, super::DEFAULT_REPAIR_JOBS);
        assert!(parser.parser_only);
    }

    #[test]
    fn archive_passphrases_preserve_supported_utf8_bytes() {
        assert_eq!(
            validated_archive_passphrase(Some(String::new()))
                .unwrap()
                .as_ref()
                .map(|value| value.as_str()),
            Some("")
        );
        assert_eq!(
            validated_archive_passphrase(Some("line one\nline two".to_owned()))
                .unwrap()
                .as_ref()
                .map(|value| value.as_str()),
            Some("line one\nline two")
        );
        assert_eq!(
            validated_archive_passphrase(Some("bad\0secret".to_owned())).err(),
            Some("archive_passphrase_invalid")
        );
    }

    #[test]
    fn archive_requests_ignore_unconsumed_fields() {
        serde_json::from_value::<super::ArchiveNestedCatalogRequest>(serde_json::json!({
            "volumes": [],
            "future_metadata": {"opaque": true},
        }))
        .expect("catalog request accepts unconsumed metadata");
        serde_json::from_value::<super::ArchiveNestedExtractionRequest>(serde_json::json!({
            "volumes": [],
            "expected_output_size": 1,
            "selected_paths": ["movie.mkv"],
            "future_metadata": {"opaque": true},
        }))
        .expect("extraction request accepts unconsumed metadata");
    }

    #[test]
    fn accepts_only_canonical_materialization_native_inspect_paths() {
        let identity = "a".repeat(64);
        assert_eq!(
            materialization_native_inspect_identity(&format!(
                "POST /v1/materializations/{identity}/native-inspect HTTP/1.1"
            )),
            Some(identity.as_str())
        );
        assert!(
            materialization_native_inspect_identity(&format!(
                "GET /v1/materializations/{identity}/native-inspect HTTP/1.1"
            ))
            .is_none()
        );
    }

    #[test]
    fn accepts_only_canonical_account_partitions() {
        assert!(super::provider::decode_partition(&"a".repeat(64)).is_ok());
        assert!(super::provider::decode_partition(&"A".repeat(64)).is_err());
        assert!(super::provider::decode_partition("a").is_err());
    }

    #[test]
    fn provider_set_route_is_idempotent_and_rejects_generation_conflicts() {
        let root = temporary_directory("provider-set-route");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create provider set route data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 4)
                .expect("initialize provider set route state"),
        );
        let generation = "b".repeat(64);
        let request = provider_registration_request(&generation, 10);
        let mut identities = Vec::new();
        for _ in 0..2 {
            let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
            assert!(response.starts_with(b"HTTP/1.1 200"));
            let body = response
                .windows(4)
                .position(|window| window == b"\r\n\r\n")
                .map(|index| &response[index + 4..])
                .expect("provider registration response body");
            let registered: serde_json::Value =
                serde_json::from_slice(body).expect("decode provider registration");
            let identity = registered["provider_set_id"]
                .as_str()
                .expect("provider set random identity");
            assert!(super::provider::valid_identity(identity));
            assert_eq!(registered["generation"], generation);
            identities.push(identity.to_owned());
        }
        assert_eq!(identities[0], identities[1]);
        let conflict = engine_response(
            &provider_registration_request(&generation, 20),
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        assert!(conflict.starts_with(b"HTTP/1.1 409"));
        assert!(
            conflict
                .windows(b"provider_set_conflict".len())
                .any(|value| value == b"provider_set_conflict")
        );
        assert_eq!(
            state
                .provider_sets
                .lock()
                .expect("provider set registry lock")
                .len(std::time::Instant::now()),
            1
        );
        std::fs::remove_dir_all(root).expect("remove provider set route data");
    }

    #[test]
    fn provider_set_tests_servers_in_parallel_and_reports_configured_order() {
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let (first_port, first_server) = synchronized_date_test_server(Arc::clone(&barrier));
        let (second_port, second_server) = synchronized_date_test_server(barrier);
        let pools = Arc::new(
            super::nntp::PoolRegistry::new(2).expect("create parallel provider test pools"),
        );
        let registration: super::provider::Registration =
            serde_json::from_value(serde_json::json!({
                "servers": [
                    {
                        "provider_configuration_id": "listed-first",
                        "host": "127.0.0.1",
                        "port": first_port,
                        "tls_mode": "plaintext",
                        "allow_private": true,
                        "username": null,
                        "password": null,
                        "connections": 1,
                        "pipeline": 1,
                        "priority": 0,
                        "backup": false
                    },
                    {
                        "provider_configuration_id": "listed-second",
                        "host": "127.0.0.1",
                        "port": second_port,
                        "tls_mode": "plaintext",
                        "allow_private": true,
                        "username": null,
                        "password": null,
                        "connections": 1,
                        "pipeline": 1,
                        "priority": 0,
                        "backup": false
                    }
                ],
                "account_partition": "a".repeat(64)
            }))
            .expect("deserialize parallel provider test registration");
        let provider_set = super::provider::Registry::new(Arc::clone(&pools))
            .register(&"b".repeat(64), registration, std::time::Instant::now())
            .expect("register parallel provider test set");

        let results = super::provider_test_results(&pools, &provider_set);

        assert_eq!(
            results
                .iter()
                .map(|result| {
                    result["provider_configuration_id"]
                        .as_str()
                        .expect("provider test identifier")
                })
                .collect::<Vec<_>>(),
            ["listed-first", "listed-second"]
        );
        assert!(results.iter().all(|result| result["ok"] == true));
        for server in [first_server, second_server] {
            assert_eq!(
                server.join().expect("join parallel DATE server"),
                [
                    "CAPABILITIES\r\n",
                    "MODE READER\r\n",
                    "CAPABILITIES\r\n",
                    "DATE\r\n",
                ]
            );
        }
    }

    #[test]
    fn provider_set_test_deadline_cancels_active_and_queued_servers() {
        let listener =
            TcpListener::bind("127.0.0.1:0").expect("bind deadline provider test server");
        let port = listener
            .local_addr()
            .expect("read deadline provider test port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept deadline provider test connection");
            stream
                .set_read_timeout(Some(std::time::Duration::from_secs(1)))
                .expect("bound deadline provider test close");
            let mut reader = BufReader::new(
                stream
                    .try_clone()
                    .expect("clone deadline provider test stream"),
            );
            let mut writer = stream;
            writer
                .write_all(b"200 deadline provider test ready\r\n")
                .expect("write deadline provider test greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader
                    .read_line(&mut command)
                    .expect("read deadline provider test setup");
                writer
                    .write_all(response)
                    .expect("write deadline provider test setup");
            }
            let mut date = String::new();
            reader
                .read_line(&mut date)
                .expect("read deadline provider DATE command");
            assert_eq!(date, "DATE\r\n");
            let mut closed = String::new();
            assert_eq!(
                reader
                    .read_line(&mut closed)
                    .expect("observe deadline provider close"),
                0
            );
            listener
                .set_nonblocking(true)
                .expect("inspect queued provider connection");
            assert!(matches!(
                listener.accept(),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock
            ));
        });
        let pools = Arc::new(
            super::nntp::PoolRegistry::new(1).expect("create deadline provider test pools"),
        );
        let registration: super::provider::Registration =
            serde_json::from_value(serde_json::json!({
                "servers": [
                    {
                        "provider_configuration_id": "active",
                        "host": "127.0.0.1",
                        "port": port,
                        "tls_mode": "plaintext",
                        "allow_private": true,
                        "username": null,
                        "password": null,
                        "connections": 1,
                        "pipeline": 1,
                        "priority": 0,
                        "backup": false
                    },
                    {
                        "provider_configuration_id": "queued",
                        "host": "127.0.0.1",
                        "port": port,
                        "tls_mode": "plaintext",
                        "allow_private": true,
                        "username": null,
                        "password": null,
                        "connections": 1,
                        "pipeline": 1,
                        "priority": 1,
                        "backup": false
                    }
                ],
                "account_partition": "a".repeat(64)
            }))
            .expect("deserialize deadline provider test registration");
        let provider_set = super::provider::Registry::new(Arc::clone(&pools))
            .register(&"b".repeat(64), registration, std::time::Instant::now())
            .expect("register deadline provider test set");
        let started = std::time::Instant::now();
        let results = super::provider_test_results_until(
            &pools,
            &provider_set,
            started + std::time::Duration::from_millis(250),
        );

        assert!(started.elapsed() < std::time::Duration::from_secs(1));
        assert_eq!(
            results
                .iter()
                .map(|result| result["provider_configuration_id"]
                    .as_str()
                    .expect("deadline provider identifier"))
                .collect::<Vec<_>>(),
            ["active", "queued"]
        );
        for result in results {
            assert_eq!(result["ok"], false);
            assert_eq!(result["code"], "nntp_provider_test_deadline");
            assert!(
                result["checks"]
                    .as_array()
                    .expect("deadline provider checks")
                    .iter()
                    .all(|check| check["status"] == "not_run")
            );
        }
        let stats = pools.stats();
        assert_eq!(stats.open, 0);
        assert_eq!(stats.active, 0);
        assert_eq!(stats.idle, 0);
        assert_eq!(stats.poisoned, 1);
        server.join().expect("join deadline provider test server");
    }

    #[test]
    fn native_provider_routing_orders_primary_priority_before_backups() {
        let registration: super::provider::Registration =
            serde_json::from_value(serde_json::json!({
            "servers": [
                {"provider_configuration_id": "backup", "host": "backup.example.test", "port": 563, "tls_mode": "implicit", "allow_private": false, "username": "user", "password": "secret", "connections": 2, "pipeline": 1, "priority": 0, "backup": true},
                {"provider_configuration_id": "later", "host": "later.example.test", "port": 563, "tls_mode": "implicit", "allow_private": false, "username": "user", "password": "secret", "connections": 2, "pipeline": 1, "priority": 20, "backup": false},
                {"provider_configuration_id": "first", "host": "first.example.test", "port": 563, "tls_mode": "implicit", "allow_private": false, "username": "user", "password": "secret", "connections": 2, "pipeline": 1, "priority": 10, "backup": false}
            ],
            "account_partition": "a".repeat(64)
        }))
        .expect("deserialize provider registration");
        let provider_set = super::provider::Registry::new(Arc::new(
            super::nntp::PoolRegistry::new(16).expect("create routing test pools"),
        ))
        .register(&"b".repeat(64), registration, std::time::Instant::now())
        .expect("normalize provider routing");
        assert_eq!(
            provider_set
                .servers
                .iter()
                .map(|server| server.provider_configuration_id.as_str())
                .collect::<Vec<_>>(),
            ["first", "later", "backup"]
        );
    }

    #[test]
    fn interactive_provider_hedge_is_delayed_bounded_and_cancels_the_loser() {
        let root = temporary_directory("interactive-provider-hedge");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create hedge local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 2)
                .expect("initialize hedge engine state"),
        );
        let (primary_port, primary) = stalled_article_server();
        let (backup_port, backup) = verified_article_server();
        let request = register_provider_from_request(
            &hedged_session_request(primary_port, backup_port, false),
            &local_data,
            &state,
        );

        let started = Instant::now();
        let response = engine_response(&request, local_data, false, Arc::clone(&state));
        let elapsed = started.elapsed();

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert!(elapsed >= Duration::from_millis(450));
        assert!(elapsed < Duration::from_millis(1500));
        assert_eq!(
            primary.join().expect("join stalled hedge primary"),
            [
                "CAPABILITIES\r\n",
                "MODE READER\r\n",
                "CAPABILITIES\r\n",
                "BODY <hedged@example.test>\r\n",
            ]
        );
        backup.join().expect("join hedge backup");
        let deadline = Instant::now() + Duration::from_secs(1);
        while state.network_singleflight.active() != 0 && Instant::now() < deadline {
            thread::yield_now();
        }
        assert_eq!(state.network_singleflight.active(), 0);
        assert_eq!(state.active_hedges.load(Ordering::Acquire), 0);
        assert_eq!(state.hedges_started.load(Ordering::Relaxed), 1);
        assert_eq!(state.hedges_won.load(Ordering::Relaxed), 1);
        let stats = state.nntp_pools.stats();
        assert_eq!(stats.provider_attempts, 2);
        assert_eq!(stats.provider_suppliers, 1);
        assert_eq!(stats.provider_failovers, 1);
        assert_eq!(stats.poisoned, 1);
        std::fs::remove_dir_all(root).expect("remove hedge test directory");
    }

    #[test]
    fn preparation_session_avoids_redundant_hedges_but_stays_interactive_after_open() {
        let root = temporary_directory("preparation-provider-routing");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create preparation routing data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 2)
                .expect("initialize preparation routing state"),
        );
        let (primary_port, primary) =
            verified_article_server_with_delay(Duration::from_millis(650));
        let unavailable = TcpListener::bind("127.0.0.1:0").expect("reserve unused backup port");
        let backup_port = unavailable
            .local_addr()
            .expect("read unused backup port")
            .port();
        drop(unavailable);
        let request = register_provider_from_request(
            &hedged_session_request(primary_port, backup_port, true),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));

        primary.join().expect("join delayed preparation server");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(state.hedges_started.load(Ordering::Relaxed), 0);
        assert_eq!(state.nntp_pools.stats().provider_attempts, 1);
        let body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("preparation session response body");
        let opened: serde_json::Value =
            serde_json::from_slice(body).expect("parse preparation session response");
        let identity = opened["identity"]
            .as_str()
            .expect("preparation session identity");
        let lease = state
            .sessions
            .lock()
            .expect("session registry lock")
            .get(identity, Instant::now())
            .expect("retain preparation session");
        assert_eq!(
            lease.context.work_class,
            super::nntp::WorkClass::Interactive
        );
        std::fs::remove_dir_all(root).expect("remove preparation routing directory");
    }

    #[test]
    fn native_provider_retries_duplicate_postings_before_the_next_provider() {
        let root = temporary_directory("duplicate-posting-fallback");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create duplicate fallback local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize duplicate fallback engine state"),
        );
        let (port, server) = fallback_article_server();
        let request = register_provider_from_request(
            &fallback_materialization_request(port),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data, false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(
            server.join().expect("join fallback article server"),
            [
                "BODY <missing@example.test>\r\n",
                "BODY <fallback@example.test>\r\n",
            ]
        );
        std::fs::remove_dir_all(root).expect("remove duplicate fallback directory");
    }

    #[test]
    fn native_provider_replaces_a_corrupt_lease_before_duplicate_fallback() {
        let root = temporary_directory("corrupt-duplicate-fallback");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create corrupt fallback local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize corrupt fallback engine state"),
        );
        let (port, server) = corrupt_fallback_article_server();
        let request = register_provider_from_request(
            &fallback_materialization_request(port),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data, false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(
            server.join().expect("join corrupt fallback server"),
            [
                "BODY <missing@example.test>\r\n",
                "BODY <fallback@example.test>\r\n",
            ]
        );
        let stats = state.nntp_pools.stats();
        assert_eq!(stats.open, 1);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.poisoned, 0);
        std::fs::remove_dir_all(root).expect("remove corrupt fallback directory");
    }

    #[test]
    fn native_provider_outage_skips_its_duplicate_before_backup_fallbacks() {
        let root = temporary_directory("duplicate-provider-outage");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create duplicate outage local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 2)
                .expect("initialize duplicate outage engine state"),
        );
        let (primary_port, primary) = failed_body_server();
        let (backup_port, backup) = fallback_article_server();
        let request = register_provider_from_request(
            &fallback_failover_materialization_request(primary_port, backup_port),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data, false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(
            primary.join().expect("join failed primary server"),
            "BODY <missing@example.test>\r\n"
        );
        assert_eq!(
            backup.join().expect("join fallback backup server"),
            [
                "BODY <missing@example.test>\r\n",
                "BODY <fallback@example.test>\r\n",
            ]
        );
        std::fs::remove_dir_all(root).expect("remove duplicate outage directory");
    }

    #[test]
    fn native_provider_routing_falls_back_after_a_primary_miss() {
        let root = temporary_directory("primary-miss-failover");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create primary miss local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 2)
                .expect("initialize failover engine state"),
        );
        let (primary_port, primary) = missing_article_server();
        let (backup_port, backup) = verified_article_server();
        let request = register_provider_from_request(
            &failover_materialization_request(primary_port, backup_port),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));

        primary.join().expect("join missing primary server");
        backup.join().expect("join successful backup server");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        let stats = state.nntp_pools.stats();
        assert_eq!(stats.provider_attempts, 2);
        assert_eq!(stats.provider_suppliers, 1);
        assert_eq!(stats.provider_failovers, 1);
        let cached = engine_response(&request, local_data, false, Arc::clone(&state));
        assert!(cached.starts_with(b"HTTP/1.1 200"));
        let cached_stats = state.nntp_pools.stats();
        assert_eq!(cached_stats.provider_attempts, 2);
        assert_eq!(cached_stats.provider_failovers, 1);
        std::fs::remove_dir_all(root).expect("remove primary miss test directory");
    }

    #[test]
    fn native_provider_routing_falls_back_after_a_primary_outage() {
        let root = temporary_directory("primary-outage-failover");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create primary outage local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 2)
                .expect("initialize outage engine state"),
        );
        let unavailable = TcpListener::bind("127.0.0.1:0").expect("reserve unavailable port");
        let primary_port = unavailable
            .local_addr()
            .expect("read unavailable primary port")
            .port();
        drop(unavailable);
        let (backup_port, backup) = verified_article_server();
        let request = register_provider_from_request(
            &failover_materialization_request(primary_port, backup_port),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data, false, Arc::clone(&state));

        backup.join().expect("join outage backup server");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        let stats = state.nntp_pools.stats();
        assert_eq!(stats.provider_attempts, 2);
        assert_eq!(stats.provider_suppliers, 1);
        assert_eq!(stats.provider_failovers, 1);
        std::fs::remove_dir_all(root).expect("remove primary outage test directory");
    }

    #[test]
    fn native_single_provider_reports_the_exact_retryable_outage() {
        let root = temporary_directory("single-provider-outage");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create single provider outage data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize single provider outage state"),
        );
        let unavailable = TcpListener::bind("127.0.0.1:0").expect("reserve unavailable port");
        let port = unavailable
            .local_addr()
            .expect("read unavailable port")
            .port();
        drop(unavailable);
        let request = register_provider_from_request(
            &materialization_request(port, "primary"),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data, false, state);

        assert!(response.starts_with(b"HTTP/1.1 503"));
        assert!(
            response.ends_with(br#"{"version":1,"code":"nntp_connect_failed","retryable":true}"#)
        );
        std::fs::remove_dir_all(root).expect("remove single provider outage directory");
    }

    #[test]
    fn native_provider_routing_never_turns_partial_topology_failure_into_absence() {
        for unavailable_first in [true, false] {
            let root = temporary_directory("inconclusive-failover");
            let local_data = root.join("data");
            std::fs::create_dir(&local_data).expect("create inconclusive local data");
            let state = Arc::new(
                super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 2)
                    .expect("initialize inconclusive engine state"),
            );
            let unavailable = TcpListener::bind("127.0.0.1:0").expect("reserve inconclusive port");
            let unavailable_port = unavailable
                .local_addr()
                .expect("read inconclusive port")
                .port();
            drop(unavailable);
            let (missing_port, missing) = missing_article_server();
            let (primary_port, backup_port) = if unavailable_first {
                (unavailable_port, missing_port)
            } else {
                (missing_port, unavailable_port)
            };
            let request = register_provider_from_request(
                &failover_materialization_request(primary_port, backup_port),
                &local_data,
                &state,
            );

            let response = engine_response(&request, local_data, false, Arc::clone(&state));

            missing.join().expect("join inconclusive missing server");
            let response = String::from_utf8(response).expect("decode inconclusive response");
            assert!(response.starts_with("HTTP/1.1 503"));
            assert!(response.contains(r#""code":"nntp_availability_unknown","retryable":true"#));
            assert!(!response.contains("nntp_article_missing"));
            std::fs::remove_dir_all(root).expect("remove inconclusive test directory");
        }
    }

    #[test]
    fn native_provider_failure_scope_preserves_only_unanimous_authentication() {
        let mut authentication = super::PostingFailureAggregate::new(1);
        authentication.observe("nntp_auth_required");
        authentication.observe("nntp_auth_failed");
        assert_eq!(authentication.finish(), "nntp_auth_required");

        let mut mixed_provider_failure = super::PostingFailureAggregate::new(1);
        mixed_provider_failure.observe("nntp_auth_failed");
        mixed_provider_failure.observe("nntp_connect_failed");
        assert_eq!(mixed_provider_failure.finish(), "nntp_availability_unknown");

        let mut mixed_content_failure = super::PostingFailureAggregate::new(1);
        mixed_content_failure.observe("nntp_article_missing");
        mixed_content_failure.observe("nntp_auth_failed");
        assert_eq!(mixed_content_failure.finish(), "nntp_availability_unknown");

        let mut terminal_content = super::PostingFailureAggregate::new(1);
        terminal_content.observe("nntp_article_missing");
        assert_eq!(terminal_content.finish(), "nntp_article_missing");

        let mut exact_transient = super::PostingFailureAggregate::new(1);
        exact_transient.observe("nntp_connect_failed");
        exact_transient.observe("nntp_connect_failed");
        assert_eq!(exact_transient.finish(), "nntp_connect_failed");

        let mut ambiguous_transient = super::PostingFailureAggregate::new(2);
        ambiguous_transient.observe("nntp_connect_failed");
        assert_eq!(ambiguous_transient.finish(), "nntp_availability_unknown");
    }

    #[test]
    fn native_materialization_publishes_to_the_shared_artifact_root() {
        let root = temporary_directory("shared-materialization");
        let local_data = root.join("replica-a");
        let artifact_data = root.join("shared-artifacts");
        std::fs::create_dir(&local_data).expect("create replica-local data");
        std::fs::create_dir(&artifact_data).expect("create shared artifact data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 32 * 1024 * 1024, 0, 32)
                .expect("initialize shared materialization engine"),
        );
        let (port, server) = verified_article_server();
        let request = register_provider_from_request(
            &materialization_request(port, "primary"),
            &local_data,
            &state,
        );

        let response = engine_response_with_artifacts(
            &request,
            local_data.clone(),
            artifact_data.clone(),
            false,
            state,
        );

        server.join().expect("join shared materialization server");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        let body = response
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("find shared materialization response");
        let payload: serde_json::Value =
            serde_json::from_slice(body).expect("decode shared materialization response");
        let identity = payload["identity"]
            .as_str()
            .expect("shared materialization identity");
        let byte_size = payload["byte_size"]
            .as_u64()
            .expect("shared materialization size");
        let expected = artifact_data
            .join("materialized")
            .join(format!("{identity}.bin"));
        assert!(expected.is_file());
        assert!(!local_data.join("materialized").exists());
        let replica_b = root.join("replica-b");
        std::fs::create_dir(&replica_b).expect("create second replica-local data");
        let state_b = Arc::new(
            super::EngineState::new(&replica_b, 16 * 1024 * 1024, 32 * 1024 * 1024, 0, 32)
                .expect("initialize second shared materialization engine"),
        );
        let inspect_body = serde_json::json!({
            "expected_size": byte_size,
        })
        .to_string();
        let mut inspect_request = format!(
            "POST /v1/materializations/{identity}/native-inspect HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            inspect_body.len()
        )
        .into_bytes();
        inspect_request.extend_from_slice(inspect_body.as_bytes());
        let reopened = engine_response_with_artifacts(
            &inspect_request,
            replica_b.clone(),
            artifact_data,
            false,
            state_b,
        );
        assert!(reopened.starts_with(b"HTTP/1.1 422"));
        assert!(
            reopened
                .windows(b"container_signature_mismatch".len())
                .any(|value| value == b"container_signature_mismatch")
        );
        assert!(!replica_b.join("materialized").exists());
        std::fs::remove_dir_all(root).expect("remove shared materialization test directory");
    }

    #[test]
    fn materialization_reuses_complete_partitioned_memory_and_disk_segments() {
        let root = temporary_directory("segment-cache");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create cache test local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 32 * 1024 * 1024, 0, 32)
                .expect("initialize cache test engine state"),
        );
        let (port, server) = verified_article_server();
        let request = register_provider_from_request(
            &materialization_request(port, "primary"),
            &local_data,
            &state,
        );

        let first = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        server.join().expect("join cache NNTP server");
        state
            .segment_cache
            .lock()
            .expect("segment cache lock")
            .purge();
        let second = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let foreign_request = register_provider_from_request(
            &materialization_request_for_generation(port, "backup", "c", 2),
            &local_data,
            &state,
        );
        let foreign_provider =
            engine_response(&foreign_request, local_data, false, Arc::clone(&state));

        let first = String::from_utf8(first).expect("first materialization response");
        let second = String::from_utf8(second).expect("second materialization response");
        assert!(first.starts_with("HTTP/1.1 200"));
        assert!(second.starts_with("HTTP/1.1 200"));
        assert!(first.contains(
            r#""asset_revision":"b37f7ae393301b129dd6f0370c14bd67e4954ab64cd772c724c32952d06c8cee""#
        ));
        assert!(second.contains(
            r#""asset_revision":"b37f7ae393301b129dd6f0370c14bd67e4954ab64cd772c724c32952d06c8cee""#
        ));
        assert_eq!(
            state
                .disk_cache
                .as_ref()
                .expect("disk cache")
                .lock()
                .expect("disk cache lock")
                .stats()
                .expect("disk cache stats")
                .mappings,
            1
        );
        assert!(
            !String::from_utf8(foreign_provider)
                .unwrap()
                .starts_with("HTTP/1.1 200")
        );
        assert_eq!(
            state
                .segment_cache
                .lock()
                .expect("segment cache lock")
                .stats()
                .entries,
            1
        );
        std::fs::remove_dir_all(root).expect("remove cache test directory");
    }

    #[test]
    fn concurrent_materializations_share_one_background_network_fill() {
        let root = temporary_directory("network-singleflight");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create singleflight local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 32)
                .expect("initialize singleflight engine state"),
        );
        let (port, body_seen, release_body, server) = blocking_verified_article_server();
        let request = register_provider_from_request(
            &materialization_request(port, "primary"),
            &local_data,
            &state,
        );
        let first_state = Arc::clone(&state);
        let first_data = local_data.clone();
        let first_request = request.clone();
        let first =
            thread::spawn(move || engine_response(&first_request, first_data, false, first_state));
        body_seen
            .recv_timeout(std::time::Duration::from_secs(1))
            .expect("first fill reached BODY");
        let second_state = Arc::clone(&state);
        let second_data = local_data.clone();
        let second =
            thread::spawn(move || engine_response(&request, second_data, false, second_state));
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        while state.network_singleflight.waiters() != 2 && std::time::Instant::now() < deadline {
            thread::yield_now();
        }
        assert_eq!(state.network_singleflight.waiters(), 2);
        release_body.send(()).expect("release shared BODY");

        let first = first.join().expect("join first materialization");
        let second = second.join().expect("join second materialization");
        server.join().expect("join singleflight NNTP server");
        let first = String::from_utf8(first).expect("first UTF-8 response");
        let second = String::from_utf8(second).expect("second UTF-8 response");
        assert!(first.starts_with("HTTP/1.1 200"), "{first}");
        assert!(second.starts_with("HTTP/1.1 200"), "{second}");
        assert_eq!(state.network_singleflight.active(), 0);
        std::fs::remove_dir_all(root).expect("remove singleflight test directory");
    }

    #[test]
    fn uds_disconnect_cancels_the_last_waiter_and_stalled_nntp_body() {
        let root = temporary_directory("disconnect-cancellation");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create disconnect local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize disconnect engine state"),
        );
        let (port, body_seen, socket_closed, nntp_server) = disconnect_stalled_article_server();
        let request = register_provider_from_request(
            &materialization_request(port, "primary"),
            &local_data,
            &state,
        );
        let (mut client, server) = UnixStream::pair().expect("create disconnect engine pair");
        let handler_state = Arc::clone(&state);
        let handler_data = local_data.clone();
        let handler = thread::spawn(move || {
            super::handle(server, &handler_data, &handler_data, false, &handler_state);
        });
        client
            .write_all(&request)
            .expect("write disconnect engine request");
        body_seen
            .recv_timeout(std::time::Duration::from_secs(1))
            .expect("stalled NNTP body started");

        drop(client);

        socket_closed
            .recv_timeout(std::time::Duration::from_secs(1))
            .expect("NNTP socket closed after UDS disconnect");
        handler.join().expect("join disconnected engine handler");
        nntp_server.join().expect("join disconnected NNTP server");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        while state.network_singleflight.active() != 0 && std::time::Instant::now() < deadline {
            thread::yield_now();
        }
        assert_eq!(state.network_singleflight.active(), 0);
        std::fs::remove_dir_all(root).expect("remove disconnect test directory");
    }

    #[test]
    fn random_access_session_reuses_one_connection_and_skips_preceding_postings() {
        let root = temporary_directory("random-access-session");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create random access local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 32)
                .expect("initialize random access engine state"),
        );
        let (port, server) = multipart_article_server(3);
        let request =
            register_provider_from_request(&multipart_session_request(port), &local_data, &state);
        let request_body = request
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &request[index + 4..])
            .expect("random access session request body");
        let operation: serde_json::Value =
            serde_json::from_slice(request_body).expect("parse random access session request");
        let provider_set_id = operation["provider_set_id"]
            .as_str()
            .expect("random access provider set identity")
            .to_owned();
        let opened = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let body = opened
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &opened[index + 4..])
            .expect("random access session response body");
        let opened: serde_json::Value =
            serde_json::from_slice(body).expect("parse random access session response");
        let identity = opened["identity"]
            .as_str()
            .expect("random access session identity");
        assert!(super::session::valid_session_id(identity));
        assert_eq!(
            opened["revision"]
                .as_str()
                .expect("random access session revision")
                .len(),
            64
        );
        assert_eq!(opened["byte_size"], 9);
        let reopened_response =
            engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let reopened_body = reopened_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &reopened_response[index + 4..])
            .expect("reopened random access session response body");
        let reopened: serde_json::Value = serde_json::from_slice(reopened_body)
            .expect("parse reopened random access session response");
        assert_eq!(reopened["identity"], opened["identity"]);
        assert_eq!(reopened["revision"], opened["revision"]);
        let open_reader = format!(
            "POST /v1/sessions/{identity}/readers HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let reader_response = engine_response(
            open_reader.as_bytes(),
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        let reader_body = reader_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &reader_response[index + 4..])
            .expect("session reader response body");
        let reader: serde_json::Value =
            serde_json::from_slice(reader_body).expect("parse session reader response");
        let reader_lease_id = reader["reader_lease_id"]
            .as_str()
            .expect("session reader lease identity");
        assert_eq!(reader["session_id"], identity);
        let range = serde_json::json!({
            "expected_size": 9,
            "start": 6,
            "end": 8,
            "reader_lease_id": reader_lease_id,
        })
        .to_string();
        let session_request = request.clone();
        let mut request = format!(
            "POST /v1/sessions/{identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            range.len()
        )
        .into_bytes();
        request.extend_from_slice(range.as_bytes());

        let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 206"));
        assert!(response.ends_with(b"GHI"));
        let full_range = serde_json::json!({
            "expected_size": 9,
            "start": 0,
            "end": 8,
            "reader_lease_id": reader_lease_id,
        })
        .to_string();
        let mut full_request = format!(
            "POST /v1/sessions/{identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            full_range.len()
        )
        .into_bytes();
        full_request.extend_from_slice(full_range.as_bytes());
        let full_response =
            engine_response(&full_request, local_data.clone(), false, Arc::clone(&state));
        assert!(full_response.starts_with(b"HTTP/1.1 206"));
        assert!(full_response.ends_with(b"ABCDEFGHI"));

        let promoted_response = engine_response(
            &session_request,
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        assert!(
            promoted_response.starts_with(b"HTTP/1.1 200"),
            "unexpected promoted session response: {}",
            String::from_utf8_lossy(&promoted_response),
        );
        let promoted_body = promoted_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &promoted_response[index + 4..])
            .expect("promoted random access session response body");
        let promoted: serde_json::Value = serde_json::from_slice(promoted_body)
            .expect("parse promoted random access session response");
        let mut expected_revision = Sha256::new();
        expected_revision.update(b"comet-asset-v1\0");
        expected_revision.update(9_u64.to_be_bytes());
        expected_revision.update(b"ABCDEFGHI");
        assert_eq!(
            promoted["asset_revision"],
            format!("{:x}", expected_revision.finalize())
        );
        let fetched = server.join().expect("join multipart NNTP server");
        assert_eq!(
            fetched,
            ["one@example.test", "three@example.test", "two@example.test",]
        );
        assert_eq!(
            state
                .sessions
                .lock()
                .expect("session registry lock")
                .len(std::time::Instant::now()),
            1
        );
        assert_eq!(
            state
                .provider_sets
                .lock()
                .expect("provider set registry lock")
                .remove(&provider_set_id, std::time::Instant::now()),
            Err("provider_set_busy")
        );
        assert_eq!(
            state
                .sessions
                .lock()
                .expect("session registry lock")
                .remove(identity, std::time::Instant::now()),
            Err("session_busy")
        );
        let close_reader = format!(
            "DELETE /v1/sessions/{identity}/readers/{reader_lease_id} HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let reader_closed = engine_response(
            close_reader.as_bytes(),
            root.join("data"),
            false,
            Arc::clone(&state),
        );
        assert!(reader_closed.starts_with(b"HTTP/1.1 204"));
        assert_eq!(
            state
                .sessions
                .lock()
                .expect("session registry lock")
                .remove(identity, std::time::Instant::now()),
            Ok(())
        );
        assert_eq!(
            state
                .sessions
                .lock()
                .expect("session registry lock")
                .len(std::time::Instant::now()),
            0
        );
        assert_eq!(
            state
                .provider_sets
                .lock()
                .expect("provider set registry lock")
                .remove(&provider_set_id, std::time::Instant::now()),
            Ok(())
        );
        let pool_stats = state.nntp_pools.stats();
        assert_eq!(pool_stats.open, 0);
        assert_eq!(pool_stats.idle, 0);
        std::fs::remove_dir_all(root).expect("remove random access test directory");
    }

    #[test]
    fn direct_session_salvage_is_exact_idempotent_and_observable() {
        let root = temporary_directory("session-salvage");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create salvage local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 3)
                .expect("initialize salvage engine state"),
        );
        let (port, server) = salvage_article_server();
        let open_request =
            register_provider_from_request(&salvage_session_request(port), &local_data, &state);
        let opened = engine_response(&open_request, local_data.clone(), false, Arc::clone(&state));
        let opened_body = opened
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &opened[index + 4..])
            .expect("salvage session response body");
        let opened: serde_json::Value =
            serde_json::from_slice(opened_body).expect("parse salvage session response");
        let identity = opened["identity"]
            .as_str()
            .expect("salvage session identity");
        assert_eq!(opened["byte_size"], 1200);
        let open_reader = format!(
            "POST /v1/sessions/{identity}/readers HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let reader_response = engine_response(
            open_reader.as_bytes(),
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        let reader_body = reader_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &reader_response[index + 4..])
            .expect("salvage reader response body");
        let reader: serde_json::Value =
            serde_json::from_slice(reader_body).expect("parse salvage reader response");
        let reader_lease_id = reader["reader_lease_id"]
            .as_str()
            .expect("salvage reader identity");
        let range_body = serde_json::json!({
            "expected_size": 1200,
            "start": 3,
            "end": 5,
            "reader_lease_id": reader_lease_id,
        })
        .to_string();
        let mut range_request = format!(
            "POST /v1/sessions/{identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            range_body.len()
        )
        .into_bytes();
        range_request.extend_from_slice(range_body.as_bytes());

        for _ in 0..2 {
            let response = engine_response(
                &range_request,
                local_data.clone(),
                false,
                Arc::clone(&state),
            );
            assert!(response.starts_with(b"HTTP/1.1 206"));
            assert!(
                response
                    .windows(b"X-Comet-Usenet-Salvage: zero-fill".len())
                    .any(|window| window == b"X-Comet-Usenet-Salvage: zero-fill")
            );
            assert!(
                response
                    .windows(b"X-Comet-Usenet-Salvaged-Bytes: 3".len())
                    .any(|window| window == b"X-Comet-Usenet-Salvaged-Bytes: 3")
            );
            assert!(
                response
                    .windows(b"X-Comet-Usenet-Salvaged-Holes: 1".len())
                    .any(|window| window == b"X-Comet-Usenet-Salvaged-Holes: 1")
            );
            assert!(response.ends_with(&[0, 0, 0]));
        }
        assert_eq!(
            server.join().expect("join salvage NNTP server"),
            [
                "one@example.test",
                "missing@example.test",
                "three@example.test",
            ]
        );
        let stats = super::runtime_stats_body(&state);
        let stats: serde_json::Value =
            serde_json::from_str(&stats).expect("parse salvage runtime stats");
        assert_eq!(stats["nntp_salvage_holes_total"], 1);
        assert_eq!(stats["nntp_salvage_bytes_total"], 3);
        std::fs::remove_dir_all(root).expect("remove salvage test directory");
    }

    #[test]
    fn session_reads_start_ordered_background_prefetch_at_the_next_part() {
        let root = temporary_directory("session-prefetch");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create prefetch local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 5)
                .expect("initialize prefetch engine state"),
        );
        let (port, server) = multipart_article_server(3);
        let request =
            register_provider_from_request(&multipart_session_request(port), &local_data, &state);
        let opened = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let body = opened
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &opened[index + 4..])
            .expect("prefetch session response body");
        let opened: serde_json::Value =
            serde_json::from_slice(body).expect("parse prefetch session response");
        let identity = opened["identity"]
            .as_str()
            .expect("prefetch session identity");
        let open_reader = format!(
            "POST /v1/sessions/{identity}/readers HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let reader_response = engine_response(
            open_reader.as_bytes(),
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        let reader_body = reader_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &reader_response[index + 4..])
            .expect("prefetch reader response body");
        let reader: serde_json::Value =
            serde_json::from_slice(reader_body).expect("parse prefetch reader response");
        let reader_lease_id = reader["reader_lease_id"]
            .as_str()
            .expect("prefetch reader lease identity");
        let range = serde_json::json!({
            "expected_size": 9,
            "start": 0,
            "end": 2,
            "reader_lease_id": reader_lease_id,
        })
        .to_string();
        let mut request = format!(
            "POST /v1/sessions/{identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            range.len()
        )
        .into_bytes();
        request.extend_from_slice(range.as_bytes());

        let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        assert!(response.starts_with(b"HTTP/1.1 206"));
        assert!(response.ends_with(b"ABC"));
        let fetched = server.join().expect("join prefetch NNTP server");
        assert_eq!(fetched[0], "one@example.test");
        let mut prefetched = fetched[1..].to_vec();
        prefetched.sort();
        assert_eq!(prefetched, ["three@example.test", "two@example.test"]);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        while state.background_prefetches.load(Ordering::Acquire) != 0
            && std::time::Instant::now() < deadline
        {
            thread::yield_now();
        }
        assert_eq!(state.background_prefetches.load(Ordering::Acquire), 0);
        assert_eq!(
            state
                .session_checkpoints
                .lock()
                .expect("session checkpoint store lock")
                .committed_merges(),
            2,
            "the initial extent and one complete prefetch wave each commit once",
        );
        let close_reader = format!(
            "DELETE /v1/sessions/{identity}/readers/{reader_lease_id} HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        assert!(
            engine_response(
                close_reader.as_bytes(),
                local_data,
                false,
                Arc::clone(&state),
            )
            .starts_with(b"HTTP/1.1 204")
        );
        std::fs::remove_dir_all(root).expect("remove prefetch test directory");
    }

    #[test]
    fn raw_composite_reads_prefetch_through_the_composite_reader_generation() {
        let root = temporary_directory("raw-composite-prefetch");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create composite prefetch local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 5)
                .expect("initialize composite prefetch engine state"),
        );
        let (port, server) = multipart_article_server(3);
        let request =
            register_provider_from_request(&multipart_session_request(port), &local_data, &state);
        let opened = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let body = opened
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &opened[index + 4..])
            .expect("composite prefetch session response body");
        let opened: serde_json::Value =
            serde_json::from_slice(body).expect("parse composite prefetch session response");
        let session_identity = opened["identity"]
            .as_str()
            .expect("composite prefetch session identity");
        let session_lease = state
            .sessions
            .lock()
            .expect("session registry lock")
            .get(session_identity, Instant::now())
            .expect("retain composite source session");
        let revision = session_lease.recreation_key.to_string();
        let source = super::raw_composite::RawCompositeSource::from_ranges(
            "d".repeat(64),
            vec![super::raw_composite::RawCompositePart {
                content_identity: revision.clone(),
                source_offset: 0,
                exact_size: 9,
                backing: super::raw_composite::RawCompositeBacking::Session {
                    identity: session_identity.to_owned(),
                    revision,
                    exact_size: 9,
                    _retention: session_lease.retention.clone(),
                },
            }],
        )
        .expect("build session-backed composite");
        drop(session_lease);
        let (composite_identity, _) = state
            .raw_composites
            .lock()
            .expect("raw composite registry lock")
            .insert(source, Instant::now())
            .expect("register session-backed composite");
        let reader_lease_id = state
            .raw_composites
            .lock()
            .expect("raw composite registry lock")
            .open_reader(&composite_identity, Instant::now())
            .expect("open composite prefetch reader");
        let range = serde_json::json!({
            "expected_size": 9,
            "start": 0,
            "end": 2,
            "reader_lease_id": reader_lease_id,
        })
        .to_string();
        let mut request = format!(
            "POST /v1/raw-composites/{composite_identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            range.len()
        )
        .into_bytes();
        request.extend_from_slice(range.as_bytes());

        let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        assert!(response.starts_with(b"HTTP/1.1 206"));
        assert!(response.ends_with(b"ABC"));
        let fetched = server.join().expect("join composite prefetch NNTP server");
        assert_eq!(fetched[0], "one@example.test");
        let mut prefetched = fetched[1..].to_vec();
        prefetched.sort();
        assert_eq!(prefetched, ["three@example.test", "two@example.test"]);
        let deadline = Instant::now() + Duration::from_secs(1);
        while state.background_prefetches.load(Ordering::Acquire) != 0 && Instant::now() < deadline
        {
            thread::yield_now();
        }
        assert_eq!(state.background_prefetches.load(Ordering::Acquire), 0);
        assert_eq!(
            state
                .raw_composites
                .lock()
                .expect("raw composite registry lock")
                .close_reader(&composite_identity, &reader_lease_id, Instant::now()),
            Ok(()),
        );
        std::fs::remove_dir_all(root).expect("remove composite prefetch test directory");
    }

    #[test]
    fn drain_cancels_stalled_background_prefetch() {
        let root = temporary_directory("prefetch-generation-cancel");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create generation local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 5)
                .expect("initialize generation engine state"),
        );
        let (port, body_seen, socket_closed, server) = obsolete_prefetch_server();
        let request =
            register_provider_from_request(&multipart_session_request(port), &local_data, &state);
        let opened = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let body = opened
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &opened[index + 4..])
            .expect("generation session response body");
        let opened: serde_json::Value =
            serde_json::from_slice(body).expect("parse generation session response");
        let identity = opened["identity"]
            .as_str()
            .expect("generation session identity");
        let open_reader = format!(
            "POST /v1/sessions/{identity}/readers HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let first_reader_response = engine_response(
            open_reader.as_bytes(),
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        let first_reader_body = first_reader_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &first_reader_response[index + 4..])
            .expect("first generation reader body");
        let first_reader: serde_json::Value =
            serde_json::from_slice(first_reader_body).expect("parse first generation reader");
        let first_reader_lease_id = first_reader["reader_lease_id"]
            .as_str()
            .expect("first generation reader identity");
        let range = serde_json::json!({
            "expected_size": 9,
            "start": 0,
            "end": 2,
            "reader_lease_id": first_reader_lease_id,
        })
        .to_string();
        let mut read = format!(
            "POST /v1/sessions/{identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            range.len()
        )
        .into_bytes();
        read.extend_from_slice(range.as_bytes());
        assert!(
            engine_response(&read, local_data.clone(), false, Arc::clone(&state),)
                .ends_with(b"ABC")
        );
        body_seen
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("background prefetch reached NNTP");

        let drained = engine_response(
            b"POST /v1/drain HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n",
            local_data.clone(),
            false,
            Arc::clone(&state),
        );
        assert!(drained.starts_with(b"HTTP/1.1 202"));
        socket_closed
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("drained prefetch connection closed");
        server.join().expect("join drained prefetch server");
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        while state.background_prefetches.load(Ordering::Acquire) != 0
            && std::time::Instant::now() < deadline
        {
            thread::yield_now();
        }
        assert_eq!(state.background_prefetches.load(Ordering::Acquire), 0);
        assert!(state.draining.load(Ordering::Acquire));
        std::fs::remove_dir_all(root).expect("remove generation test directory");
    }

    #[test]
    fn parser_runtime_catalogs_the_digest_bound_rust_manifest() {
        let root = temporary_directory("native-catalog");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create native catalog data directory");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 32)
                .expect("initialize native catalog engine state"),
        );
        let manifest = crate::nzb::parse(
            br#"<nzb>
                <file subject='"Season 01\Show.S01E02.mkv" yEnc'><segments><segment bytes="10" number="1">video</segment></segments></file>
                <file subject='"release.vol00+01.par2" yEnc'><segments><segment bytes="20" number="1">par2</segment></segments></file>
            </nzb>"#,
        )
        .expect("parse native catalog fixture");
        let artifact = "ab".repeat(32);
        let body = serde_json::to_vec(&serde_json::json!({
            "manifest_identity": manifest.nm1,
            "metadata": manifest.metadata,
            "manifest": manifest.files,
        }))
        .expect("serialize native catalog fixture");
        let mut request = format!(
            "POST /v1/artifacts/{artifact}/native-catalog HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, local_data.clone(), true, Arc::clone(&state));
        assert!(response.starts_with(b"HTTP/1.1 200"));
        let body_start = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .expect("find native catalog response body")
            + 4;
        let payload: serde_json::Value =
            serde_json::from_slice(&response[body_start..]).expect("parse native catalog response");
        assert_eq!(payload["artifact_sha256"], artifact);
        assert_eq!(payload["assets"][0]["file_index"], 0);
        assert_eq!(
            payload["assets"][0]["relative_path"],
            "Season 01/Show.S01E02.mkv"
        );
        assert_eq!(payload["assets"][0]["declared_bytes"], 10);
        assert_eq!(payload["assets"][0]["kind"], "video");
        assert_eq!(payload["assets"][1]["kind"], "par2");

        let mut invalid: serde_json::Value =
            serde_json::from_slice(&body).expect("parse catalog request body");
        invalid["manifest_identity"] = serde_json::Value::String(format!("nm1:{}", "0".repeat(64)));
        let invalid = serde_json::to_vec(&invalid).expect("serialize invalid catalog request");
        let mut request = format!(
            "POST /v1/artifacts/{artifact}/native-catalog HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            invalid.len()
        )
        .into_bytes();
        request.extend_from_slice(&invalid);
        let response = engine_response(&request, local_data, false, state);
        assert!(response.starts_with(b"HTTP/1.1 422"));
        assert!(
            response
                .ends_with(br#"{"version":1,"code":"asset_catalog_invalid","retryable":false}"#)
        );
        std::fs::remove_dir_all(root).expect("remove native catalog test directory");
    }

    #[test]
    fn native_inspection_fetches_verified_media_and_reuses_the_scoped_cache() {
        let root = temporary_directory("native-inspection");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create native inspection local data");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 32)
                .expect("initialize native inspection engine state"),
        );
        let media = inspection_mp4();
        let (port, server) = inspection_article_server(media.clone());
        let request = register_provider_from_request(
            &native_inspection_request(port, media.len()),
            &local_data,
            &state,
        );

        let response = engine_response(&request, local_data.clone(), false, Arc::clone(&state));
        let body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("native inspection response body");
        let evidence: serde_json::Value =
            serde_json::from_slice(body).expect("parse native inspection response");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(evidence["artifact_sha256"], "c".repeat(64));
        assert_eq!(evidence["inspection_state"], "provisionally_streamable");
        assert_eq!(evidence["container"], "mp4");
        assert_eq!(evidence["duration_millis"], 90_500);
        assert_eq!(evidence["inspected_head_bytes"], media.len());
        assert_eq!(evidence["inspected_tail_bytes"], 0);

        let cached = register_provider_from_request(
            &native_inspection_request(port, media.len()),
            &local_data,
            &state,
        );
        let cached = engine_response(&cached, local_data, false, Arc::clone(&state));
        assert!(cached.starts_with(b"HTTP/1.1 200"));
        assert_eq!(
            server.join().expect("join inspection NNTP server"),
            ["BODY <inspection@example.test>\r\n"]
        );
        std::fs::remove_dir_all(root).expect("remove native inspection test directory");
    }

    #[test]
    fn accepts_only_canonical_materialization_archive_and_composite_routes() {
        assert!(is_materialization_request(
            "POST /v1/materializations HTTP/1.1"
        ));
        assert!(!is_materialization_request(
            "GET /v1/materializations HTTP/1.1"
        ));
        assert!(is_archive_plan_request("POST /v1/archive-plan HTTP/1.1"));
        assert!(!is_archive_plan_request("GET /v1/archive-plan HTTP/1.1"));
        assert!(is_archive_direct_catalog_request(
            "POST /v1/archive-direct/catalog HTTP/1.1"
        ));
        assert!(!is_archive_direct_catalog_request(
            "GET /v1/archive-direct/catalog HTTP/1.1"
        ));
        assert!(is_archive_direct_open_request(
            "POST /v1/archive-direct/open HTTP/1.1"
        ));
        assert!(!is_archive_direct_open_request(
            "GET /v1/archive-direct/open HTTP/1.1"
        ));
        assert!(is_session_archive_catalog_request(
            "POST /v1/session-archives/catalog HTTP/1.1"
        ));
        assert!(is_session_archive_open_request(
            "POST /v1/session-archives/open HTTP/1.1"
        ));
        assert!(is_par2_discovery_request("POST /v1/par2/discover HTTP/1.1"));
        assert!(!is_par2_discovery_request("GET /v1/par2/discover HTTP/1.1"));
        assert!(is_par2_source_map_request(
            "POST /v1/par2/map-sources HTTP/1.1"
        ));
        assert!(!is_par2_source_map_request(
            "GET /v1/par2/map-sources HTTP/1.1"
        ));
        assert!(is_par2_repair_request("POST /v1/par2/repair HTTP/1.1"));
        assert!(!is_par2_repair_request("GET /v1/par2/repair HTTP/1.1"));
        assert!(is_raw_composite_open_request(
            "POST /v1/raw-composites HTTP/1.1"
        ));
        let identity = "a".repeat(64);
        let request = format!("POST /v1/raw-composites/{identity}/read HTTP/1.1");
        assert_eq!(
            raw_composite_read_identity(&request),
            Some(identity.as_str())
        );
        let request = format!("POST /v1/raw-composites/{identity}/readers HTTP/1.1");
        assert_eq!(
            raw_composite_reader_open_identity(&request),
            Some(identity.as_str())
        );
        let lease_id = "A".repeat(22);
        let request = format!("DELETE /v1/raw-composites/{identity}/readers/{lease_id} HTTP/1.1");
        assert_eq!(
            raw_composite_reader_close_identities(&request),
            Some((identity.as_str(), lease_id.as_str()))
        );
        let request = format!("POST /v1/raw-composites/{identity}/native-inspect HTTP/1.1");
        assert_eq!(
            raw_composite_native_inspect_identity(&request),
            Some(identity.as_str())
        );
        assert!(
            raw_composite_native_inspect_identity(&format!(
                "GET /v1/raw-composites/{identity}/native-inspect HTTP/1.1"
            ))
            .is_none()
        );
    }

    #[test]
    fn native_archive_plan_reopens_verified_parts_and_returns_a_proven_raw_split() {
        let root = temporary_directory("archive-plan");
        let materialized = root.join("materialized");
        std::fs::create_dir(&materialized).expect("create archive-plan materialized directory");
        std::fs::set_permissions(&materialized, std::fs::Permissions::from_mode(0o700))
            .expect("secure archive-plan materialized directory");
        let parts = [b"raw-first".as_slice(), b"raw-second".as_slice()]
            .into_iter()
            .map(|bytes| {
                let identity = format!("{:x}", Sha256::digest(bytes));
                let path = materialized.join(format!("{identity}.bin"));
                std::fs::write(&path, bytes).expect("write archive-plan part");
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400))
                    .expect("make archive-plan part immutable");
                (identity, bytes.len())
            })
            .collect::<Vec<_>>();
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize archive-plan engine state"),
        );
        let body = serde_json::to_vec(&serde_json::json!({
            "volumes": [
                {
                    "content_identity": parts[0].0,
                    "relative_path": "movie.mkv.001",
                    "expected_size": parts[0].1,
                },
                {
                    "content_identity": parts[1].0,
                    "relative_path": "movie.mkv.002",
                    "expected_size": parts[1].1,
                },
            ],
        }))
        .expect("serialize archive-plan request");
        let mut request = format!(
            "POST /v1/archive-plan HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));
        let body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("archive-plan response body");
        let plan: serde_json::Value =
            serde_json::from_slice(body).expect("parse archive-plan response");

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(plan["version"], 1);
        assert_eq!(plan["plan"]["kind"]["layout"], "raw_split");
        assert_eq!(plan["plan"]["exact_size"], 19);
        assert_eq!(plan["plan"]["volumes"][0]["number"], 0);
        assert_eq!(plan["plan"]["volumes"][1]["number"], 1);
        assert_eq!(plan["plan"]["set_identity"].as_str().unwrap().len(), 64);
        let stats = state
            .resources
            .stats()
            .expect("archive-plan resource stats");
        assert_eq!(stats.archive_jobs_active, 0);
        assert_eq!(stats.reserved_bytes, 0);

        let open_body = serde_json::to_vec(&serde_json::json!({
            "volumes": [
                {
                    "content_identity": parts[0].0,
                    "relative_path": "movie.mkv.001",
                    "expected_size": parts[0].1,
                },
                {
                    "content_identity": parts[1].0,
                    "relative_path": "movie.mkv.002",
                    "expected_size": parts[1].1,
                },
            ],
        }))
        .expect("serialize raw-composite request");
        let mut request = format!(
            "POST /v1/raw-composites HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            open_body.len()
        )
        .into_bytes();
        request.extend_from_slice(&open_body);
        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));
        let response_body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("raw-composite response body");
        let opened: serde_json::Value =
            serde_json::from_slice(response_body).expect("parse raw-composite response");
        let composite_identity = opened["identity"]
            .as_str()
            .expect("raw-composite identity")
            .to_owned();
        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(opened["version"], 1);
        assert_eq!(opened["exact_size"], 19);
        assert_eq!(opened["etag"], composite_identity);
        assert_eq!(composite_identity, plan["plan"]["set_identity"]);
        assert_eq!(
            state
                .raw_composites
                .lock()
                .expect("raw composite registry")
                .len(),
            1
        );
        assert_eq!(
            std::fs::read_dir(&materialized)
                .expect("read materialized directory")
                .count(),
            2
        );

        let inspect_body = br#"{"expected_size":19}"#;
        let mut request = format!(
            "POST /v1/raw-composites/{composite_identity}/native-inspect HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            inspect_body.len()
        )
        .into_bytes();
        request.extend_from_slice(inspect_body);
        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));
        assert!(response.starts_with(b"HTTP/1.1 422"));
        assert!(response.ends_with(
            br#"{"version":1,"code":"container_signature_mismatch","retryable":false}"#
        ));

        let open_reader = format!(
            "POST /v1/raw-composites/{composite_identity}/readers HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let reader_response = engine_response(
            open_reader.as_bytes(),
            root.clone(),
            false,
            Arc::clone(&state),
        );
        let reader_body = reader_response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &reader_response[index + 4..])
            .expect("raw composite reader response body");
        let reader: serde_json::Value =
            serde_json::from_slice(reader_body).expect("parse raw composite reader response");
        let reader_lease_id = reader["reader_lease_id"]
            .as_str()
            .expect("raw composite reader lease");
        assert_eq!(reader["source_identity"], composite_identity);
        let range_body = serde_json::json!({
            "expected_size": 19,
            "start": 7,
            "end": 11,
            "reader_lease_id": reader_lease_id,
        })
        .to_string();
        let mut request = format!(
            "POST /v1/raw-composites/{composite_identity}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            range_body.len()
        )
        .into_bytes();
        request.extend_from_slice(range_body.as_bytes());
        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));
        assert!(response.starts_with(b"HTTP/1.1 206"));
        assert!(
            response
                .windows(b"Content-Range: bytes 7-11/19".len())
                .any(|bytes| bytes == b"Content-Range: bytes 7-11/19")
        );
        assert!(response.ends_with(b"straw"));

        assert_eq!(
            state
                .raw_composites
                .lock()
                .expect("raw composite registry")
                .remove(&composite_identity, std::time::Instant::now()),
            Err("raw_composite_busy")
        );
        let close_reader = format!(
            "DELETE /v1/raw-composites/{composite_identity}/readers/{reader_lease_id} HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let reader_closed = engine_response(
            close_reader.as_bytes(),
            root.clone(),
            false,
            Arc::clone(&state),
        );
        assert!(reader_closed.starts_with(b"HTTP/1.1 204"));
        assert_eq!(
            state
                .raw_composites
                .lock()
                .expect("raw composite registry")
                .remove(&composite_identity, std::time::Instant::now()),
            Ok(())
        );
        assert_eq!(
            state
                .raw_composites
                .lock()
                .expect("raw composite registry")
                .len(),
            0
        );

        let oversized = serde_json::to_vec(&serde_json::json!({
            "volumes": [
                {
                    "content_identity": "a".repeat(64),
                    "relative_path": "release.001",
                    "expected_size": 600_u64 * 1024 * 1024 * 1024,
                },
                {
                    "content_identity": "b".repeat(64),
                    "relative_path": "release.002",
                    "expected_size": 600_u64 * 1024 * 1024 * 1024,
                },
            ],
        }))
        .expect("serialize oversized archive-plan request");
        let mut request = format!(
            "POST /v1/archive-plan HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            oversized.len()
        )
        .into_bytes();
        request.extend_from_slice(&oversized);
        let response = engine_response(&request, root.clone(), false, state);
        assert!(response.starts_with(b"HTTP/1.1 422"));
        assert!(
            response
                .ends_with(br#"{"version":1,"code":"archive_volume_budget","retryable":false}"#)
        );
        std::fs::remove_dir_all(root).expect("remove archive-plan test directory");
    }

    #[test]
    fn stored_rar5_routes_map_member_bytes_without_joining_or_extracting() {
        fn vint(mut value: u64) -> Vec<u8> {
            let mut encoded = Vec::new();
            loop {
                let mut byte = (value & 0x7f) as u8;
                value >>= 7;
                if value != 0 {
                    byte |= 0x80;
                }
                encoded.push(byte);
                if value == 0 {
                    return encoded;
                }
            }
        }

        fn block(kind: u64, body: &[u8], data: &[u8]) -> Vec<u8> {
            let mut header = vint(kind);
            header.extend(vint(if data.is_empty() { 0 } else { 0x0002 }));
            if !data.is_empty() {
                header.extend(vint(data.len() as u64));
            }
            header.extend_from_slice(body);
            let mut encoded = vec![0; 4];
            encoded.extend(vint(header.len() as u64));
            encoded.extend_from_slice(&header);
            let crc = crc32fast::hash(&encoded[4..]);
            encoded[..4].copy_from_slice(&crc.to_le_bytes());
            encoded.extend_from_slice(data);
            encoded
        }

        let media = b"verified stored member";
        let relative_path = "Movie.2026.mkv";
        let mut file_body = vint(0x0004);
        file_body.extend(vint(media.len() as u64));
        file_body.extend(vint(0));
        file_body.extend_from_slice(&crc32fast::hash(media).to_le_bytes());
        file_body.extend(vint(0));
        file_body.extend(vint(1));
        file_body.extend(vint(relative_path.len() as u64));
        file_body.extend_from_slice(relative_path.as_bytes());
        let mut archive = b"Rar!\x1a\x07\x01\0".to_vec();
        archive.extend(block(1, &vint(0), &[]));
        archive.extend(block(2, &file_body, media));
        archive.extend(block(5, &vint(0), &[]));

        let root = temporary_directory("stored-rar5-direct");
        let materialized = root.join("materialized");
        std::fs::create_dir(&materialized).expect("create materialized directory");
        std::fs::set_permissions(&materialized, std::fs::Permissions::from_mode(0o700))
            .expect("secure materialized directory");
        let content_identity = format!("{:x}", Sha256::digest(&archive));
        let path = materialized.join(format!("{content_identity}.bin"));
        std::fs::write(&path, &archive).expect("write stored RAR5 fixture");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400))
            .expect("make stored RAR5 fixture immutable");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize stored RAR5 engine state"),
        );
        let volumes = serde_json::json!([{
            "content_identity": content_identity,
            "relative_path": "release.rar",
            "expected_size": archive.len(),
        }]);
        let catalog_body = serde_json::to_vec(&serde_json::json!({
            "volumes": volumes,
        }))
        .expect("serialize stored RAR5 catalog request");
        let mut request = format!(
            "POST /v1/archive-direct/catalog HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            catalog_body.len()
        )
        .into_bytes();
        request.extend_from_slice(&catalog_body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));
        let body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("stored RAR5 catalog response body");
        let catalog: serde_json::Value =
            serde_json::from_slice(body).expect("parse stored RAR5 catalog");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(catalog["members"][0]["relative_path"], relative_path);
        assert_eq!(catalog["members"][0]["exact_size"], media.len());
        assert_eq!(catalog["members"][0]["kind"], "video");
        let plan = catalog["plan"].clone();
        let member_id = catalog["members"][0]["member_id"]
            .as_str()
            .expect("stored RAR5 member identity")
            .to_owned();

        let open_body = serde_json::to_vec(&serde_json::json!({
            "volumes": volumes,
            "expected_output_size": media.len(),
            "selected_path": relative_path,
        }))
        .expect("serialize stored RAR5 open request");
        let mut request = format!(
            "POST /v1/archive-direct/open HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            open_body.len()
        )
        .into_bytes();
        request.extend_from_slice(&open_body);
        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));
        let body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("stored RAR5 open response body");
        let opened: serde_json::Value =
            serde_json::from_slice(body).expect("parse stored RAR5 open response");
        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert_eq!(opened["identity"], member_id);
        assert_eq!(opened["exact_size"], media.len());
        assert_eq!(opened["plan"], plan);

        let lease = state
            .raw_composites
            .lock()
            .expect("raw composite registry")
            .get(&member_id, Instant::now())
            .expect("stored RAR5 member source");
        let bytes = lease
            .source
            .read_at(0, media.len() as u64, &|| false, |part, start, end| {
                let super::raw_composite::RawCompositeBacking::Materialization(file_identity) =
                    &part.backing
                else {
                    panic!("stored RAR5 test source must be materialized");
                };
                super::materialization::read_immutable_range_cancellable(
                    &root,
                    &part.content_identity,
                    file_identity.size,
                    start,
                    end,
                    *file_identity,
                    &|| false,
                )
            })
            .expect("read stored RAR5 member ranges");
        assert_eq!(bytes, media);
        assert_eq!(
            std::fs::read_dir(&materialized)
                .expect("read materialized directory")
                .count(),
            1
        );
        std::fs::remove_dir_all(root).expect("remove stored RAR5 test directory");
    }

    #[test]
    fn stored_rar4_plan_maps_member_ranges_without_reconstruction() {
        fn block(kind: u8, flags: u16, body: &[u8], data: &[u8]) -> Vec<u8> {
            let mut encoded = vec![0; 2];
            encoded.push(kind);
            encoded.extend_from_slice(&flags.to_le_bytes());
            encoded.extend_from_slice(
                &u16::try_from(7 + body.len())
                    .expect("small RAR4 header")
                    .to_le_bytes(),
            );
            encoded.extend_from_slice(body);
            let crc = crc32fast::hash(&encoded[2..]) as u16;
            encoded[..2].copy_from_slice(&crc.to_le_bytes());
            encoded.extend_from_slice(data);
            encoded
        }

        let media = b"RAR4 stored member";
        let relative_path = "Movie.2026.mkv";
        let mut file_body = Vec::new();
        file_body.extend_from_slice(&(media.len() as u32).to_le_bytes());
        file_body.extend_from_slice(&(media.len() as u32).to_le_bytes());
        file_body.push(2);
        file_body.extend_from_slice(&crc32fast::hash(media).to_le_bytes());
        file_body.extend_from_slice(&0_u32.to_le_bytes());
        file_body.push(29);
        file_body.push(0x30);
        file_body.extend_from_slice(&(relative_path.len() as u16).to_le_bytes());
        file_body.extend_from_slice(&0_u32.to_le_bytes());
        file_body.extend_from_slice(relative_path.as_bytes());
        let mut archive = b"Rar!\x1a\x07\0".to_vec();
        archive.extend(block(0x73, 0, &[0; 6], &[]));
        archive.extend(block(0x74, 0x8000, &file_body, media));
        archive.extend(block(0x7b, 0, &[], &[]));

        let root = temporary_directory("stored-rar4-direct");
        let materialized = root.join("materialized");
        std::fs::create_dir(&materialized).expect("create materialized directory");
        std::fs::set_permissions(&materialized, std::fs::Permissions::from_mode(0o700))
            .expect("secure materialized directory");
        let content_identity = format!("{:x}", Sha256::digest(&archive));
        let path = materialized.join(format!("{content_identity}.bin"));
        std::fs::write(&path, &archive).expect("write stored RAR4 fixture");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400))
            .expect("make stored RAR4 fixture immutable");
        let (plan, file_identities) = super::plan_archive_request(
            super::ArchivePlanRequest {
                volumes: vec![super::ArchivePlanVolumeRequest {
                    content_identity,
                    relative_path: "release.rar".to_owned(),
                    expected_size: archive.len() as u64,
                }],
            },
            &root,
            None,
            &|| false,
        )
        .expect("plan stored RAR4 fixture");

        let members = super::parse_stored_direct_members(&plan, &file_identities, &root, &|| false)
            .expect("map stored RAR4 member");

        assert!(matches!(
            plan.kind,
            super::archive_group::VolumePlanKind::SingleArchive(
                super::archive::ArchiveFormat::Rar4
            )
        ));
        assert_eq!(members.len(), 1);
        assert_eq!(members[0].relative_path, relative_path);
        assert_eq!(members[0].exact_size, media.len() as u64);
        assert_eq!(
            &archive[members[0].ranges[0].offset as usize..][..media.len()],
            media
        );
        assert_eq!(
            std::fs::read_dir(&materialized)
                .expect("read materialized directory")
                .count(),
            1
        );
        std::fs::remove_dir_all(root).expect("remove stored RAR4 test directory");
    }

    #[test]
    fn materialization_inspection_reopens_and_probes_the_exact_immutable_file() {
        fn element(id: &[u8], payload: &[u8]) -> Vec<u8> {
            let mut encoded = id.to_vec();
            encoded.push(0x80 | u8::try_from(payload.len()).expect("small EBML payload"));
            encoded.extend_from_slice(payload);
            encoded
        }

        let root = temporary_directory("materialization-inspection");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize materialization inspection state"),
        );
        let doctype = element(&[0x42, 0x82], b"matroska");
        let mut bytes = element(&[0x1a, 0x45, 0xdf, 0xa3], &doctype);
        let scale = element(&[0x2a, 0xd7, 0xb1], &[0x0f, 0x42, 0x40]);
        let duration = element(&[0x44, 0x89], &90.5_f64.to_be_bytes());
        let mut info_payload = scale;
        info_payload.extend_from_slice(&duration);
        let info = element(&[0x15, 0x49, 0xa9, 0x66], &info_payload);
        bytes.extend_from_slice(&[0x18, 0x53, 0x80, 0x67, 0xff]);
        bytes.extend_from_slice(&info);
        let identity = format!("{:x}", Sha256::digest(&bytes));
        let directory = root.join("materialized");
        std::fs::create_dir(&directory).expect("create materialized directory");
        std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
            .expect("secure materialized directory");
        let path = directory.join(format!("{identity}.bin"));
        std::fs::write(&path, &bytes).expect("write immutable probe fixture");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400))
            .expect("secure immutable probe fixture");
        let body = serde_json::to_vec(&serde_json::json!({
            "expected_size": bytes.len(),
        }))
        .expect("serialize materialization inspection request");
        let mut request = format!(
            "POST /v1/materializations/{identity}/native-inspect HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert!(
            response
                .windows(identity.len())
                .any(|value| value == identity.as_bytes())
        );
        assert!(
            response
                .windows(b"\"container\":\"matroska\"".len())
                .any(|value| value == b"\"container\":\"matroska\"")
        );
        assert!(
            response
                .windows(b"\"inspection_state\":\"provisionally_streamable\"".len())
                .any(|value| value == b"\"inspection_state\":\"provisionally_streamable\"")
        );
        assert_eq!(
            state
                .raw_composites
                .lock()
                .expect("immutable source registry")
                .len(),
            1
        );

        std::fs::remove_dir_all(root).expect("remove materialization inspection test directory");
    }

    #[test]
    fn par2_discovery_mapping_and_repair_return_id_bound_evidence() {
        let root = temporary_directory("par2-routes");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize PAR2 route state"),
        );
        let name = "Movie.mkv";
        let source_bytes = b"DATA";
        let first_16k_md5: [u8; 16] = Md5::digest(source_bytes).into();
        let mut file_id_digest = Md5::new();
        file_id_digest.update(first_16k_md5);
        file_id_digest.update(4_u64.to_le_bytes());
        file_id_digest.update(name.as_bytes());
        let file_id: [u8; 16] = file_id_digest.finalize().into();
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&file_id);
        let set_id: [u8; 16] = Md5::digest(&main).into();
        let mut description = file_id.to_vec();
        description.extend_from_slice(&first_16k_md5);
        description.extend_from_slice(&first_16k_md5);
        description.extend_from_slice(&4_u64.to_le_bytes());
        description.extend_from_slice(name.as_bytes());
        while !(64 + description.len()).is_multiple_of(4) {
            description.push(0);
        }
        let mut checksums = file_id.to_vec();
        checksums.extend_from_slice(&first_16k_md5);
        checksums.extend_from_slice(&crc32fast::hash(source_bytes).to_le_bytes());
        let mut bytes = par2_packet(&set_id, b"PAR 2.0\0Main\0\0\0\0", &main);
        bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0FileDesc", &description));
        bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0IFSC\0\0\0\0", &checksums));
        let mut recovery = 0_u32.to_le_bytes().to_vec();
        recovery.extend_from_slice(source_bytes);
        bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0RecvSlic", &recovery));
        let identity = format!("{:x}", Sha256::digest(&bytes));
        let directory = root.join("materialized");
        std::fs::create_dir(&directory).expect("create PAR2 materialized directory");
        std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
            .expect("secure PAR2 materialized directory");
        let path = directory.join(format!("{identity}.bin"));
        std::fs::write(&path, &bytes).expect("write PAR2 fixture");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o400))
            .expect("secure PAR2 fixture");
        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": identity.clone(),
                "relative_path": "opaque-sidecar",
                "expected_size": bytes.len(),
            }],
        }))
        .expect("serialize PAR2 discovery request");

        let mut request = format!(
            "POST /v1/par2/discover HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        let response = engine_response(&request, root.clone(), false, state);
        assert!(response.starts_with(b"HTTP/1.1 200"));
        let response = String::from_utf8(response).expect("UTF-8 PAR2 discovery response");
        let discovery: serde_json::Value = serde_json::from_str(
            response
                .split_once("\r\n\r\n")
                .map(|(_headers, body)| body)
                .expect("PAR2 discovery response body"),
        )
        .expect("decode PAR2 discovery response");
        assert_eq!(discovery["version"], 1);
        assert_eq!(discovery["sets"].as_array().map(Vec::len), Some(1));
        assert_eq!(
            discovery["sets"][0]["volume_content_identities"],
            serde_json::json!([identity])
        );

        let source_identity = format!("{:x}", Sha256::digest(source_bytes));
        let source_path = directory.join(format!("{source_identity}.bin"));
        std::fs::write(&source_path, source_bytes).expect("write PAR2 source fixture");
        std::fs::set_permissions(&source_path, std::fs::Permissions::from_mode(0o400))
            .expect("secure PAR2 source fixture");
        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": identity,
                "relative_path": "opaque-sidecar",
                "expected_size": bytes.len(),
            }],
            "sources": [{
                "content_identity": source_identity.clone(),
                "relative_path": "obfuscated.001",
                "expected_size": source_bytes.len(),
            }],
        }))
        .expect("serialize PAR2 source map request");
        let mut request = format!(
            "POST /v1/par2/map-sources HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize PAR2 source map state"),
        );

        let response = engine_response(&request, root.clone(), false, state);

        assert!(response.starts_with(b"HTTP/1.1 200"));
        let response = String::from_utf8(response).expect("UTF-8 PAR2 map response");
        assert!(response.contains(&format!(r#""content_identity":"{source_identity}""#)));
        assert!(response.contains(&format!(r#""file_id":"{}""#, super::lower_hex(&file_id))));
        assert!(response.contains(r#""relative_path":"Movie.mkv""#));

        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": format!("{:x}", Sha256::digest(&bytes)),
                "relative_path": "opaque-sidecar",
                "expected_size": bytes.len(),
            }],
            "sources": [{
                "content_identity": source_identity.clone(),
                "relative_path": "obfuscated.001",
                "expected_size": source_bytes.len(),
            }],
            "selected_file_id": super::lower_hex(&file_id),
        }))
        .expect("serialize complete PAR2 repair source request");
        let mut request = format!(
            "POST /v1/par2/repair HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize complete PAR2 source state"),
        );

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 200"));
        assert!(
            response
                .windows(source_identity.len())
                .any(|value| value == source_identity.as_bytes())
        );
        let stats = state.resources.stats().expect("complete PAR2 source stats");
        assert_eq!(stats.reserved_bytes, 0);
        assert_eq!(stats.repair_jobs_active, 0);

        let calculator = root.join("par2-fixture");
        std::fs::write(
            &calculator,
            "#!/bin/sh\n\
             if [ \"$1\" = \"-V\" ]; then\n\
               printf 'par2cmdline-turbo version 1.4.0\\n'\n\
               exit 0\n\
             fi\n\
             case \"$PWD\" in\n\
               *'.par2-readiness-'*) printf 'COMET-PAR2-READY' > readiness.bin ;;\n\
               *) printf 'DATA' > Movie.mkv ;;\n\
             esac\n",
        )
        .expect("write PAR2 calculator fixture");
        std::fs::set_permissions(&calculator, std::fs::Permissions::from_mode(0o755))
            .expect("make PAR2 calculator fixture executable");
        let state = Arc::new(
            super::EngineState::new_with_par2(
                &root,
                super::NativeBudgets {
                    memory_cache_bytes: 16 * 1024 * 1024,
                    disk_cache_bytes: 0,
                    minimum_free_disk_bytes: 0,
                    maximum_nntp_connections: 1,
                    maximum_spool_bytes: super::DEFAULT_SPOOL_BYTES,
                    maximum_archive_jobs: 1,
                    maximum_repair_jobs: 1,
                },
                Some(&calculator),
                None,
            )
            .expect("initialize production PAR2 repair state"),
        );
        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": format!("{:x}", Sha256::digest(&bytes)),
                "relative_path": "opaque-sidecar",
                "expected_size": bytes.len(),
            }],
            "sources": [],
            "selected_file_id": super::lower_hex(&file_id),
        }))
        .expect("serialize PAR2 repair request");
        let mut request = format!(
            "POST /v1/par2/repair HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));

        assert!(response.starts_with(b"HTTP/1.1 200"));
        let response_body = response
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("PAR2 repair response body");
        let response: serde_json::Value =
            serde_json::from_slice(response_body).expect("parse PAR2 repair response");
        let repaired_identity = format!("{:x}", Sha256::digest(source_bytes));
        assert_eq!(response["set_id"], super::lower_hex(&set_id));
        assert_eq!(response["file_id"], super::lower_hex(&file_id));
        assert_eq!(response["relative_path"], name);
        assert_eq!(response["identity"], repaired_identity);
        assert_eq!(response["byte_size"], source_bytes.len());
        let repaired = directory.join(format!("{repaired_identity}.bin"));
        assert_eq!(std::fs::read(&repaired).unwrap(), source_bytes);
        assert_eq!(
            std::fs::metadata(&repaired).unwrap().permissions().mode() & 0o777,
            0o400
        );
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let stats = state.resources.stats().expect("PAR2 resource stats");
        assert_eq!(stats.reserved_bytes, 0);
        assert_eq!(stats.repair_jobs_active, 0);

        let corrupt_calculator = root.join("par2-corrupt-fixture");
        std::fs::write(
            &corrupt_calculator,
            "#!/bin/sh\n\
             if [ \"$1\" = \"-V\" ]; then\n\
               printf 'par2cmdline-turbo version 1.4.0\\n'\n\
               exit 0\n\
             fi\n\
             case \"$PWD\" in\n\
               *'.par2-readiness-'*) printf 'COMET-PAR2-READY' > readiness.bin ;;\n\
               *) printf 'FAIL' > Movie.mkv ;;\n\
             esac\n",
        )
        .expect("write corrupt PAR2 calculator fixture");
        std::fs::set_permissions(&corrupt_calculator, std::fs::Permissions::from_mode(0o755))
            .expect("make corrupt PAR2 calculator fixture executable");
        let corrupt_state = Arc::new(
            super::EngineState::new_with_par2(
                &root,
                super::NativeBudgets {
                    memory_cache_bytes: 16 * 1024 * 1024,
                    disk_cache_bytes: 0,
                    minimum_free_disk_bytes: 0,
                    maximum_nntp_connections: 1,
                    maximum_spool_bytes: super::DEFAULT_SPOOL_BYTES,
                    maximum_archive_jobs: 1,
                    maximum_repair_jobs: 1,
                },
                Some(&corrupt_calculator),
                None,
            )
            .expect("initialize corrupt PAR2 repair state"),
        );
        let response = engine_response(&request, root.clone(), false, corrupt_state);
        assert!(response.starts_with(b"HTTP/1.1 422"));
        assert!(
            response.ends_with(
                br#"{"version":1,"code":"par2_file_evidence_invalid","retryable":false}"#
            )
        );
        let corrupt_identity = format!("{:x}", Sha256::digest(b"FAIL"));
        assert!(!directory.join(format!("{corrupt_identity}.bin")).exists());
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        std::fs::remove_dir_all(root).expect("remove PAR2 catalog test directory");
    }

    #[test]
    fn par2_survey_accepts_an_article_available_through_the_last_fallback() {
        let root = temporary_directory("par2-stat-provider-fallback");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 2)
                .expect("initialize fallback survey engine state"),
        );
        let (primary_port, primary) = stat_article_server(&[
            ("primary@example.test", false),
            ("fallback@example.test", false),
        ]);
        let (backup_port, backup) = stat_article_server(&[
            ("primary@example.test", false),
            ("fallback@example.test", true),
        ]);
        let servers = [("primary", primary_port), ("backup", backup_port)]
            .into_iter()
            .enumerate()
            .map(
                |(index, (provider_configuration_id, port))| super::provider::Server {
                    provider_configuration_id: provider_configuration_id.to_owned(),
                    connections: 1,
                    pipeline: 2,
                    priority: u16::try_from(index).unwrap(),
                    backup: index != 0,
                    request: super::nntp::BodyRequest {
                        host: "127.0.0.1".to_owned(),
                        port,
                        tls_mode: "plaintext".to_owned(),
                        allow_private: true,
                        username: None,
                        password: None,
                        message_id: "template@example.test".to_owned(),
                    },
                },
            )
            .collect::<Vec<_>>();
        let pool_references = servers
            .iter()
            .map(|server| {
                state
                    .nntp_pools
                    .reference(&server.request, server.connections, server.pipeline)
                    .expect("create fallback survey pool")
            })
            .collect();
        let source = super::SessionSource {
            provider_set: Arc::new(super::provider::ProviderSet {
                identity: "fallback-provider-set".to_owned(),
                generation: "b".repeat(64),
                account_partition: [1; 32],
                servers,
                pool_references,
            }),
            group: None,
            scheduler_session: "fallback-survey".to_owned(),
            work_class: super::nntp::WorkClass::Preparation,
            use_memory_cache: true,
            use_disk_cache: true,
            admit_disk_cache: true,
        };
        let postings = [super::session::SessionPosting {
            number: 1,
            declared_encoded_bytes: 4,
            message_id: "primary@example.test".to_owned(),
            fallback_postings: vec![super::session::SessionFallbackPosting {
                declared_encoded_bytes: 4,
                message_id: "fallback@example.test".to_owned(),
            }],
        }];

        let cached = state.cached_posting_states(&source, &postings);
        assert_eq!(
            state.survey_missing_slices(&source, &postings, &cached, 4, &|| false),
            Ok(Some(0))
        );
        assert_eq!(primary.join().expect("join primary STAT server").len(), 2);
        assert_eq!(backup.join().expect("join backup STAT server").len(), 2);
        std::fs::remove_dir_all(root).expect("remove fallback survey directory");
    }

    #[test]
    fn par2_staging_fills_the_configured_body_pipeline() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind staging NNTP server");
        let port = listener
            .local_addr()
            .expect("read staging NNTP port")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept staging NNTP connection");
            let mut reader = BufReader::new(stream.try_clone().expect("clone staging stream"));
            let mut writer = stream;
            writer
                .write_all(b"200 staging test ready\r\n")
                .expect("write staging greeting");
            for response in [
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
                b"200 reader enabled\r\n".as_slice(),
                b"101 capabilities follow\r\nVERSION 2\r\n.\r\n".as_slice(),
            ] {
                let mut command = String::new();
                reader.read_line(&mut command).expect("read staging setup");
                writer.write_all(response).expect("write staging setup");
            }
            let mut commands = Vec::new();
            for _ in 0..2 {
                let mut command = String::new();
                reader.read_line(&mut command).expect("read pipelined BODY");
                commands.push(command);
            }
            writer
                .write_all(&yenc_part_article(b"DATA", 1, 8))
                .expect("write first staging article");
            writer
                .write_all(&yenc_part_article(b"MORE", 5, 8))
                .expect("write second staging article");
            commands
        });
        let root = temporary_directory("par2-staging-pipeline");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize staging engine state"),
        );
        let request = super::nntp::BodyRequest {
            host: "127.0.0.1".to_owned(),
            port,
            tls_mode: "plaintext".to_owned(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "template@example.test".to_owned(),
        };
        let pool_reference = state
            .nntp_pools
            .reference(&request, 1, 2)
            .expect("create staging pool");
        let source = super::SessionSource {
            provider_set: Arc::new(super::provider::ProviderSet {
                identity: "staging-provider-set".to_owned(),
                generation: "b".repeat(64),
                account_partition: [1; 32],
                servers: vec![super::provider::Server {
                    provider_configuration_id: "primary".to_owned(),
                    connections: 1,
                    pipeline: 2,
                    priority: 0,
                    backup: false,
                    request,
                }],
                pool_references: vec![pool_reference],
            }),
            group: None,
            scheduler_session: "staging-pipeline".to_owned(),
            work_class: super::nntp::WorkClass::Preparation,
            use_memory_cache: false,
            use_disk_cache: false,
            admit_disk_cache: false,
        };
        let postings = [
            super::session::SessionPosting {
                number: 1,
                declared_encoded_bytes: 10,
                message_id: "first@example.test".to_owned(),
                fallback_postings: Vec::new(),
            },
            super::session::SessionPosting {
                number: 2,
                declared_encoded_bytes: 10,
                message_id: "second@example.test".to_owned(),
                fallback_postings: Vec::new(),
            },
        ];
        let mut staged = Vec::new();

        state
            .fetch_postings_batched(&source, &postings, false, &|| false, |segment| {
                staged.extend_from_slice(segment.bytes());
                Ok(())
            })
            .expect("stage pipelined postings");

        assert_eq!(staged, b"DATAMORE");
        assert_eq!(
            server
                .join()
                .expect("join staging NNTP server")
                .iter()
                .map(|command| command.trim())
                .collect::<Vec<_>>(),
            ["BODY <first@example.test>", "BODY <second@example.test>"]
        );
        let mut consume = |_segment: &super::cache::VerifiedSegment| Ok(());
        assert_eq!(
            state.consume_staged_posting(
                &source,
                &postings[0],
                Err("nntp_article_missing"),
                false,
                &|| false,
                &mut consume,
            ),
            Err("nntp_article_missing")
        );
        assert_eq!(
            state.consume_staged_posting(
                &source,
                &postings[0],
                Err("nntp_article_missing"),
                true,
                &|| false,
                &mut consume,
            ),
            Ok(())
        );
        std::fs::remove_dir_all(root).expect("remove staging test directory");
    }

    #[test]
    fn par2_repair_rejects_proven_missing_slices_without_downloading_sources() {
        let root = temporary_directory("par2-stat-survey");
        let source_bytes = b"DATAMOREPLUS";
        let source_name = "Movie.mkv";
        let first_16k_md5: [u8; 16] = Md5::digest(source_bytes).into();
        let mut file_id_digest = Md5::new();
        file_id_digest.update(first_16k_md5);
        file_id_digest.update(12_u64.to_le_bytes());
        file_id_digest.update(source_name.as_bytes());
        let file_id: [u8; 16] = file_id_digest.finalize().into();
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&file_id);
        let set_id: [u8; 16] = Md5::digest(&main).into();
        let mut description = file_id.to_vec();
        description.extend_from_slice(&Md5::digest(source_bytes));
        description.extend_from_slice(&first_16k_md5);
        description.extend_from_slice(&12_u64.to_le_bytes());
        description.extend_from_slice(source_name.as_bytes());
        while !(64 + description.len()).is_multiple_of(4) {
            description.push(0);
        }
        let mut checksums = file_id.to_vec();
        for slice in source_bytes.chunks(4) {
            checksums.extend_from_slice(&Md5::digest(slice));
            checksums.extend_from_slice(&crc32fast::hash(slice).to_le_bytes());
        }
        let mut par2_bytes = par2_packet(&set_id, b"PAR 2.0\0Main\0\0\0\0", &main);
        par2_bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0FileDesc", &description));
        par2_bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0IFSC\0\0\0\0", &checksums));
        let recovery_identity = format!("{:x}", Sha256::digest(&par2_bytes));
        let materialized = root.join("materialized");
        std::fs::create_dir(&materialized).expect("create STAT survey materialized directory");
        std::fs::set_permissions(&materialized, std::fs::Permissions::from_mode(0o700))
            .expect("secure STAT survey materialized directory");
        let recovery_path = materialized.join(format!("{recovery_identity}.bin"));
        std::fs::write(&recovery_path, &par2_bytes).expect("write STAT survey PAR2 index");
        std::fs::set_permissions(&recovery_path, std::fs::Permissions::from_mode(0o400))
            .expect("secure STAT survey PAR2 index");

        let (port, server) = stat_article_server(&[
            ("present@example.test", true),
            ("missing@example.test", false),
        ]);
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize STAT survey engine state"),
        );
        let registration = materialization_request(port, "stat-survey");
        let operation = register_provider_from_request(&registration, &root, &state);
        let operation_body = operation
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &operation[index + 4..])
            .expect("STAT survey operation body");
        let operation: serde_json::Value =
            serde_json::from_slice(operation_body).expect("decode STAT survey operation");
        let provider_set_id = operation["provider_set_id"]
            .as_str()
            .expect("STAT survey provider set identity");
        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": recovery_identity,
                "relative_path": "release.par2",
                "expected_size": par2_bytes.len(),
            }],
            "sources": [],
            "partial_sources": [{
                "postings": [
                    {"number": 1, "bytes": 4, "message_id": "present@example.test"},
                    {"number": 2, "bytes": 8, "message_id": "missing@example.test"},
                ],
                "group": null,
                "account_partition": "a".repeat(64),
                "provider_set_id": provider_set_id,
            }],
            "selected_file_id": super::lower_hex(&file_id),
        }))
        .expect("serialize STAT survey repair request");
        let mut request = format!(
            "POST /v1/par2/repair HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));

        assert!(
            response.starts_with(b"HTTP/1.1 422"),
            "{}",
            String::from_utf8_lossy(&response)
        );
        assert!(
            response.ends_with(
                br#"{"version":1,"code":"repair_insufficient","retryable":false,"required_recovery_blocks":2}"#
            )
        );
        assert_eq!(
            server
                .join()
                .expect("join STAT survey NNTP server")
                .iter()
                .map(|command| command.trim())
                .collect::<Vec<_>>(),
            ["STAT <present@example.test>", "STAT <missing@example.test>"]
        );
        let stats = state.resources.stats().expect("STAT survey resource stats");
        assert_eq!(stats.reserved_bytes, 0);
        assert_eq!(stats.repair_jobs_active, 0);
        assert!(!root.join("staging").exists());
        std::fs::remove_dir_all(root).expect("remove STAT survey directory");
    }

    #[test]
    fn par2_repair_persists_merged_partial_slices_across_engine_restart() {
        let root = temporary_directory("par2-partial-source-repair");
        let source_bytes = b"DATAMOREPLUS";
        let source_name = "Movie.mkv";
        let first_16k_md5: [u8; 16] = Md5::digest(source_bytes).into();
        let mut file_id_digest = Md5::new();
        file_id_digest.update(first_16k_md5);
        file_id_digest.update(12_u64.to_le_bytes());
        file_id_digest.update(source_name.as_bytes());
        let file_id: [u8; 16] = file_id_digest.finalize().into();
        let mut main = 4_u64.to_le_bytes().to_vec();
        main.extend_from_slice(&1_u32.to_le_bytes());
        main.extend_from_slice(&file_id);
        let set_id: [u8; 16] = Md5::digest(&main).into();
        let mut description = file_id.to_vec();
        description.extend_from_slice(&Md5::digest(source_bytes));
        description.extend_from_slice(&first_16k_md5);
        description.extend_from_slice(&12_u64.to_le_bytes());
        description.extend_from_slice(source_name.as_bytes());
        while !(64 + description.len()).is_multiple_of(4) {
            description.push(0);
        }
        let mut checksums = file_id.to_vec();
        for slice in source_bytes.chunks(4) {
            checksums.extend_from_slice(&Md5::digest(slice));
            checksums.extend_from_slice(&crc32fast::hash(slice).to_le_bytes());
        }
        let mut recovery = 0_u32.to_le_bytes().to_vec();
        recovery.extend_from_slice(&[0; 4]);
        let mut par2_bytes = par2_packet(&set_id, b"PAR 2.0\0Main\0\0\0\0", &main);
        par2_bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0FileDesc", &description));
        par2_bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0IFSC\0\0\0\0", &checksums));
        par2_bytes.extend_from_slice(&par2_packet(&set_id, b"PAR 2.0\0RecvSlic", &recovery));
        let recovery_identity = format!("{:x}", Sha256::digest(&par2_bytes));
        let materialized = root.join("materialized");
        std::fs::create_dir(&materialized).expect("create partial PAR2 materialized directory");
        std::fs::set_permissions(&materialized, std::fs::Permissions::from_mode(0o700))
            .expect("secure partial PAR2 materialized directory");
        let recovery_path = materialized.join(format!("{recovery_identity}.bin"));
        std::fs::write(&recovery_path, &par2_bytes).expect("write partial PAR2 fixture");
        std::fs::set_permissions(&recovery_path, std::fs::Permissions::from_mode(0o400))
            .expect("secure partial PAR2 fixture");

        let calculator = root.join("par2-partial-fixture");
        std::fs::write(
            &calculator,
            "#!/bin/sh\n\
             if [ \"$1\" = \"-V\" ]; then\n\
               printf 'par2cmdline-turbo version 1.4.0\\n'\n\
               exit 0\n\
             fi\n\
             case \"$PWD\" in\n\
               *'.par2-readiness-'*) printf 'COMET-PAR2-READY' > readiness.bin ;;\n\
               *)\n\
                 [ \"$(dd if=Movie.mkv bs=8 count=1 2>dd-error)\" = 'DATAMORE' ] || exit 3\n\
                 printf 'DATAMOREPLUS' > Movie.mkv\n\
                 ;;\n\
             esac\n",
        )
        .expect("write partial PAR2 calculator");
        std::fs::set_permissions(&calculator, std::fs::Permissions::from_mode(0o755))
            .expect("make partial PAR2 calculator executable");
        let state = Arc::new(
            super::EngineState::new_with_par2(
                &root,
                super::NativeBudgets {
                    memory_cache_bytes: 16 * 1024 * 1024,
                    disk_cache_bytes: 32 * 1024 * 1024,
                    minimum_free_disk_bytes: 0,
                    maximum_nntp_connections: 1,
                    maximum_spool_bytes: super::DEFAULT_SPOOL_BYTES,
                    maximum_archive_jobs: 1,
                    maximum_repair_jobs: 1,
                },
                Some(&calculator),
                None,
            )
            .expect("initialize partial PAR2 repair state"),
        );
        let (port, server) = partial_repair_article_server();
        let registration = materialization_request_for_generation(port, "partial", "b", 1);
        let operation = register_provider_from_request(&registration, &root, &state);
        let operation_body = operation
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &operation[index + 4..])
            .expect("partial repair operation body");
        let operation: serde_json::Value =
            serde_json::from_slice(operation_body).expect("decode partial repair operation");
        let provider_set_id = operation["provider_set_id"]
            .as_str()
            .expect("partial repair provider set identity");
        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": recovery_identity.clone(),
                "relative_path": "release.par2",
                "expected_size": par2_bytes.len(),
            }],
            "sources": [],
            "partial_sources": [
                {
                    "postings": [
                        {"number": 1, "bytes": 10, "message_id": "unrelated@example.test"},
                    ],
                    "group": null,
                    "account_partition": "a".repeat(64),
                    "provider_set_id": provider_set_id,
                },
                {
                    "postings": [
                        {"number": 1, "bytes": 10, "message_id": "known-first@example.test"},
                        {"number": 2, "bytes": 10, "message_id": "missing-first@example.test"},
                    ],
                    "group": null,
                    "account_partition": "a".repeat(64),
                    "provider_set_id": provider_set_id,
                },
                {
                    "postings": [
                        {"number": 1, "bytes": 10, "message_id": "known-second@example.test"},
                        {"number": 2, "bytes": 10, "message_id": "missing-second@example.test"},
                    ],
                    "group": null,
                    "account_partition": "a".repeat(64),
                    "provider_set_id": provider_set_id,
                },
            ],
            "selected_file_id": super::lower_hex(&file_id),
        }))
        .expect("serialize partial PAR2 repair request");
        let mut request = format!(
            "POST /v1/par2/repair HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&state));

        assert!(
            response.starts_with(b"HTTP/1.1 200"),
            "{}",
            String::from_utf8_lossy(&response)
        );
        let repaired_identity = format!("{:x}", Sha256::digest(source_bytes));
        assert!(
            response
                .windows(repaired_identity.len())
                .any(|value| value == repaired_identity.as_bytes())
        );
        assert!(
            response
                .windows(br#""partial_source_mapped":true"#.len())
                .any(|value| value == br#""partial_source_mapped":true"#)
        );
        assert_eq!(
            std::fs::read(materialized.join(format!("{repaired_identity}.bin"))).unwrap(),
            source_bytes
        );
        assert_eq!(
            server
                .join()
                .expect("join partial repair NNTP server")
                .iter()
                .map(|command| command.trim())
                .collect::<Vec<_>>(),
            [
                "BODY <unrelated@example.test>",
                "BODY <known-first@example.test>",
                "BODY <missing-first@example.test>",
                "BODY <known-second@example.test>",
                "BODY <missing-second@example.test>"
            ]
        );
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        let stats = state
            .resources
            .stats()
            .expect("partial PAR2 resource stats");
        assert_eq!(stats.reserved_bytes, 0);
        assert_eq!(stats.repair_jobs_active, 0);
        drop(state);

        let restarted = Arc::new(
            super::EngineState::new_with_par2(
                &root,
                super::NativeBudgets {
                    memory_cache_bytes: 16 * 1024 * 1024,
                    disk_cache_bytes: 32 * 1024 * 1024,
                    minimum_free_disk_bytes: 0,
                    maximum_nntp_connections: 1,
                    maximum_spool_bytes: super::DEFAULT_SPOOL_BYTES,
                    maximum_archive_jobs: 1,
                    maximum_repair_jobs: 1,
                },
                Some(&calculator),
                None,
            )
            .expect("restart partial PAR2 repair state"),
        );
        let operation = register_provider_from_request(&registration, &root, &restarted);
        let operation_body = operation
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &operation[index + 4..])
            .expect("restarted partial repair operation body");
        let operation: serde_json::Value = serde_json::from_slice(operation_body)
            .expect("decode restarted partial repair operation");
        let provider_set_id = operation["provider_set_id"]
            .as_str()
            .expect("restarted partial repair provider set identity");
        let body = serde_json::to_vec(&serde_json::json!({
            "files": [{
                "content_identity": recovery_identity,
                "relative_path": "release.par2",
                "expected_size": par2_bytes.len(),
            }],
            "sources": [],
            "partial_sources": [
                {
                    "postings": [
                        {"number": 1, "bytes": 10, "message_id": "known-first@example.test"},
                    ],
                    "group": null,
                    "account_partition": "a".repeat(64),
                    "provider_set_id": provider_set_id,
                },
                {
                    "postings": [
                        {"number": 1, "bytes": 10, "message_id": "known-second@example.test"},
                    ],
                    "group": null,
                    "account_partition": "a".repeat(64),
                    "provider_set_id": provider_set_id,
                },
            ],
            "selected_file_id": super::lower_hex(&file_id),
        }))
        .expect("serialize restarted partial PAR2 repair request");
        let mut request = format!(
            "POST /v1/par2/repair HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            body.len()
        )
        .into_bytes();
        request.extend_from_slice(&body);

        let response = engine_response(&request, root.clone(), false, Arc::clone(&restarted));

        assert!(
            response.starts_with(b"HTTP/1.1 200"),
            "{}",
            String::from_utf8_lossy(&response)
        );
        let disk_stats = restarted
            .disk_cache
            .as_ref()
            .expect("restarted disk segment cache")
            .lock()
            .expect("restarted disk cache lock")
            .stats()
            .expect("restarted disk cache stats");
        assert_eq!(disk_stats.mappings, 3);
        assert_eq!(std::fs::read_dir(root.join("staging")).unwrap().count(), 0);
        std::fs::remove_dir_all(root).expect("remove partial PAR2 repair directory");
    }

    #[test]
    fn accepts_only_canonical_random_access_session_routes() {
        assert!(super::is_session_request("POST /v1/sessions HTTP/1.1"));
        let identity = "A".repeat(22);
        assert_eq!(
            super::session_read_identity(&format!("POST /v1/sessions/{identity}/read HTTP/1.1")),
            Some(identity.as_str())
        );
        assert_eq!(
            super::session_reader_open_identity(&format!(
                "POST /v1/sessions/{identity}/readers HTTP/1.1"
            )),
            Some(identity.as_str())
        );
        assert_eq!(
            super::session_reader_close_identities(&format!(
                "DELETE /v1/sessions/{identity}/readers/{identity} HTTP/1.1"
            )),
            Some((identity.as_str(), identity.as_str()))
        );
        assert!(
            super::session_read_identity("POST /v1/sessions/not-an-identity/read HTTP/1.1")
                .is_none()
        );
    }

    #[test]
    fn session_read_failures_use_native_retryability_policy() {
        let root = temporary_directory("session-read-failure-policy");
        let local_data = root.join("data");
        std::fs::create_dir(&local_data).expect("create session failure policy directory");
        let state = Arc::new(
            super::EngineState::new(&local_data, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize session failure policy state"),
        );
        let body = serde_json::json!({
            "expected_size": 1,
            "start": 0,
            "end": 0,
            "reader_lease_id": "L".repeat(22),
        })
        .to_string();
        let mut request = format!(
            "POST /v1/sessions/{}/read HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: {}\r\n\r\n",
            "A".repeat(22),
            body.len(),
        )
        .into_bytes();
        request.extend_from_slice(body.as_bytes());

        let response = engine_response(&request, local_data, false, state);

        assert!(response.starts_with(b"HTTP/1.1 503"));
        assert!(
            response.ends_with(br#"{"version":1,"code":"session_unavailable","retryable":true}"#)
        );
        let cancelled = super::native_work_failure_response("nntp_cancelled");
        assert!(cancelled.starts_with(b"HTTP/1.1 503"));
        assert!(cancelled.ends_with(br#"{"version":1,"code":"nntp_cancelled","retryable":true}"#));
        let provider = super::native_work_failure_response("nntp_connect_failed");
        assert!(provider.starts_with(b"HTTP/1.1 503"));
        assert!(
            provider.ends_with(br#"{"version":1,"code":"nntp_connect_failed","retryable":true}"#)
        );
        std::fs::remove_dir_all(root).expect("remove session failure policy directory");
    }

    #[test]
    fn identifies_routes_that_require_the_native_engine() {
        assert!(is_native_work_request("POST /v1/materializations HTTP/1.1"));
        assert!(is_native_work_request("POST /v1/archive-plan HTTP/1.1"));
        assert!(is_native_work_request(
            "POST /v1/archive-nested/catalog HTTP/1.1"
        ));
        assert!(is_native_work_request(
            "POST /v1/archive-nested/extract HTTP/1.1"
        ));
        assert!(is_native_work_request("POST /v1/par2/discover HTTP/1.1"));
        assert!(is_native_work_request("POST /v1/par2/map-sources HTTP/1.1"));
        assert!(is_native_work_request("POST /v1/par2/repair HTTP/1.1"));
        assert!(is_native_work_request("POST /v1/raw-composites HTTP/1.1"));
        assert!(is_native_work_request(&format!(
            "POST /v1/raw-composites/{}/read HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(is_native_work_request(&format!(
            "POST /v1/raw-composites/{}/readers HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(is_native_work_request(&format!(
            "POST /v1/raw-composites/{}/native-inspect HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(is_native_work_request(&format!(
            "POST /v1/materializations/{}/native-inspect HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(is_native_work_request("POST /v1/sessions HTTP/1.1"));
        assert!(is_native_work_request(
            "POST /v1/sessions/AAAAAAAAAAAAAAAAAAAAAA/readers HTTP/1.1"
        ));
        assert!(is_native_work_request(
            "DELETE /v1/sessions/AAAAAAAAAAAAAAAAAAAAAA/readers/BBBBBBBBBBBBBBBBBBBBBB HTTP/1.1"
        ));
        assert!(is_native_work_request(&format!(
            "POST /v1/artifacts/{}/native-inspect HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(!is_native_work_request(&format!(
            "POST /v1/artifacts/{}/native-catalog HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(is_native_work_request(&format!(
            "PUT /v1/provider-sets/{} HTTP/1.1",
            "a".repeat(64)
        )));
        assert!(!is_native_work_request(&format!(
            "POST /v1/artifacts/{}/parse HTTP/1.1",
            "a".repeat(64)
        )));
    }

    #[test]
    fn parser_only_runtime_hides_native_work_routes() {
        let response = parser_only_response(
            b"POST /v1/materializations HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{}",
        );

        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("native_engine_disabled")
        );
        let response = parser_only_response(
            b"POST /v1/archive-plan HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{}",
        );
        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("native_engine_disabled")
        );
        let response = parser_only_response(
            b"POST /v1/par2/map-sources HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{}",
        );
        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("native_engine_disabled")
        );
        let response = parser_only_response(
            b"POST /v1/par2/repair HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{}",
        );
        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("native_engine_disabled")
        );
        let response = parser_only_response(
            b"POST /v1/raw-composites HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{}",
        );
        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("native_engine_disabled")
        );
        let response = parser_only_response(
            format!(
                "POST /v1/artifacts/{}/native-inspect HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{{}}",
                "a".repeat(64)
            )
            .as_bytes(),
        );
        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("native_engine_disabled")
        );
    }

    #[test]
    fn parser_only_runtime_reports_its_restricted_mode() {
        let response = parser_only_response(
            b"GET /v1/health HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n",
        );

        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains(r#""mode":"parser""#)
        );
    }

    #[test]
    fn drain_stops_new_runtime_work_after_its_acknowledgement() {
        let root = temporary_directory("drain");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize drain engine state"),
        );
        let drained = engine_response(
            b"POST /v1/drain HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n",
            root.clone(),
            true,
            Arc::clone(&state),
        );
        assert!(
            String::from_utf8(drained)
                .expect("UTF-8 drain response")
                .contains("202 Accepted\r\n")
        );
        assert!(state.draining.load(Ordering::Acquire));

        let rejected = engine_response(
            b"POST /v1/sessions HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 2\r\n\r\n{}",
            root.clone(),
            true,
            Arc::clone(&state),
        );
        let rejected = String::from_utf8(rejected).expect("UTF-8 draining response");
        assert!(rejected.contains("503 Service Unavailable\r\n"));
        assert!(rejected.contains(r#""code":"engine_draining""#));

        let resumed = engine_response(
            b"POST /v1/resume HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n",
            root.clone(),
            true,
            state,
        );
        assert!(
            String::from_utf8(resumed)
                .expect("UTF-8 resume response")
                .contains(r#""draining":false"#)
        );
        std::fs::remove_dir_all(root).expect("remove drain test directory");
    }

    #[test]
    fn runtime_stats_expose_bounded_nntp_pool_accounting() {
        let response = parser_only_response(
            b"GET /v1/stats HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n",
        );
        let body = response
            .windows(4)
            .position(|bytes| bytes == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("stats response body");
        let stats: serde_json::Value = serde_json::from_slice(body).expect("parse runtime stats");

        assert_eq!(stats["spool_stats_available"], true);
        assert_eq!(stats["disk_cache_stats_available"], true);
        assert_eq!(stats["draining"], false);
        assert_eq!(stats["request_workers"], super::ENGINE_REQUEST_WORKERS);
        assert_eq!(stats["nntp_preparation_slots"], 32);
        assert_eq!(stats["request_body_limit_bytes"], MAX_NZB_METADATA_BYTES);
        for field in [
            "requests_active",
            "request_body_reserved_bytes",
            "request_body_busy_rejections_total",
            "request_queue_busy_rejections_total",
            "session_prefetches_active",
            "raw_composites",
            "provider_sets",
            "spool_resident_bytes",
            "spool_reserved_bytes",
            "archive_jobs_active",
            "repair_jobs_active",
            "archive_busy_rejections_total",
            "repair_busy_rejections_total",
            "spool_rejections_total",
            "nntp_pools",
            "nntp_connections_open",
            "nntp_connections_active",
            "nntp_connections_idle",
            "nntp_queue_interactive",
            "nntp_queue_preparation",
            "nntp_queue_background",
            "nntp_reserved_commands",
            "nntp_reserved_encoded_bytes",
            "nntp_reserved_decoded_bytes",
            "nntp_scheduler_busy_rejections_total",
            "nntp_connections_poisoned",
            "nntp_provider_attempts_total",
            "nntp_provider_suppliers_total",
            "nntp_provider_hits_total",
            "nntp_provider_missing_total",
            "nntp_provider_corrupt_total",
            "nntp_provider_failures_total",
            "nntp_provider_cancellations_total",
            "nntp_provider_failovers_total",
        ] {
            assert_eq!(stats[field], 0);
        }
    }

    #[test]
    fn materialization_admission_preserves_interactive_request_workers() {
        let root = temporary_directory("materialization-admission");
        let state = super::EngineState::new(&root, 2 * 1024 * 1024 * 1024, 0, 0, 1024)
            .expect("initialize materialization admission state");
        assert_eq!(
            state.maximum_background_prefetches,
            super::MAX_CONCURRENT_PREFETCH_SESSIONS
        );
        let permits = (0..super::ENGINE_REQUEST_WORKERS)
            .map_while(|_| state.try_materialization_permit())
            .collect::<Vec<_>>();

        assert_eq!(
            permits.len(),
            super::ENGINE_REQUEST_WORKERS - super::ENGINE_INTERACTIVE_WORKER_RESERVE
        );
        assert!(state.try_materialization_permit().is_none());
        drop(permits);
        assert!(state.try_materialization_permit().is_some());
        std::fs::remove_dir_all(root).expect("remove materialization admission directory");
    }

    #[test]
    fn rejects_ambiguous_noncanonical_and_malformed_control_framing() {
        for request in [
            b"GET /v1/health HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n"
                .as_slice(),
            b"GET /v1/health HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: +0\r\n\r\n"
                .as_slice(),
            b"GET /v1/health HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\ncontent-length: 0\r\n\r\n"
                .as_slice(),
            b"GET /v1/health HTTP/1.1\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n malformed\r\n\r\n"
                .as_slice(),
            b"GET /v1/health HTTP/1.1\nX-Comet-Engine-Version: 1\nContent-Length: 0\r\n\r\n"
                .as_slice(),
            b"\r\nX-Comet-Engine-Version: 1\r\nContent-Length: 0\r\n\r\n".as_slice(),
        ] {
            let response = parser_only_response(request);
            assert!(
                String::from_utf8(response)
                    .expect("UTF-8 response")
                    .contains("invalid_request")
            );
        }
    }

    #[test]
    fn unknown_routes_cannot_reserve_an_nzb_sized_request_body() {
        assert_eq!(maximum_request_body("POST /v1/unknown HTTP/1.1"), 0);
        assert_eq!(
            maximum_request_body(&format!(
                "POST /v1/artifacts/{}/parse HTTP/1.1",
                "a".repeat(64)
            )),
            MAX_NZB_BYTES
        );
    }

    #[test]
    fn request_body_memory_is_aggregate_bounded_and_released_exactly() {
        let budget = RequestBodyBudget::new(10);
        let first = budget.reserve(7).expect("reserve first request body");
        let second = budget.reserve(3).expect("fill request body budget");
        assert_eq!(budget.reserve(1).err(), Some("native_busy"));
        assert_eq!(budget.busy_rejections.load(Ordering::Relaxed), 1);
        assert_eq!(budget.reserved_bytes.load(Ordering::Acquire), 10);

        drop(second);
        let replacement = budget.reserve(3).expect("reuse released body capacity");
        assert_eq!(budget.reserved_bytes.load(Ordering::Acquire), 10);
        drop(first);
        drop(replacement);
        assert_eq!(budget.reserved_bytes.load(Ordering::Acquire), 0);
    }

    #[test]
    fn full_request_queue_returns_retryable_busy_without_blocking_admission() {
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        let (_queued_client, queued_server) =
            UnixStream::pair().expect("create queued request socket");
        sender
            .try_send(queued_server)
            .expect("fill native request queue");
        let (mut rejected_client, rejected_server) =
            UnixStream::pair().expect("create rejected request socket");
        let busy_rejections = AtomicU64::new(0);

        assert!(admit_request(&sender, rejected_server, &busy_rejections));
        assert_eq!(busy_rejections.load(Ordering::Relaxed), 1);
        rejected_client
            .set_read_timeout(Some(Duration::from_secs(1)))
            .expect("bound busy response read");
        let mut response = Vec::new();
        rejected_client
            .read_to_end(&mut response)
            .expect("read busy response");
        let response = String::from_utf8(response).expect("UTF-8 busy response");
        assert!(response.starts_with("HTTP/1.1 503 Service Unavailable\r\n"));
        assert!(response.contains(r#""code":"native_busy""#));
        drop(receiver);

        let (_disconnected_client, disconnected_server) =
            UnixStream::pair().expect("create disconnected queue socket");
        assert!(!admit_request(
            &sender,
            disconnected_server,
            &busy_rejections
        ));
        assert_eq!(busy_rejections.load(Ordering::Relaxed), 1);
    }

    #[test]
    fn accept_loop_retries_only_transient_socket_errors() {
        for kind in [
            std::io::ErrorKind::WouldBlock,
            std::io::ErrorKind::Interrupted,
            std::io::ErrorKind::ConnectionAborted,
        ] {
            assert!(accept_error_is_transient(&std::io::Error::from(kind)));
        }
        for kind in [
            std::io::ErrorKind::PermissionDenied,
            std::io::ErrorKind::InvalidInput,
            std::io::ErrorKind::Other,
        ] {
            assert!(!accept_error_is_transient(&std::io::Error::from(kind)));
        }
    }

    #[test]
    fn closing_admission_unlinks_the_socket_without_interrupting_accepted_peers() {
        let root = temporary_directory("close-admission");
        let socket = root.join("engine.sock");
        let listener = UnixListener::bind(&socket).expect("bind lifecycle socket");
        let mut client = UnixStream::connect(&socket).expect("connect admitted peer");
        let (mut accepted, _) = listener.accept().expect("accept lifecycle peer");

        close_admission(listener, &socket);
        assert!(!socket.exists());
        assert!(UnixStream::connect(&socket).is_err());
        client
            .write_all(b"still-active")
            .expect("write admitted peer");
        let mut payload = [0u8; 12];
        accepted
            .read_exact(&mut payload)
            .expect("read admitted peer");
        assert_eq!(&payload, b"still-active");
        std::fs::remove_dir_all(root).expect("remove lifecycle socket directory");
    }

    #[test]
    fn drain_discards_queued_requests_without_waiting_for_request_timeouts() {
        let root = temporary_directory("drained-request-queue");
        let state = Arc::new(
            super::EngineState::new(&root, 16 * 1024 * 1024, 0, 0, 1)
                .expect("initialize draining engine state"),
        );
        state.draining.store(true, Ordering::Release);
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        let (mut queued_client, queued_server) =
            UnixStream::pair().expect("create queued draining socket");
        sender
            .try_send(queued_server)
            .expect("queue request before drain");
        let receiver = Arc::new(std::sync::Mutex::new(receiver));
        let worker_receiver = Arc::clone(&receiver);
        let worker_root = root.clone();
        let worker_state = Arc::clone(&state);
        let started = Instant::now();
        let worker = thread::spawn(move || {
            run_request_worker(
                &worker_receiver,
                &worker_root,
                &worker_root,
                true,
                &worker_state,
            );
        });
        drop(sender);
        drop(receiver);
        worker.join().expect("join drained request worker");
        assert!(started.elapsed() < Duration::from_secs(1));

        queued_client
            .set_read_timeout(Some(Duration::from_secs(1)))
            .expect("bound drained queue read");
        let mut response = Vec::new();
        queued_client
            .read_to_end(&mut response)
            .expect("queued socket closes");
        assert!(response.is_empty());
        std::fs::remove_dir_all(root).expect("remove drained request queue directory");
    }

    #[test]
    fn rejects_control_headers_above_the_fixed_limit() {
        let mut request = b"GET /v1/health HTTP/1.1\r\n".to_vec();
        request.resize(MAX_REQUEST_HEADER_BYTES, b'x');

        let response = parser_only_response(&request);

        assert!(
            String::from_utf8(response)
                .expect("UTF-8 response")
                .contains("request_header_too_large")
        );
    }

    #[test]
    fn rejects_nzb_metadata_that_cannot_fit_the_bounded_parse_response() {
        let mut document = String::from("<nzb>");
        for _ in 0..20_000 {
            document.push_str(
                r#"<file subject="x" poster="x" date="1"><segments><segment bytes="1" number="1">x</segment></segments></file>"#,
            );
        }
        document.push_str("</nzb>");

        assert_eq!(
            parse_nzb_with_metadata_limit(document.as_bytes(), MAX_NATIVE_CATALOG_BYTES,),
            Err("nzb_metadata_too_large")
        );
    }

    #[test]
    fn accepts_nzb_metadata_within_the_bounded_parse_response() {
        let mut document = String::from(
            r#"<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd"><nzb><file subject="&quot;movie.mkv&quot; yEnc" poster="x" date="1"><segments>"#,
        );
        for number in 1..=29_252 {
            document.push_str(&format!(
                r#"<segment bytes="750000" number="{number}">{number}.abcdefghijklmnopqrstuvwxyz0123456789@example.test</segment>"#
            ));
        }
        document.push_str("</segments></file></nzb>");

        let payload = parse_nzb(document.as_bytes()).expect("bounded NZB metadata");
        let manifest: serde_json::Value =
            serde_json::from_str(&payload).expect("deserialize large NZB metadata");

        assert!(payload.len() > MAX_NATIVE_CATALOG_BYTES);
        assert!(payload.len() <= MAX_NZB_METADATA_BYTES);
        assert_eq!(
            manifest["manifest"][0]["postings"]
                .as_array()
                .unwrap()
                .len(),
            29_252
        );
    }

    #[test]
    fn applies_the_control_limit_to_materialization_requests() {
        assert_eq!(
            maximum_request_body("POST /v1/materializations HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body(&format!(
                "POST /v1/materializations/{}/native-inspect HTTP/1.1",
                "a".repeat(64)
            )),
            MAX_CONTROL_REQUEST_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/archive-plan HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/archive-nested/catalog HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/archive-nested/extract HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/archive-direct/catalog HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/archive-direct/open HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/session-archives/catalog HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/session-archives/open HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/par2/discover HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/par2/map-sources HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/par2/repair HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/raw-composites HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body(&format!(
                "POST /v1/raw-composites/{}/native-inspect HTTP/1.1",
                "a".repeat(64)
            )),
            MAX_CONTROL_REQUEST_BYTES
        );
        assert_eq!(
            maximum_request_body(&format!(
                "POST /v1/artifacts/{}/parse HTTP/1.1",
                "a".repeat(64)
            )),
            MAX_NZB_BYTES
        );
        assert_eq!(
            maximum_request_body(&format!(
                "POST /v1/artifacts/{}/native-inspect HTTP/1.1",
                "a".repeat(64)
            )),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body(&format!(
                "POST /v1/artifacts/{}/native-catalog HTTP/1.1",
                "a".repeat(64)
            )),
            MAX_NZB_METADATA_BYTES
        );
        assert_eq!(
            maximum_request_body("POST /v1/sessions HTTP/1.1"),
            MAX_NZB_METADATA_BYTES
        );
    }

    #[test]
    fn allocator_collection_targets_only_memory_intensive_requests() {
        assert!(request_releases_allocator_slack(
            "POST /v1/materializations HTTP/1.1",
            1,
        ));
        assert!(request_releases_allocator_slack(
            &format!("POST /v1/artifacts/{}/parse HTTP/1.1", "a".repeat(64)),
            MAX_CONTROL_REQUEST_BYTES,
        ));
        assert!(!request_releases_allocator_slack(
            &format!("POST /v1/raw-composites/{}/read HTTP/1.1", "a".repeat(64)),
            128,
        ));
    }
}
