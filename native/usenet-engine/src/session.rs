use crate::cache::{SegmentLease, VerifiedSegment};
use crate::limits;
use crate::materialization::asset_revision_hasher;
use crate::reader_lease::{
    ReaderGeneration, ReaderLeaseError, ReaderLeases, ReaderPermit, random_lease_id,
};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const MAX_SESSION_BYTES: u64 = limits::MAX_LOGICAL_BYTES;
const MAX_READ_BYTES: u64 = 8 * 1024 * 1024;
pub const MAX_DECLARED_POSTING_BYTES: u64 = limits::MAX_DECLARED_POSTING_BYTES;
const MAX_ACTIVE_SESSIONS: usize = 1024;
// A maximum-size metadata request must have room to expand into Rust
// collections even when the configured segment cache is deliberately tiny.
pub const MIN_SESSION_METADATA_BUDGET_BYTES: usize = limits::MAX_NZB_METADATA_BYTES * 4;
const SESSION_BASE_METADATA_BYTES: usize = 1024;
// These conservative charges include the retained posting vectors, allocator
// metadata and the future ordered/hash extent indexes, not just live payload.
const SESSION_POSTING_METADATA_BYTES: usize = 256;
const SESSION_FALLBACK_METADATA_BYTES: usize = 96;
const MAX_SESSION_READERS: usize = 8;
const MAX_SALVAGE_POSTINGS_PER_HOLE: usize = 2;
const MAX_SALVAGE_HOLES: usize = 2;
const MAX_SALVAGE_HOLE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_SALVAGE_TOTAL_BYTES: u64 = 4 * 1024 * 1024;
const SALVAGE_FILE_FRACTION_DIVISOR: u64 = 400;
const SESSION_IDLE_TTL: Duration = Duration::from_secs(15 * 60);
const SESSION_ABSOLUTE_TTL: Duration = Duration::from_secs(6 * 60 * 60);
const SESSION_SWEEP_INTERVAL: Duration = Duration::from_secs(60);

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionFallbackPosting {
    pub declared_encoded_bytes: u64,
    pub message_id: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionPosting {
    pub number: u64,
    pub declared_encoded_bytes: u64,
    pub message_id: String,
    pub fallback_postings: Vec<SessionFallbackPosting>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Extent {
    begin: u64,
    end: u64,
    posting_index: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SalvageExtent {
    begin: u64,
    end: u64,
    first_posting_index: usize,
    last_posting_index: usize,
}

enum ProvenRange {
    Segment(usize),
    Fetch(usize),
    Salvage(SalvageExtent),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SessionCheckpoint {
    pub posting_index: usize,
    pub begin: u64,
    pub end: u64,
    pub total_size: u64,
}

pub struct RandomAccessSession {
    identity: String,
    size: u64,
    postings: Arc<[SessionPosting]>,
    declared_prefix_ends: Vec<u64>,
    extents: BTreeMap<u64, Extent>,
    posting_extents: HashMap<usize, Extent>,
    salvage_extents: BTreeMap<u64, SalvageExtent>,
    salvage_posting_extents: HashMap<usize, SalvageExtent>,
    allow_degraded_playback: bool,
    salvaged_bytes: u64,
    salvaged_holes: usize,
    reported_salvaged_bytes: u64,
    reported_salvaged_holes: usize,
    census_complete: bool,
    pending_checkpoints: Vec<SessionCheckpoint>,
    asset_revision_hasher: Option<Sha256>,
    asset_revision_next: u64,
    strong_asset_revision: Option<String>,
}

struct RegisteredSession<T> {
    session: Arc<Mutex<RandomAccessSession>>,
    context: Arc<T>,
    recreation_key: Arc<str>,
    retention: SessionRetention,
    retained_metadata_bytes: usize,
    readers: ReaderLeases,
    created_at: Instant,
    last_access: Instant,
}

#[derive(Clone, Debug)]
pub struct SessionRetention(Arc<()>);

impl SessionRetention {
    fn new() -> Self {
        Self(Arc::new(()))
    }

    fn retained(&self) -> bool {
        Arc::strong_count(&self.0) > 1
    }
}

pub struct SessionLease<T> {
    pub session: Arc<Mutex<RandomAccessSession>>,
    pub context: Arc<T>,
    pub recreation_key: Arc<str>,
    pub retention: SessionRetention,
    _permit: ReaderPermit,
}

pub struct PersistentSessionLease<T> {
    pub session: Arc<Mutex<RandomAccessSession>>,
    pub context: Arc<T>,
    pub recreation_key: Arc<str>,
    pub prefetch_generation: ReaderGeneration,
    _permit: ReaderPermit,
}

pub struct SessionRegistry<T> {
    entries: HashMap<String, RegisteredSession<T>>,
    retained_metadata_bytes: usize,
    maximum_retained_metadata_bytes: usize,
    last_sweep: Instant,
}

impl<T> SessionRegistry<T> {
    pub fn new(maximum_retained_metadata_bytes: usize) -> Self {
        Self {
            entries: HashMap::new(),
            retained_metadata_bytes: 0,
            maximum_retained_metadata_bytes,
            last_sweep: Instant::now(),
        }
    }

    fn maybe_sweep(&mut self, now: Instant) {
        if now.duration_since(self.last_sweep) >= SESSION_SWEEP_INTERVAL {
            self.remove_expired(now);
            self.last_sweep = now;
        }
    }

    pub fn insert(
        &mut self,
        session: RandomAccessSession,
        context: T,
        recreation_key: String,
        now: Instant,
    ) -> Result<(String, u64, Option<String>), &'static str> {
        self.maybe_sweep(now);
        if let Some(entry) = self
            .entries
            .values_mut()
            .find(|entry| entry.recreation_key.as_ref() == recreation_key)
        {
            entry.last_access = now;
            let session = entry.session.lock().expect("random access session lock");
            return Ok((
                session.identity().to_owned(),
                session.size(),
                session.strong_asset_revision().map(str::to_owned),
            ));
        }
        if self.entries.contains_key(session.identity()) {
            return Err("session_id_collision");
        }
        let retained_metadata_bytes = session.retained_metadata_bytes();
        if retained_metadata_bytes > self.maximum_retained_metadata_bytes {
            return Err("session_capacity");
        }
        while self.entries.len() >= MAX_ACTIVE_SESSIONS
            || retained_metadata_bytes
                > self.maximum_retained_metadata_bytes - self.retained_metadata_bytes
        {
            let oldest = self
                .entries
                .iter()
                .filter(|(_, entry)| !entry.readers.is_busy() && !entry.retention.retained())
                .min_by_key(|(_, entry)| entry.last_access)
                .map(|(identity, _)| identity.clone());
            if let Some(identity) = oldest {
                self.remove_entry(&identity);
            } else {
                return Err("session_capacity");
            }
        }
        let identity = session.identity().to_owned();
        let size = session.size();
        let strong_asset_revision = session.strong_asset_revision().map(str::to_owned);
        self.entries.insert(
            identity.clone(),
            RegisteredSession {
                session: Arc::new(Mutex::new(session)),
                context: Arc::new(context),
                recreation_key: Arc::from(recreation_key),
                retention: SessionRetention::new(),
                retained_metadata_bytes,
                readers: ReaderLeases::new(
                    MAX_SESSION_READERS,
                    SESSION_IDLE_TTL,
                    SESSION_ABSOLUTE_TTL,
                ),
                created_at: now,
                last_access: now,
            },
        );
        self.retained_metadata_bytes += retained_metadata_bytes;
        Ok((identity, size, strong_asset_revision))
    }

    pub fn describe_by_recreation_key(
        &mut self,
        recreation_key: &str,
        now: Instant,
    ) -> Option<(String, u64, Option<String>)> {
        self.maybe_sweep(now);
        let entry = self
            .entries
            .values_mut()
            .find(|entry| entry.recreation_key.as_ref() == recreation_key)?;
        entry.last_access = now;
        let session = entry.session.lock().expect("random access session lock");
        Some((
            session.identity().to_owned(),
            session.size(),
            session.strong_asset_revision().map(str::to_owned),
        ))
    }

    pub fn get(&mut self, identity: &str, now: Instant) -> Result<SessionLease<T>, &'static str> {
        self.maybe_sweep(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("session_unavailable")?;
        if expired(entry, now) {
            return Err("session_busy");
        }
        let permit = entry
            .readers
            .acquire_transient()
            .map_err(session_reader_error)?;
        entry.last_access = now;
        Ok(SessionLease {
            session: Arc::clone(&entry.session),
            context: Arc::clone(&entry.context),
            recreation_key: Arc::clone(&entry.recreation_key),
            retention: entry.retention.clone(),
            _permit: permit,
        })
    }

    pub fn open_reader(&mut self, identity: &str, now: Instant) -> Result<String, &'static str> {
        self.maybe_sweep(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("session_unavailable")?;
        if expired(entry, now) {
            return Err("session_busy");
        }
        let lease_id = entry.readers.open(now).map_err(session_reader_error)?;
        entry.last_access = now;
        Ok(lease_id)
    }

    pub fn get_with_reader(
        &mut self,
        identity: &str,
        lease_id: &str,
        now: Instant,
    ) -> Result<PersistentSessionLease<T>, &'static str> {
        self.maybe_sweep(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("session_unavailable")?;
        let permit = entry
            .readers
            .acquire(lease_id, now)
            .map_err(session_reader_error)?;
        let prefetch_generation = permit
            .generation()
            .expect("persistent session reader generation");
        entry.last_access = now;
        Ok(PersistentSessionLease {
            session: Arc::clone(&entry.session),
            context: Arc::clone(&entry.context),
            recreation_key: Arc::clone(&entry.recreation_key),
            prefetch_generation,
            _permit: permit,
        })
    }

    pub fn close_reader(
        &mut self,
        identity: &str,
        lease_id: &str,
        now: Instant,
    ) -> Result<(), &'static str> {
        self.maybe_sweep(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("session_unavailable")?;
        entry
            .readers
            .close(lease_id, now)
            .map_err(session_reader_error)
    }

    #[cfg(test)]
    pub fn remove(&mut self, identity: &str, now: Instant) -> Result<(), &'static str> {
        self.maybe_sweep(now);
        let entry = self.entries.get(identity).ok_or("session_unavailable")?;
        if entry.readers.is_busy() || entry.retention.retained() {
            return Err("session_busy");
        }
        self.remove_entry(identity);
        Ok(())
    }

    pub fn len(&mut self, now: Instant) -> usize {
        self.remove_expired(now);
        self.entries.len()
    }

    fn remove_expired(&mut self, now: Instant) {
        self.entries.retain(|_, entry| {
            entry.readers.remove_expired(now);
            let retain =
                entry.readers.is_busy() || entry.retention.retained() || !expired(entry, now);
            if !retain {
                self.retained_metadata_bytes -= entry.retained_metadata_bytes;
            }
            retain
        });
    }

    fn remove_entry(&mut self, identity: &str) {
        let entry = self
            .entries
            .remove(identity)
            .expect("registered session exists");
        self.retained_metadata_bytes -= entry.retained_metadata_bytes;
    }
}

fn session_reader_error(error: ReaderLeaseError) -> &'static str {
    match error {
        ReaderLeaseError::Busy => "session_reader_busy",
        ReaderLeaseError::Capacity => "session_reader_capacity",
        ReaderLeaseError::RandomUnavailable => "session_random_unavailable",
        ReaderLeaseError::Unavailable => "session_reader_unavailable",
    }
}

fn expired<T>(entry: &RegisteredSession<T>, now: Instant) -> bool {
    now.duration_since(entry.last_access) >= SESSION_IDLE_TTL
        || now.duration_since(entry.created_at) >= SESSION_ABSOLUTE_TTL
}

impl RandomAccessSession {
    pub fn new(
        identity: String,
        postings: Vec<SessionPosting>,
        first: &VerifiedSegment,
    ) -> Result<Self, &'static str> {
        Self::new_with_degraded_playback(identity, postings, first, false)
    }

    pub fn new_with_degraded_playback(
        identity: String,
        postings: Vec<SessionPosting>,
        first: &VerifiedSegment,
        allow_degraded_playback: bool,
    ) -> Result<Self, &'static str> {
        if first.begin != 1
            || first.total_size == 0
            || first.total_size > MAX_SESSION_BYTES
            || (postings.len() > 1 && first.end == first.total_size)
        {
            return Err("invalid_random_access_session");
        }
        let size = first.total_size;
        let mut declared_total = 0_u64;
        let mut declared_prefix_ends = Vec::with_capacity(postings.len());
        for posting in &postings {
            declared_total += posting.declared_encoded_bytes;
            declared_prefix_ends.push(declared_total);
        }
        let mut session = Self {
            identity,
            size,
            postings: postings.into(),
            declared_prefix_ends,
            extents: BTreeMap::new(),
            posting_extents: HashMap::new(),
            salvage_extents: BTreeMap::new(),
            salvage_posting_extents: HashMap::new(),
            allow_degraded_playback,
            salvaged_bytes: 0,
            salvaged_holes: 0,
            reported_salvaged_bytes: 0,
            reported_salvaged_holes: 0,
            census_complete: false,
            pending_checkpoints: Vec::new(),
            asset_revision_hasher: Some(asset_revision_hasher(size)),
            asset_revision_next: 1,
            strong_asset_revision: None,
        };
        session.record_extent(0, first)?;
        if session.postings.len() == 1 {
            if first.end != size {
                return Err("session_extent_gap");
            }
            session.census_complete = true;
        }
        Ok(session)
    }

    pub fn identity(&self) -> &str {
        &self.identity
    }

    pub fn size(&self) -> u64 {
        self.size
    }

    pub fn strong_asset_revision(&self) -> Option<&str> {
        self.strong_asset_revision.as_deref()
    }

    fn retained_metadata_bytes(&self) -> usize {
        let mut total = SESSION_BASE_METADATA_BYTES
            + self.identity.capacity()
            + self.postings.len() * SESSION_POSTING_METADATA_BYTES;
        for posting in self.postings.iter() {
            total += posting.message_id.capacity()
                + posting.fallback_postings.capacity() * SESSION_FALLBACK_METADATA_BYTES;
            for fallback in &posting.fallback_postings {
                total += fallback.message_id.capacity();
            }
        }
        total
    }

    pub fn restore_checkpoints(
        &mut self,
        checkpoints: &[SessionCheckpoint],
    ) -> Result<(), &'static str> {
        let original_extents = self.extents.clone();
        let original_posting_extents = self.posting_extents.clone();
        for checkpoint in checkpoints {
            if checkpoint.total_size != self.size {
                self.extents = original_extents;
                self.posting_extents = original_posting_extents;
                return Err("session_checkpoint_conflict");
            }
            if let Err(code) = self.install_extent(
                Extent {
                    begin: checkpoint.begin,
                    end: checkpoint.end,
                    posting_index: checkpoint.posting_index,
                },
                false,
            ) {
                self.extents = original_extents;
                self.posting_extents = original_posting_extents;
                return Err(code);
            }
        }
        Ok(())
    }

    pub fn pending_checkpoints(&self) -> &[SessionCheckpoint] {
        &self.pending_checkpoints
    }

    pub fn commit_pending_checkpoints(&mut self, count: usize) {
        self.pending_checkpoints.drain(..count);
    }

    pub fn prefetch_start_after(&self, offset: u64) -> Option<usize> {
        let current = self.extent_containing(offset + 1)?;
        (current.posting_index + 1 < self.postings.len()).then_some(current.posting_index + 1)
    }

    pub fn postings_for_prefetch(
        &self,
        posting_index: usize,
        window_bytes: u64,
    ) -> (Arc<[SessionPosting]>, Vec<usize>) {
        let mut scheduled_bytes = 0_u64;
        let indexes = self
            .postings
            .iter()
            .enumerate()
            .skip(posting_index)
            .take_while(|(_, posting)| {
                let include = scheduled_bytes < window_bytes;
                if include {
                    scheduled_bytes += posting.declared_encoded_bytes;
                }
                include
            })
            .filter(|(index, _)| {
                !self.posting_extents.contains_key(index)
                    && !self.salvage_posting_extents.contains_key(index)
            })
            .map(|(index, _)| index)
            .collect();
        (Arc::clone(&self.postings), indexes)
    }

    pub fn salvage_state(&self) -> (bool, u64, usize) {
        (
            self.salvaged_holes != 0,
            self.salvaged_bytes,
            self.salvaged_holes,
        )
    }

    pub fn salvage_delta(&mut self) -> (u64, usize) {
        let delta = (
            self.salvaged_bytes - self.reported_salvaged_bytes,
            self.salvaged_holes - self.reported_salvaged_holes,
        );
        self.reported_salvaged_bytes = self.salvaged_bytes;
        self.reported_salvaged_holes = self.salvaged_holes;
        delta
    }

    pub fn record_prefetched(
        &mut self,
        posting_index: usize,
        segment: &VerifiedSegment,
    ) -> Result<(), &'static str> {
        self.record_extent(posting_index, segment)
    }

    pub fn read_at<F>(
        shared: &Arc<Mutex<Self>>,
        offset: u64,
        length: u64,
        mut fetch: F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: FnMut(&SessionPosting) -> Result<SegmentLease, &'static str>,
    {
        let (final_logical, postings) = {
            let session = shared.lock().expect("random access session lock");
            let final_logical = offset.checked_add(length).ok_or("invalid_session_range")?;
            if length == 0
                || length > MAX_READ_BYTES
                || offset >= session.size
                || final_logical > session.size
            {
                return Err("invalid_session_range");
            }
            (final_logical, Arc::clone(&session.postings))
        };
        let mut output = Vec::with_capacity(length as usize);
        let mut logical = offset + 1;
        while logical <= final_logical {
            let mut retained = None;
            loop {
                let proven = shared
                    .lock()
                    .expect("random access session lock")
                    .mapping_probe(logical)?;
                match proven {
                    ProvenRange::Fetch(posting_index) => match fetch(&postings[posting_index]) {
                        Ok(segment) => {
                            shared
                                .lock()
                                .expect("random access session lock")
                                .record_extent(posting_index, segment.segment())?;
                            retained = Some((posting_index, segment));
                        }
                        Err(code) => Self::resolve_salvage(
                            shared,
                            &postings,
                            posting_index,
                            code,
                            &mut fetch,
                        )?,
                    },
                    ProvenRange::Segment(posting_index) => {
                        let segment = match retained.take() {
                            Some((retained_index, segment)) if retained_index == posting_index => {
                                segment
                            }
                            _ => fetch(&postings[posting_index])?,
                        };
                        let extent = {
                            let mut session = shared.lock().expect("random access session lock");
                            session.record_extent(posting_index, segment.segment())?;
                            session.posting_extents[&posting_index]
                        };
                        let slice_end = extent.end.min(final_logical);
                        segment.segment().read_logical_range_into(
                            logical,
                            slice_end,
                            &mut output,
                        )?;
                        logical = slice_end
                            .checked_add(1)
                            .expect("session extent ends within the logical size");
                        break;
                    }
                    ProvenRange::Salvage(extent) => {
                        let slice_end = extent.end.min(final_logical);
                        let fill_bytes = (slice_end - logical + 1) as usize;
                        output.resize(output.len() + fill_bytes, 0);
                        logical = slice_end + 1;
                        break;
                    }
                }
            }
        }
        assert_eq!(
            output.len(),
            length as usize,
            "session read produced the requested length"
        );
        Ok(output)
    }

    fn mapping_probe(&self, logical: u64) -> Result<ProvenRange, &'static str> {
        if let Some(extent) = self.extent_containing(logical) {
            return Ok(ProvenRange::Segment(extent.posting_index));
        }
        if let Some(extent) = self.salvage_containing(logical) {
            return Ok(ProvenRange::Salvage(extent));
        }
        let mut lower = 0usize;
        let mut upper = self.postings.len() - 1;
        let declared_total = self.declared_prefix_ends[self.declared_prefix_ends.len() - 1];
        let declared_offset =
            u128::from(logical - 1) * u128::from(declared_total) / u128::from(self.size);
        let proportional = self
            .declared_prefix_ends
            .partition_point(|end| u128::from(*end) <= declared_offset)
            .min(upper);
        let mut candidate = proportional;
        loop {
            let (begin, end, proven) =
                if let Some(extent) = self.posting_extents.get(&candidate).copied() {
                    (extent.begin, extent.end, ProvenRange::Segment(candidate))
                } else if let Some(extent) = self.salvage_posting_extents.get(&candidate).copied() {
                    (extent.begin, extent.end, ProvenRange::Salvage(extent))
                } else {
                    return Ok(ProvenRange::Fetch(candidate));
                };
            if (begin..=end).contains(&logical) {
                return Ok(proven);
            }
            if end < logical {
                lower = candidate + 1;
            } else {
                if candidate == 0 {
                    return Err("session_extent_gap");
                }
                upper = candidate - 1;
            }
            if lower > upper {
                return Err("session_extent_gap");
            }
            candidate = lower + (upper - lower) / 2;
        }
    }

    fn resolve_salvage<F>(
        shared: &Arc<Mutex<Self>>,
        postings: &[SessionPosting],
        missing_posting_index: usize,
        failure: &'static str,
        fetch: &mut F,
    ) -> Result<(), &'static str>
    where
        F: FnMut(&SessionPosting) -> Result<SegmentLease, &'static str>,
    {
        {
            let session = shared.lock().expect("random access session lock");
            if session.posting_extents.contains_key(&missing_posting_index)
                || session
                    .salvage_posting_extents
                    .contains_key(&missing_posting_index)
            {
                return Ok(());
            }
            if !session.allow_degraded_playback || !salvage_failure(failure) {
                return Err(failure);
            }
        }
        let mut first_missing = missing_posting_index;
        let mut last_missing = missing_posting_index;
        let left_end = loop {
            if first_missing == 0 {
                return Err(failure);
            }
            let index = first_missing - 1;
            if let Some(extent) = shared
                .lock()
                .expect("random access session lock")
                .posting_extents
                .get(&index)
                .copied()
            {
                break extent.end;
            }
            match fetch(&postings[index]) {
                Ok(segment) => {
                    let mut session = shared.lock().expect("random access session lock");
                    session.record_extent(index, segment.segment())?;
                    break session.posting_extents[&index].end;
                }
                Err(code) if salvage_failure(code) => {
                    first_missing = index;
                    if last_missing - first_missing + 1 > MAX_SALVAGE_POSTINGS_PER_HOLE {
                        return Err("session_salvage_budget_exceeded");
                    }
                }
                Err(code) => return Err(code),
            }
        };
        let right_begin = loop {
            let index = last_missing + 1;
            let size = shared.lock().expect("random access session lock").size;
            if index == postings.len() {
                break size + 1;
            }
            if let Some(extent) = shared
                .lock()
                .expect("random access session lock")
                .posting_extents
                .get(&index)
                .copied()
            {
                break extent.begin;
            }
            match fetch(&postings[index]) {
                Ok(segment) => {
                    let mut session = shared.lock().expect("random access session lock");
                    session.record_extent(index, segment.segment())?;
                    break session.posting_extents[&index].begin;
                }
                Err(code) if salvage_failure(code) => {
                    last_missing = index;
                    if last_missing - first_missing + 1 > MAX_SALVAGE_POSTINGS_PER_HOLE {
                        return Err("session_salvage_budget_exceeded");
                    }
                }
                Err(code) => return Err(code),
            }
        };
        let mut session = shared.lock().expect("random access session lock");
        if session.posting_extents.contains_key(&missing_posting_index)
            || session
                .salvage_posting_extents
                .contains_key(&missing_posting_index)
        {
            return Ok(());
        }
        let begin = left_end + 1;
        let end = right_begin - 1;
        if begin > end {
            return Err("session_extent_gap");
        }
        let hole_bytes = end - begin + 1;
        let total_budget =
            (session.size / SALVAGE_FILE_FRACTION_DIVISOR).min(MAX_SALVAGE_TOTAL_BYTES);
        let next_total = session.salvaged_bytes + hole_bytes;
        if hole_bytes > MAX_SALVAGE_HOLE_BYTES
            || session.salvaged_holes >= MAX_SALVAGE_HOLES
            || next_total > total_budget
        {
            return Err("session_salvage_budget_exceeded");
        }
        let extent = SalvageExtent {
            begin,
            end,
            first_posting_index: first_missing,
            last_posting_index: last_missing,
        };
        if session
            .salvage_extents
            .range(..=end)
            .next_back()
            .is_some_and(|(_, existing)| existing.end >= begin)
            || session
                .extents
                .range(..=end)
                .next_back()
                .is_some_and(|(_, existing)| existing.end >= begin)
        {
            return Err("session_extent_overlap");
        }
        if (first_missing..=last_missing)
            .any(|index| session.salvage_posting_extents.contains_key(&index))
        {
            return Err("session_extent_overlap");
        }
        session.salvage_extents.insert(begin, extent);
        for index in first_missing..=last_missing {
            session.salvage_posting_extents.insert(index, extent);
        }
        session.salvaged_bytes = next_total;
        session.salvaged_holes += 1;
        session.asset_revision_hasher = None;
        Ok(())
    }

    fn record_extent(
        &mut self,
        posting_index: usize,
        segment: &VerifiedSegment,
    ) -> Result<(), &'static str> {
        if segment.total_size != self.size
            || (self.postings.len() > 1 && segment.begin == 1 && segment.end == segment.total_size)
        {
            return Err("session_extent_conflict");
        }
        let extent = Extent {
            begin: segment.begin,
            end: segment.end,
            posting_index,
        };
        self.install_extent(extent, true)?;
        self.observe_asset_revision(segment)
    }

    fn observe_asset_revision(&mut self, segment: &VerifiedSegment) -> Result<(), &'static str> {
        if self.salvaged_holes != 0
            || self.strong_asset_revision.is_some()
            || segment.begin != self.asset_revision_next
        {
            return Ok(());
        }
        self.asset_revision_hasher
            .as_mut()
            .expect("asset revision hasher exists until completion or salvage")
            .update(segment.bytes());
        self.asset_revision_next = segment.end + 1;
        if self.posting_extents.len() == self.postings.len() {
            let mut next = 1_u64;
            for extent in self.extents.values() {
                if extent.begin != next {
                    return Err("session_extent_gap");
                }
                next = extent.end + 1;
            }
            if next != self.size + 1 {
                return Err("session_extent_gap");
            }
            self.census_complete = true;
        }
        if self.census_complete && self.asset_revision_next == self.size + 1 {
            let digest = self
                .asset_revision_hasher
                .take()
                .expect("asset revision hasher exists until completion")
                .finalize();
            self.strong_asset_revision = Some(format!("{digest:x}"));
        }
        Ok(())
    }

    fn install_extent(&mut self, extent: Extent, pending: bool) -> Result<(), &'static str> {
        if extent.posting_index >= self.postings.len()
            || extent.begin == 0
            || extent.end < extent.begin
            || extent.end > self.size
            || (self.postings.len() > 1 && extent.begin == 1 && extent.end == self.size)
        {
            return Err("session_extent_conflict");
        }
        if let Some(previous) = self.posting_extents.get(&extent.posting_index) {
            return if *previous == extent {
                Ok(())
            } else {
                Err("session_extent_conflict")
            };
        }
        let previous = self
            .extents
            .range(..=extent.begin)
            .next_back()
            .map(|(_, previous)| previous);
        let next = self
            .extents
            .range(extent.begin..)
            .next()
            .map(|(_, next)| next);
        if previous.is_some_and(|previous| previous.end >= extent.begin)
            || next.is_some_and(|next| next.begin <= extent.end)
        {
            return Err("session_extent_overlap");
        }
        if previous.is_some_and(|previous| previous.posting_index >= extent.posting_index)
            || next.is_some_and(|next| next.posting_index <= extent.posting_index)
        {
            return Err("session_extent_conflict");
        }
        self.extents.insert(extent.begin, extent);
        self.posting_extents.insert(extent.posting_index, extent);
        if pending {
            self.pending_checkpoints.push(SessionCheckpoint {
                posting_index: extent.posting_index,
                begin: extent.begin,
                end: extent.end,
                total_size: self.size,
            });
        }
        Ok(())
    }

    fn extent_containing(&self, logical: u64) -> Option<Extent> {
        self.extents
            .range(..=logical)
            .next_back()
            .map(|(_, extent)| *extent)
            .filter(|extent| logical <= extent.end)
    }

    fn salvage_containing(&self, logical: u64) -> Option<SalvageExtent> {
        self.salvage_extents
            .range(..=logical)
            .next_back()
            .map(|(_, extent)| *extent)
            .filter(|extent| logical <= extent.end)
    }
}

fn salvage_failure(code: &str) -> bool {
    code == "nntp_article_missing" || crate::yenc::integrity_failure(code)
}

pub fn session_recreation_key(
    account_partition: [u8; 32],
    provider_set_generation: &str,
    group: Option<&str>,
    postings: &[SessionPosting],
) -> String {
    session_recreation_key_with_degraded_playback(
        account_partition,
        provider_set_generation,
        group,
        postings,
        false,
    )
}

pub fn session_recreation_key_with_degraded_playback(
    account_partition: [u8; 32],
    provider_set_generation: &str,
    group: Option<&str>,
    postings: &[SessionPosting],
    allow_degraded_playback: bool,
) -> String {
    let mut digest = Sha256::new();
    digest.update(b"comet-random-access-session-v3\0");
    digest.update([u8::from(allow_degraded_playback)]);
    digest.update(account_partition);
    append_length_prefixed(&mut digest, provider_set_generation.as_bytes());
    append_length_prefixed(&mut digest, group.unwrap_or("").as_bytes());
    for posting in postings {
        digest.update(posting.number.to_be_bytes());
        digest.update(posting.declared_encoded_bytes.to_be_bytes());
        append_length_prefixed(&mut digest, posting.message_id.as_bytes());
        digest.update((posting.fallback_postings.len() as u64).to_be_bytes());
        for fallback in &posting.fallback_postings {
            digest.update(fallback.declared_encoded_bytes.to_be_bytes());
            append_length_prefixed(&mut digest, fallback.message_id.as_bytes());
        }
    }
    format!("{:x}", digest.finalize())
}

pub fn random_session_id() -> Result<String, &'static str> {
    let encoded = random_lease_id().map_err(|_| "session_random_unavailable")?;
    debug_assert!(valid_session_id(&encoded));
    Ok(encoded)
}

fn append_length_prefixed(digest: &mut Sha256, value: &[u8]) {
    digest.update((value.len() as u64).to_be_bytes());
    digest.update(value);
}

pub fn valid_session_id(value: &str) -> bool {
    value.len() == 22
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

#[cfg(test)]
mod tests {
    use super::{
        MAX_DECLARED_POSTING_BYTES, RandomAccessSession, SESSION_IDLE_TTL, SessionFallbackPosting,
        SessionPosting, SessionRegistry, random_session_id, session_recreation_key,
        session_recreation_key_with_degraded_playback,
    };
    use crate::cache::{SegmentLease, SessionCheckpointStore, VerifiedSegment};
    use crate::materialization::asset_revision_hasher;
    use crate::yenc::DecodedPart;
    use sha2::Digest;
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    fn temporary_directory(label: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "comet-session-{label}-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir(&path).unwrap();
        path
    }

    fn posting(number: u64) -> SessionPosting {
        SessionPosting {
            number,
            declared_encoded_bytes: 10,
            message_id: format!("{number}@example.test"),
            fallback_postings: Vec::new(),
        }
    }

    fn identity() -> String {
        "A".repeat(22)
    }

    fn recreation_key() -> String {
        "b".repeat(64)
    }

    fn segment(begin: u64, end: u64, total: u64, value: u8) -> Arc<VerifiedSegment> {
        Arc::new(
            VerifiedSegment::from_decoded(DecodedPart {
                bytes: vec![value; usize::try_from(end - begin + 1).unwrap()],
                begin,
                end,
                total_size: total,
                expected_crc32: Some(u32::from(value)),
                expected_whole_crc32: None,
            })
            .unwrap(),
        )
    }

    fn fetched(segment: &Arc<VerifiedSegment>) -> SegmentLease {
        SegmentLease::detached(Arc::clone(segment))
    }

    fn fixture() -> (
        Arc<Mutex<RandomAccessSession>>,
        HashMap<u64, Arc<VerifiedSegment>>,
    ) {
        let postings = (1..=10).map(posting).collect::<Vec<_>>();
        let segments = (1..=10)
            .map(|number| {
                let begin = (number - 1) * 10 + 1;
                (number, segment(begin, begin + 9, 100, number as u8))
            })
            .collect::<HashMap<_, _>>();
        let session = RandomAccessSession::new(identity(), postings, &segments[&1]).unwrap();
        (Arc::new(Mutex::new(session)), segments)
    }

    #[test]
    fn derives_a_stable_partitioned_length_delimited_identity() {
        let mut first_posting = posting(1);
        first_posting.fallback_postings = vec![
            SessionFallbackPosting {
                declared_encoded_bytes: 11,
                message_id: "fallback-a@example.test".into(),
            },
            SessionFallbackPosting {
                declared_encoded_bytes: 12,
                message_id: "fallback-b@example.test".into(),
            },
        ];
        let postings = vec![first_posting, posting(2)];
        let first = session_recreation_key([1; 32], "generation", Some("alt.video"), &postings);
        let second = session_recreation_key([1; 32], "generation", Some("alt.video"), &postings);
        let foreign = session_recreation_key([2; 32], "generation", Some("alt.video"), &postings);
        let other_group =
            session_recreation_key([1; 32], "generation", Some("alt.other"), &postings);
        let mut reordered_postings = postings.clone();
        reordered_postings[0].fallback_postings.reverse();
        let reordered = session_recreation_key(
            [1; 32],
            "generation",
            Some("alt.video"),
            &reordered_postings,
        );
        let mut resized_primary = postings.clone();
        resized_primary[0].declared_encoded_bytes += 1;
        let resized_primary =
            session_recreation_key([1; 32], "generation", Some("alt.video"), &resized_primary);
        let mut resized_fallback = postings.clone();
        resized_fallback[0].fallback_postings[0].declared_encoded_bytes += 1;
        let resized_fallback =
            session_recreation_key([1; 32], "generation", Some("alt.video"), &resized_fallback);

        assert_eq!(first, second);
        assert_ne!(first, foreign);
        assert_ne!(first, other_group);
        assert_ne!(first, reordered);
        assert_ne!(first, resized_primary);
        assert_ne!(first, resized_fallback);
        assert_ne!(
            first,
            session_recreation_key_with_degraded_playback(
                [1; 32],
                "generation",
                Some("alt.video"),
                &postings,
                true,
            )
        );
        assert_eq!(first.len(), 64);
    }

    #[test]
    fn random_session_ids_are_canonical_base64url_and_non_deterministic() {
        let first = random_session_id().unwrap();
        let second = random_session_id().unwrap();
        assert_eq!(first.len(), 22);
        assert!(
            first
                .bytes()
                .all(|byte| { byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_') })
        );
        assert_ne!(first, second);
    }

    #[test]
    fn seeks_with_sparse_probes_without_fetching_preceding_postings() {
        let (session, segments) = fixture();
        let mut fetched_numbers = Vec::new();

        let bytes = RandomAccessSession::read_at(&session, 84, 4, |posting| {
            fetched_numbers.push(posting.number);
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();

        assert_eq!(bytes, vec![9; 4]);
        assert!(!fetched_numbers.contains(&2));
        assert!(fetched_numbers.len() <= 2);
        assert_eq!(session.lock().unwrap().strong_asset_revision(), None);
    }

    #[test]
    fn releases_the_session_lock_while_fetching_postings() {
        let (session, segments) = fixture();
        let shared = Arc::clone(&session);
        let mut did_fetch = false;

        let bytes = RandomAccessSession::read_at(&session, 84, 1, |posting| {
            assert!(
                shared.try_lock().is_ok(),
                "a network fetch must not serialize concurrent session reads"
            );
            did_fetch = true;
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();

        assert!(did_fetch);
        assert_eq!(bytes, [9]);
    }

    #[test]
    fn large_skewed_seeks_remain_logarithmic() {
        const POSTING_COUNT: u64 = 4096;
        const TARGET_POSTING: u64 = POSTING_COUNT - 1;

        let mut postings = (1..=POSTING_COUNT).map(posting).collect::<Vec<_>>();
        postings[0].declared_encoded_bytes = MAX_DECLARED_POSTING_BYTES;
        for posting in &mut postings[1..] {
            posting.declared_encoded_bytes = 1;
        }
        let segments = (1..=POSTING_COUNT)
            .map(|number| {
                (
                    number,
                    segment(
                        number,
                        number,
                        POSTING_COUNT,
                        u8::try_from(number % 251).unwrap(),
                    ),
                )
            })
            .collect::<HashMap<_, _>>();
        let session = Arc::new(Mutex::new(
            RandomAccessSession::new(identity(), postings, &segments[&1]).unwrap(),
        ));
        let mut fetched_numbers = Vec::new();

        let bytes = RandomAccessSession::read_at(&session, TARGET_POSTING - 1, 1, |posting| {
            fetched_numbers.push(posting.number);
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();

        assert_eq!(bytes, [u8::try_from(TARGET_POSTING % 251).unwrap()]);
        assert_eq!(fetched_numbers.last(), Some(&TARGET_POSTING));
        assert!(fetched_numbers.len() > 8);
        assert!(fetched_numbers.len() <= POSTING_COUNT.ilog2() as usize);
    }

    #[test]
    fn promotes_only_a_complete_ordered_byte_pass_to_a_strong_revision() {
        let (session, segments) = fixture();
        RandomAccessSession::read_at(&session, 84, 4, |posting| {
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();
        assert_eq!(session.lock().unwrap().strong_asset_revision(), None);

        let bytes = RandomAccessSession::read_at(&session, 0, 100, |posting| {
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();
        let mut expected = asset_revision_hasher(100);
        expected.update(&bytes);
        let expected = format!("{:x}", expected.finalize());

        assert_eq!(
            session.lock().unwrap().strong_asset_revision(),
            Some(expected.as_str())
        );
    }

    #[test]
    fn a_complete_single_posting_has_an_immediate_strong_revision() {
        let first = segment(1, 3, 3, 7);
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let mut expected = asset_revision_hasher(3);
        expected.update(first.bytes());
        let expected = format!("{:x}", expected.finalize());

        assert_eq!(session.strong_asset_revision(), Some(expected.as_str()));
    }

    #[test]
    fn persisted_checkpoints_survive_recreation_but_are_reverified_before_use() {
        let root = temporary_directory("checkpoint-recreation");
        let key = recreation_key();
        let (first_session, segments) = fixture();
        RandomAccessSession::read_at(&first_session, 80, 1, |posting| {
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();
        let mut store = SessionCheckpointStore::open(&root).unwrap();
        store
            .merge(&key, first_session.lock().unwrap().pending_checkpoints())
            .unwrap();
        drop(store);

        let postings = (1..=10).map(posting).collect::<Vec<_>>();
        let mut recreated = RandomAccessSession::new(identity(), postings, &segments[&1]).unwrap();
        let checkpoints = SessionCheckpointStore::open(&root)
            .unwrap()
            .load(&key, 10, 100)
            .unwrap();
        recreated.restore_checkpoints(&checkpoints).unwrap();
        let recreated = Arc::new(Mutex::new(recreated));
        let mut fetched_postings = Vec::new();
        let bytes = RandomAccessSession::read_at(&recreated, 80, 1, |posting| {
            fetched_postings.push(posting.number);
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();

        assert_eq!(bytes, [9]);
        assert_eq!(fetched_postings, [9]);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn seek_estimates_use_cumulative_declared_posting_sizes() {
        let mut postings = vec![posting(1), posting(2), posting(3)];
        postings[0].declared_encoded_bytes = 10;
        postings[1].declared_encoded_bytes = 10;
        postings[2].declared_encoded_bytes = 80;
        let segments = [
            (1, segment(1, 10, 100, 1)),
            (2, segment(11, 20, 100, 2)),
            (3, segment(21, 100, 100, 3)),
        ]
        .into_iter()
        .collect::<HashMap<_, _>>();
        let session = Arc::new(Mutex::new(
            RandomAccessSession::new(identity(), postings, &segments[&1]).unwrap(),
        ));
        let mut fetched_numbers = Vec::new();

        let bytes = RandomAccessSession::read_at(&session, 60, 1, |posting| {
            fetched_numbers.push(posting.number);
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();

        assert_eq!(bytes, [3]);
        assert_eq!(fetched_numbers, [3]);
    }

    #[test]
    fn prefetch_starts_at_the_next_unproven_part_boundary() {
        let (session, segments) = fixture();

        assert_eq!(session.lock().unwrap().prefetch_start_after(0), Some(1));
        let (postings, indexes) = session.lock().unwrap().postings_for_prefetch(1, 20);
        assert_eq!(
            indexes
                .into_iter()
                .map(|index| (index, postings[index].number))
                .collect::<Vec<_>>(),
            [(1, 2), (2, 3)]
        );
        session
            .lock()
            .unwrap()
            .record_prefetched(1, &segments[&2])
            .expect("record verified prefetched extent");
        let (postings, indexes) = session.lock().unwrap().postings_for_prefetch(1, 20);
        assert_eq!(
            indexes
                .into_iter()
                .map(|index| (index, postings[index].number))
                .collect::<Vec<_>>(),
            [(2, 3)]
        );
        assert_eq!(session.lock().unwrap().prefetch_start_after(10), Some(2));
    }

    #[test]
    fn reads_exactly_across_proven_extent_boundaries() {
        let (session, segments) = fixture();

        let bytes = RandomAccessSession::read_at(&session, 8, 5, |posting| {
            Ok(fetched(&segments[&posting.number]))
        })
        .unwrap();

        assert_eq!(bytes, vec![1, 1, 2, 2, 2]);
    }

    #[test]
    fn degraded_playback_fills_only_one_exact_header_proven_hole_idempotently() {
        let postings = (1..=4).map(posting).collect::<Vec<_>>();
        let segments = HashMap::from([
            (1, segment(1, 3, 1200, 1)),
            (3, segment(7, 9, 1200, 3)),
            (4, segment(10, 1200, 1200, 4)),
        ]);
        let session = Arc::new(Mutex::new(
            RandomAccessSession::new_with_degraded_playback(
                identity(),
                postings,
                &segments[&1],
                true,
            )
            .unwrap(),
        ));
        let mut missing_attempts = 0;

        let bytes = RandomAccessSession::read_at(&session, 0, 12, |posting| {
            if posting.number == 2 {
                missing_attempts += 1;
                Err("nntp_article_missing")
            } else {
                Ok(fetched(&segments[&posting.number]))
            }
        })
        .unwrap();

        assert_eq!(bytes, [1, 1, 1, 0, 0, 0, 3, 3, 3, 4, 4, 4]);
        assert_eq!(missing_attempts, 1);
        assert_eq!(session.lock().unwrap().salvage_state(), (true, 3, 1));
        assert_eq!(session.lock().unwrap().salvage_delta(), (3, 1));
        assert_eq!(session.lock().unwrap().salvage_delta(), (0, 0));
        assert_eq!(
            RandomAccessSession::read_at(&session, 3, 3, |_| panic!(
                "an installed salvage hole must be idempotent"
            ))
            .unwrap(),
            [0, 0, 0]
        );
        assert_eq!(session.lock().unwrap().salvage_state(), (true, 3, 1));
        assert!(session.lock().unwrap().strong_asset_revision().is_none());
    }

    #[test]
    fn degraded_playback_never_fills_transient_or_unproven_failures() {
        let postings = (1..=3).map(posting).collect::<Vec<_>>();
        let segments = HashMap::from([(1, segment(1, 3, 1200, 1)), (3, segment(7, 1200, 1200, 3))]);
        for (enabled, failure) in [
            (false, "nntp_article_missing"),
            (true, "nntp_connect_failed"),
        ] {
            let session = Arc::new(Mutex::new(
                RandomAccessSession::new_with_degraded_playback(
                    identity(),
                    postings.clone(),
                    &segments[&1],
                    enabled,
                )
                .unwrap(),
            ));

            assert_eq!(
                RandomAccessSession::read_at(&session, 3, 1, |posting| {
                    if posting.number == 2 {
                        Err(failure)
                    } else {
                        Ok(fetched(&segments[&posting.number]))
                    }
                }),
                Err(failure)
            );
            assert_eq!(session.lock().unwrap().salvage_state(), (false, 0, 0));
        }
    }

    #[test]
    fn degraded_playback_rejects_holes_over_the_fractional_budget() {
        let postings = (1..=3).map(posting).collect::<Vec<_>>();
        let segments = HashMap::from([(1, segment(1, 3, 1200, 1)), (3, segment(8, 1200, 1200, 3))]);
        let session = Arc::new(Mutex::new(
            RandomAccessSession::new_with_degraded_playback(
                identity(),
                postings,
                &segments[&1],
                true,
            )
            .unwrap(),
        ));

        assert_eq!(
            RandomAccessSession::read_at(&session, 3, 1, |posting| {
                if posting.number == 2 {
                    Err("yenc_crc_mismatch")
                } else {
                    Ok(fetched(&segments[&posting.number]))
                }
            }),
            Err("session_salvage_budget_exceeded")
        );
        assert_eq!(session.lock().unwrap().salvage_state(), (false, 0, 0));
    }

    #[test]
    fn rejects_a_proven_gap_without_fetching_the_whole_file() {
        let postings = (1..=10).map(posting).collect::<Vec<_>>();
        let mut segments = (1..=10)
            .map(|number| {
                let begin = (number - 1) * 10 + 1;
                (number, segment(begin, begin + 9, 100, number as u8))
            })
            .collect::<HashMap<_, _>>();
        segments.insert(5, segment(42, 50, 100, 5));
        let session = Arc::new(Mutex::new(
            RandomAccessSession::new(identity(), postings, &segments[&1]).unwrap(),
        ));
        let mut fetched_numbers = Vec::new();

        assert_eq!(
            RandomAccessSession::read_at(&session, 40, 20, |posting| {
                fetched_numbers.push(posting.number);
                Ok(fetched(&segments[&posting.number]))
            }),
            Err("session_extent_gap")
        );
        assert!(fetched_numbers.len() < 10);
    }

    #[test]
    fn rejects_overlaps_and_inconsistent_total_sizes() {
        let postings = vec![posting(1), posting(2)];
        let first = segment(1, 5, 10, 1);
        let session = Arc::new(Mutex::new(
            RandomAccessSession::new(identity(), postings, &first).unwrap(),
        ));

        assert_eq!(
            RandomAccessSession::read_at(&session, 5, 1, |_| {
                Ok(SegmentLease::detached(segment(5, 10, 10, 2)))
            }),
            Err("session_extent_overlap")
        );
        assert_eq!(
            RandomAccessSession::read_at(&session, 5, 1, |_| {
                Ok(SegmentLease::detached(segment(6, 10, 11, 2)))
            }),
            Err("session_extent_conflict")
        );
    }

    #[test]
    fn enforces_exact_range_bounds() {
        let (session, segments) = fixture();

        assert_eq!(
            RandomAccessSession::read_at(&session, 100, 1, |posting| {
                Ok(fetched(&segments[&posting.number]))
            }),
            Err("invalid_session_range")
        );
        assert_eq!(
            RandomAccessSession::read_at(&session, 0, 0, |posting| {
                Ok(fetched(&segments[&posting.number]))
            }),
            Err("invalid_session_range")
        );
    }

    #[test]
    fn registry_expiry_does_not_invalidate_an_active_session_lease() {
        let first = segment(1, 1, 1, 1);
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let now = Instant::now();
        let mut registry = SessionRegistry::new(1024 * 1024);
        registry
            .insert(session, "context", recreation_key(), now)
            .unwrap();
        let lease = registry.get(&identity(), now).unwrap();

        assert_eq!(registry.len(now + Duration::from_secs(15 * 60)), 1);
        assert_eq!(
            RandomAccessSession::read_at(&lease.session, 0, 1, |_| Ok(fetched(&first))).unwrap(),
            [1]
        );
        drop(lease);
        assert_eq!(registry.len(now + Duration::from_secs(15 * 60)), 0);
        registry
            .insert(
                RandomAccessSession::new("B".repeat(22), vec![posting(1)], &first).unwrap(),
                "replacement-context",
                "c".repeat(64),
                now + Duration::from_secs(15 * 60),
            )
            .expect("expired session releases its metadata budget");
    }

    #[test]
    fn registry_converges_recreation_key_races_on_the_first_random_id() {
        let first = segment(1, 1, 1, 1);
        let now = Instant::now();
        let mut registry = SessionRegistry::new(1024 * 1024);
        let first_result = registry
            .insert(
                RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap(),
                "first-context",
                recreation_key(),
                now,
            )
            .unwrap();
        let second_result = registry
            .insert(
                RandomAccessSession::new("B".repeat(22), vec![posting(1)], &first).unwrap(),
                "second-context",
                recreation_key(),
                now,
            )
            .unwrap();

        let mut expected = asset_revision_hasher(1);
        expected.update(first.bytes());
        assert_eq!(
            first_result,
            (identity(), 1, Some(format!("{:x}", expected.finalize())),)
        );
        assert_eq!(second_result, first_result);
        assert_eq!(registry.len(now), 1);
    }

    #[test]
    fn registry_evicts_idle_metadata_but_never_busy_sessions() {
        let first = segment(1, 1, 1, 1);
        let now = Instant::now();
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let budget = session.retained_metadata_bytes();
        let mut registry = SessionRegistry::new(budget);
        registry
            .insert(session, "first-context", recreation_key(), now)
            .unwrap();
        registry
            .insert(
                RandomAccessSession::new("B".repeat(22), vec![posting(1)], &first).unwrap(),
                "second-context",
                "c".repeat(64),
                now + Duration::from_secs(1),
            )
            .expect("evict idle metadata to admit the second session");

        assert!(matches!(
            registry.get(&identity(), now + Duration::from_secs(1)),
            Err("session_unavailable")
        ));
        drop(
            registry
                .get(&"B".repeat(22), now + Duration::from_secs(1))
                .expect("second session remains"),
        );

        let mut busy_registry = SessionRegistry::new(budget);
        busy_registry
            .insert(
                RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap(),
                "busy-context",
                recreation_key(),
                now,
            )
            .unwrap();
        let reader = busy_registry.open_reader(&identity(), now).unwrap();
        let rejected = RandomAccessSession::new("B".repeat(22), vec![posting(1)], &first).unwrap();
        assert_eq!(
            busy_registry
                .insert(
                    rejected,
                    "rejected-context",
                    "c".repeat(64),
                    now + Duration::from_secs(1),
                )
                .err(),
            Some("session_capacity")
        );
        assert_eq!(
            busy_registry.close_reader(&identity(), &reader, now),
            Ok(())
        );
    }

    #[test]
    fn registry_enforces_eight_readers_and_refuses_busy_deletion() {
        let first = segment(1, 1, 1, 1);
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let now = Instant::now();
        let mut registry = SessionRegistry::new(1024 * 1024);
        registry
            .insert(session, "context", recreation_key(), now)
            .unwrap();
        let mut leases = (0..8)
            .map(|_| registry.get(&identity(), now).unwrap())
            .collect::<Vec<_>>();

        assert!(matches!(
            registry.get(&identity(), now),
            Err("session_reader_capacity")
        ));
        assert_eq!(registry.remove(&identity(), now), Err("session_busy"));
        leases.pop();
        assert!(registry.get(&identity(), now).is_ok());
        drop(leases);
        assert_eq!(registry.remove(&identity(), now), Ok(()));
    }

    #[test]
    fn persistent_reader_lease_spans_reads_and_releases_exactly_once() {
        let first = segment(1, 1, 1, 1);
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let now = Instant::now();
        let mut registry = SessionRegistry::new(1024 * 1024);
        registry
            .insert(session, "context", recreation_key(), now)
            .unwrap();
        let reader = registry.open_reader(&identity(), now).unwrap();
        let lease = registry.get_with_reader(&identity(), &reader, now).unwrap();

        assert_eq!(
            registry.get_with_reader(&identity(), &reader, now).err(),
            Some("session_reader_busy")
        );
        assert_eq!(registry.close_reader(&identity(), &reader, now), Ok(()));
        assert!(matches!(
            registry.get_with_reader(&identity(), &reader, now),
            Err("session_reader_unavailable")
        ));
        assert_eq!(registry.remove(&identity(), now), Ok(()));
        assert_eq!(
            RandomAccessSession::read_at(&lease.session, 0, 1, |_| Ok(fetched(&first))).unwrap(),
            [1]
        );
        drop(lease);
        assert_eq!(
            registry.close_reader(&identity(), &reader, now),
            Err("session_unavailable")
        );
    }

    #[test]
    fn abandoned_persistent_reader_expires_and_unblocks_session_gc() {
        let first = segment(1, 1, 1, 1);
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let now = Instant::now();
        let mut registry = SessionRegistry::new(1024 * 1024);
        registry
            .insert(session, "context", recreation_key(), now)
            .unwrap();
        let reader = registry.open_reader(&identity(), now).unwrap();

        assert_eq!(registry.len(now + SESSION_IDLE_TTL), 0);
        assert_eq!(
            registry.close_reader(&identity(), &reader, now + SESSION_IDLE_TTL),
            Err("session_unavailable")
        );
    }

    #[test]
    fn absolute_ttl_expires_even_when_idle_access_is_refreshed() {
        let first = segment(1, 1, 1, 1);
        let session = RandomAccessSession::new(identity(), vec![posting(1)], &first).unwrap();
        let now = Instant::now();
        let mut registry = SessionRegistry::new(1024 * 1024);
        registry
            .insert(session, "context", recreation_key(), now)
            .unwrap();
        for ten_minutes in 1..36 {
            drop(
                registry
                    .get(
                        &identity(),
                        now + Duration::from_secs(ten_minutes * 10 * 60),
                    )
                    .unwrap(),
            );
        }

        assert!(matches!(
            registry.get(&identity(), now + Duration::from_secs(6 * 60 * 60)),
            Err("session_unavailable")
        ));
    }
}
