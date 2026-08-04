use crate::archive_group::VolumePlan;
use crate::materialization::ImmutableFileIdentity;
use crate::reader_lease::{ReaderGeneration, ReaderLeaseError, ReaderLeases, ReaderPermit};
use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::sync::Arc;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

pub const MAX_COMPOSITE_PARTS: usize = crate::archive_group::MAX_VOLUMES;
const MAX_COMPOSITE_BYTES: u64 = crate::limits::MAX_LOGICAL_BYTES;
pub const MIN_COMPOSITE_METADATA_BUDGET_BYTES: usize = 8 * 1024 * 1024;
const COMPOSITE_BASE_METADATA_BYTES: usize = 512;
const COMPOSITE_PART_METADATA_BYTES: usize = 384;
const MAX_COMPOSITE_READERS: usize = 8;
const COMPOSITE_IDLE_TTL: Duration = Duration::from_secs(15 * 60);
const COMPOSITE_ABSOLUTE_TTL: Duration = Duration::from_secs(6 * 60 * 60);

#[derive(Clone)]
pub enum RawCompositeBacking {
    Materialization(ImmutableFileIdentity),
    Session {
        identity: String,
        revision: String,
        exact_size: u64,
        _retention: crate::session::SessionRetention,
    },
    Aes256CbcSessions {
        sessions: Arc<SessionSet>,
        pack_offset: u64,
        pack_size: u64,
        key: Arc<Zeroizing<[u8; 32]>>,
        initial_vector: [u8; 16],
    },
}

#[derive(Clone, Debug)]
pub struct SessionSource {
    pub identity: String,
    pub revision: String,
    pub exact_size: u64,
    pub retention: crate::session::SessionRetention,
}

#[derive(Clone, Debug)]
pub struct SessionSet {
    sessions: Vec<SessionSource>,
    prefix_ends: Vec<u64>,
    exact_size: u64,
}

impl SessionSet {
    pub fn new(sessions: Vec<SessionSource>) -> Result<Self, &'static str> {
        if sessions.is_empty() || sessions.len() > MAX_COMPOSITE_PARTS {
            return Err("invalid_raw_composite");
        }
        let mut exact_size = 0_u64;
        let mut identities = std::collections::BTreeSet::new();
        let mut revisions = std::collections::BTreeSet::new();
        let mut prefix_ends = Vec::with_capacity(sessions.len());
        for session in &sessions {
            if session.exact_size == 0
                || !identities.insert(&session.identity)
                || !revisions.insert(&session.revision)
            {
                return Err("invalid_raw_composite");
            }
            exact_size = exact_size
                .checked_add(session.exact_size)
                .ok_or("invalid_raw_composite")?;
            if exact_size > MAX_COMPOSITE_BYTES {
                return Err("invalid_raw_composite");
            }
            prefix_ends.push(exact_size);
        }
        Ok(Self {
            sessions,
            prefix_ends,
            exact_size,
        })
    }

    pub fn exact_size(&self) -> u64 {
        self.exact_size
    }

    pub fn ranges(
        &self,
        offset: u64,
        length: u64,
    ) -> Result<Vec<(&SessionSource, u64, u64)>, &'static str> {
        let end = offset.checked_add(length).ok_or("raw_composite_conflict")?;
        if length == 0 || end > self.exact_size {
            return Err("raw_composite_conflict");
        }
        let mut ranges = Vec::new();
        let mut position = offset;
        while position < end {
            let index = self
                .prefix_ends
                .partition_point(|prefix| *prefix <= position);
            let session = self.sessions.get(index).ok_or("raw_composite_conflict")?;
            let session_start = if index == 0 {
                0
            } else {
                self.prefix_ends[index - 1]
            };
            let source_offset = position - session_start;
            let available = session.exact_size - source_offset;
            let range_length = available.min(end - position);
            ranges.push((session, source_offset, range_length));
            position += range_length;
        }
        Ok(ranges)
    }
}

impl PartialEq for RawCompositeBacking {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Materialization(left), Self::Materialization(right)) => left == right,
            (
                Self::Session {
                    identity: left_identity,
                    revision: left_revision,
                    exact_size: left_size,
                    ..
                },
                Self::Session {
                    identity: right_identity,
                    revision: right_revision,
                    exact_size: right_size,
                    ..
                },
            ) => {
                left_identity == right_identity
                    && left_revision == right_revision
                    && left_size == right_size
            }
            (
                Self::Aes256CbcSessions {
                    sessions: left_sessions,
                    pack_offset: left_offset,
                    pack_size: left_size,
                    key: left_key,
                    initial_vector: left_vector,
                },
                Self::Aes256CbcSessions {
                    sessions: right_sessions,
                    pack_offset: right_offset,
                    pack_size: right_size,
                    key: right_key,
                    initial_vector: right_vector,
                },
            ) => {
                left_sessions == right_sessions
                    && left_offset == right_offset
                    && left_size == right_size
                    && left_key == right_key
                    && left_vector == right_vector
            }
            _ => false,
        }
    }
}

impl Eq for RawCompositeBacking {}

impl RawCompositeBacking {
    fn exact_size(&self) -> u64 {
        match self {
            Self::Materialization(identity) => identity.size,
            Self::Session { exact_size, .. } => *exact_size,
            Self::Aes256CbcSessions { pack_size, .. } => *pack_size,
        }
    }
}

impl fmt::Debug for RawCompositeBacking {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Materialization(identity) => formatter
                .debug_tuple("Materialization")
                .field(identity)
                .finish(),
            Self::Session {
                identity,
                revision,
                exact_size,
                ..
            } => formatter
                .debug_struct("Session")
                .field("identity", identity)
                .field("revision", revision)
                .field("exact_size", exact_size)
                .finish(),
            Self::Aes256CbcSessions {
                sessions,
                pack_offset,
                pack_size,
                initial_vector,
                ..
            } => formatter
                .debug_struct("Aes256CbcSessions")
                .field("sessions", sessions)
                .field("pack_offset", pack_offset)
                .field("pack_size", pack_size)
                .field("initial_vector", initial_vector)
                .field("key", &"[redacted]")
                .finish(),
        }
    }
}

impl PartialEq for SessionSource {
    fn eq(&self, other: &Self) -> bool {
        self.identity == other.identity
            && self.revision == other.revision
            && self.exact_size == other.exact_size
    }
}

impl Eq for SessionSource {}

impl PartialEq for SessionSet {
    fn eq(&self, other: &Self) -> bool {
        self.sessions == other.sessions
    }
}

impl Eq for SessionSet {}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RawCompositePart {
    pub content_identity: String,
    pub source_offset: u64,
    pub exact_size: u64,
    pub backing: RawCompositeBacking,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RawCompositeSource {
    identity: String,
    exact_size: u64,
    parts: Vec<RawCompositePart>,
    prefix_ends: Vec<u64>,
}

impl RawCompositeSource {
    pub fn from_materialization(
        content_identity: String,
        exact_size: u64,
        file_identity: ImmutableFileIdentity,
    ) -> Result<Self, &'static str> {
        Ok(Self {
            identity: content_identity.clone(),
            exact_size,
            parts: vec![RawCompositePart {
                content_identity,
                source_offset: 0,
                exact_size,
                backing: RawCompositeBacking::Materialization(file_identity),
            }],
            prefix_ends: vec![exact_size],
        })
    }

    pub fn from_plan(
        plan: VolumePlan,
        mut file_identities: BTreeMap<String, ImmutableFileIdentity>,
    ) -> Result<Self, &'static str> {
        let mut total = 0_u64;
        let mut prefix_ends = Vec::with_capacity(plan.volumes.len());
        let mut parts = Vec::with_capacity(plan.volumes.len());
        for volume in plan.volumes {
            total += volume.exact_size;
            prefix_ends.push(total);
            parts.push(RawCompositePart {
                backing: RawCompositeBacking::Materialization(
                    file_identities
                        .remove(&volume.content_identity)
                        .expect("planned materialization identity"),
                ),
                content_identity: volume.content_identity,
                source_offset: 0,
                exact_size: volume.exact_size,
            });
        }
        Ok(Self {
            identity: plan.set_identity,
            exact_size: plan.exact_size,
            parts,
            prefix_ends,
        })
    }

    pub fn from_ranges(
        identity: String,
        parts: Vec<RawCompositePart>,
    ) -> Result<Self, &'static str> {
        if !(1..=MAX_COMPOSITE_PARTS).contains(&parts.len()) {
            return Err("invalid_raw_composite");
        }
        let mut exact_size = 0_u64;
        let mut prefix_ends = Vec::with_capacity(parts.len());
        for part in &parts {
            if part.exact_size == 0
                || part
                    .source_offset
                    .checked_add(part.exact_size)
                    .is_none_or(|end| end > part.backing.exact_size())
            {
                return Err("invalid_raw_composite");
            }
            exact_size = exact_size
                .checked_add(part.exact_size)
                .ok_or("invalid_raw_composite")?;
            if exact_size > MAX_COMPOSITE_BYTES {
                return Err("invalid_raw_composite");
            }
            prefix_ends.push(exact_size);
        }
        Ok(Self {
            identity,
            exact_size,
            parts,
            prefix_ends,
        })
    }

    pub fn identity(&self) -> &str {
        &self.identity
    }

    pub fn exact_size(&self) -> u64 {
        self.exact_size
    }

    fn retained_metadata_bytes(&self) -> usize {
        COMPOSITE_BASE_METADATA_BYTES + self.parts.len() * COMPOSITE_PART_METADATA_BYTES
    }

    pub fn read_at<F, C>(
        &self,
        offset: u64,
        length: u64,
        cancelled: &C,
        mut read_part: F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: FnMut(&RawCompositePart, u64, u64) -> Result<Vec<u8>, &'static str>,
        C: Fn() -> bool,
    {
        if length == 0
            || length > crate::limits::MAX_RANGE_BYTES
            || offset >= self.exact_size
            || offset
                .checked_add(length)
                .is_none_or(|end| end > self.exact_size)
        {
            return Err("invalid_raw_composite_range");
        }
        if cancelled() {
            return Err("raw_composite_cancelled");
        }
        let mut output =
            Vec::with_capacity(usize::try_from(length).map_err(|_| "invalid_raw_composite_range")?);
        let mut logical = offset;
        let final_offset = offset
            .checked_add(length)
            .ok_or("invalid_raw_composite_range")?;
        while logical < final_offset {
            if cancelled() {
                return Err("raw_composite_cancelled");
            }
            let index = self.prefix_ends.partition_point(|end| *end <= logical);
            let part = self.parts.get(index).ok_or("raw_composite_conflict")?;
            let part_begin = if index == 0 {
                0
            } else {
                self.prefix_ends[index - 1]
            };
            let part_offset = logical
                .checked_sub(part_begin)
                .ok_or("raw_composite_conflict")?;
            let available = part
                .exact_size
                .checked_sub(part_offset)
                .ok_or("raw_composite_conflict")?;
            let part_length = available.min(final_offset - logical);
            let part_end = part_offset
                .checked_add(part_length)
                .and_then(|end| end.checked_sub(1))
                .ok_or("raw_composite_conflict")?;
            let source_start = part
                .source_offset
                .checked_add(part_offset)
                .ok_or("raw_composite_conflict")?;
            let source_end = part
                .source_offset
                .checked_add(part_end)
                .ok_or("raw_composite_conflict")?;
            let bytes = read_part(part, source_start, source_end)?;
            if bytes.len()
                != usize::try_from(part_length).map_err(|_| "invalid_raw_composite_range")?
            {
                return Err("raw_composite_conflict");
            }
            output.extend_from_slice(&bytes);
            logical = logical
                .checked_add(part_length)
                .ok_or("raw_composite_conflict")?;
        }
        if output.len() != usize::try_from(length).map_err(|_| "invalid_raw_composite_range")? {
            return Err("raw_composite_conflict");
        }
        Ok(output)
    }
}

struct RegisteredComposite {
    source: Arc<RawCompositeSource>,
    retained_metadata_bytes: usize,
    readers: ReaderLeases,
    created_at: Instant,
    last_access: Instant,
}

pub struct RawCompositeLease {
    pub source: Arc<RawCompositeSource>,
    pub prefetch_generation: Option<ReaderGeneration>,
    _permit: ReaderPermit,
}

pub struct RawCompositeRegistry {
    entries: HashMap<String, RegisteredComposite>,
    retained_metadata_bytes: usize,
    maximum_retained_metadata_bytes: usize,
}

impl RawCompositeRegistry {
    pub fn new(maximum_retained_metadata_bytes: usize) -> Self {
        Self {
            entries: HashMap::new(),
            retained_metadata_bytes: 0,
            maximum_retained_metadata_bytes,
        }
    }

    pub fn insert(
        &mut self,
        source: RawCompositeSource,
        now: Instant,
    ) -> Result<(String, u64), &'static str> {
        self.remove_expired(now);
        if let Some(existing) = self.entries.get_mut(source.identity()) {
            if existing.source.as_ref() != &source {
                return Err("raw_composite_conflict");
            }
            existing.last_access = now;
            return Ok((
                existing.source.identity().to_owned(),
                existing.source.exact_size(),
            ));
        }
        let retained_metadata_bytes = source.retained_metadata_bytes();
        if retained_metadata_bytes > self.maximum_retained_metadata_bytes {
            return Err("raw_composite_capacity");
        }
        while self.retained_metadata_bytes
            > self.maximum_retained_metadata_bytes - retained_metadata_bytes
        {
            let oldest = self
                .entries
                .iter()
                .filter(|(_, entry)| !entry.readers.is_busy())
                .min_by_key(|(_, entry)| entry.last_access)
                .map(|(identity, _)| identity.clone());
            if let Some(identity) = oldest {
                self.remove_entry(&identity);
            } else {
                return Err("raw_composite_capacity");
            }
        }
        let identity = source.identity().to_owned();
        let exact_size = source.exact_size();
        self.entries.insert(
            identity.clone(),
            RegisteredComposite {
                source: Arc::new(source),
                retained_metadata_bytes,
                readers: ReaderLeases::new(MAX_COMPOSITE_READERS),
                created_at: now,
                last_access: now,
            },
        );
        self.retained_metadata_bytes = self
            .retained_metadata_bytes
            .checked_add(retained_metadata_bytes)
            .expect("validated raw composite metadata accounting");
        Ok((identity, exact_size))
    }

    pub fn get(&mut self, identity: &str, now: Instant) -> Result<RawCompositeLease, &'static str> {
        self.remove_expired(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("raw_composite_unavailable")?;
        if expired(entry, now) {
            return Err("raw_composite_busy");
        }
        let permit = entry
            .readers
            .acquire_transient()
            .map_err(raw_reader_error)?;
        entry.last_access = now;
        Ok(RawCompositeLease {
            source: Arc::clone(&entry.source),
            prefetch_generation: None,
            _permit: permit,
        })
    }

    pub fn exact_size(&mut self, identity: &str, now: Instant) -> Option<u64> {
        self.remove_expired(now);
        let entry = self.entries.get_mut(identity)?;
        entry.last_access = now;
        Some(entry.source.exact_size())
    }

    pub fn open_reader(&mut self, identity: &str, now: Instant) -> Result<String, &'static str> {
        self.remove_expired(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("raw_composite_unavailable")?;
        if expired(entry, now) {
            return Err("raw_composite_busy");
        }
        let lease_id = entry.readers.open().map_err(raw_reader_error)?;
        entry.last_access = now;
        Ok(lease_id)
    }

    pub fn get_with_reader(
        &mut self,
        identity: &str,
        lease_id: &str,
        now: Instant,
    ) -> Result<RawCompositeLease, &'static str> {
        self.remove_expired(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("raw_composite_unavailable")?;
        let permit = entry.readers.acquire(lease_id).map_err(raw_reader_error)?;
        let prefetch_generation = permit.generation();
        entry.last_access = now;
        Ok(RawCompositeLease {
            source: Arc::clone(&entry.source),
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
        self.remove_expired(now);
        let entry = self
            .entries
            .get_mut(identity)
            .ok_or("raw_composite_unavailable")?;
        entry.readers.close(lease_id).map_err(raw_reader_error)
    }

    #[cfg(test)]
    pub fn remove(&mut self, identity: &str, now: Instant) -> Result<(), &'static str> {
        self.remove_expired(now);
        let entry = self
            .entries
            .get(identity)
            .ok_or("raw_composite_unavailable")?;
        if entry.readers.is_busy() {
            return Err("raw_composite_busy");
        }
        self.remove_entry(identity);
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    fn remove_expired(&mut self, now: Instant) {
        let retained_metadata_bytes = &mut self.retained_metadata_bytes;
        self.entries.retain(|_, entry| {
            let keep = entry.readers.is_busy() || !expired(entry, now);
            if !keep {
                *retained_metadata_bytes = retained_metadata_bytes
                    .checked_sub(entry.retained_metadata_bytes)
                    .expect("raw composite metadata accounting");
            }
            keep
        });
    }

    fn remove_entry(&mut self, identity: &str) {
        let entry = self
            .entries
            .remove(identity)
            .expect("registered raw composite");
        self.retained_metadata_bytes = self
            .retained_metadata_bytes
            .checked_sub(entry.retained_metadata_bytes)
            .expect("raw composite metadata accounting");
    }
}

fn raw_reader_error(error: ReaderLeaseError) -> &'static str {
    match error {
        ReaderLeaseError::Busy => "raw_composite_reader_busy",
        ReaderLeaseError::Capacity => "raw_composite_reader_capacity",
        ReaderLeaseError::RandomUnavailable => "raw_composite_random_unavailable",
        ReaderLeaseError::Unavailable => "raw_composite_reader_unavailable",
    }
}

fn expired(entry: &RegisteredComposite, now: Instant) -> bool {
    !entry.readers.is_busy()
        && (now.duration_since(entry.last_access) >= COMPOSITE_IDLE_TTL
            || now.duration_since(entry.created_at) >= COMPOSITE_ABSOLUTE_TTL)
}

#[cfg(test)]
mod tests {
    use super::{RawCompositePart, RawCompositeRegistry, RawCompositeSource};
    use crate::archive_group::{PlannedVolume, VolumePlan, VolumePlanKind};
    use crate::materialization::ImmutableFileIdentity;
    use std::collections::BTreeMap;
    use std::time::{Duration, Instant};

    fn source() -> RawCompositeSource {
        let volumes = vec![
            PlannedVolume {
                content_identity: "a".repeat(64),
                relative_path: "movie.mkv.001".to_owned(),
                number: 0,
                exact_size: 3,
            },
            PlannedVolume {
                content_identity: "b".repeat(64),
                relative_path: "movie.mkv.002".to_owned(),
                number: 1,
                exact_size: 4,
            },
            PlannedVolume {
                content_identity: "c".repeat(64),
                relative_path: "movie.mkv.003".to_owned(),
                number: 2,
                exact_size: 2,
            },
        ];
        let file_identities = volumes
            .iter()
            .enumerate()
            .map(|(index, volume)| {
                (
                    volume.content_identity.clone(),
                    ImmutableFileIdentity {
                        device: 1,
                        inode: u64::try_from(index + 1).unwrap(),
                        size: volume.exact_size,
                        mode: 0o100400,
                        links: 1,
                        modified_seconds: 1,
                        modified_nanoseconds: 0,
                        changed_seconds: 1,
                        changed_nanoseconds: 0,
                    },
                )
            })
            .collect::<BTreeMap<_, _>>();
        RawCompositeSource::from_plan(
            VolumePlan {
                set_identity: "f".repeat(64),
                kind: VolumePlanKind::RawSplit,
                exact_size: 9,
                volumes,
            },
            file_identities,
        )
        .expect("raw composite source")
    }

    #[test]
    fn maps_exact_ranges_across_part_boundaries_without_a_composite_copy() {
        let source = source();
        let mut reads = Vec::new();

        let bytes = source
            .read_at(2, 6, &|| false, |part, start, end| {
                reads.push((part.content_identity.clone(), start, end));
                let byte = match part.content_identity.as_bytes()[0] {
                    b'a' => b'A',
                    b'b' => b'B',
                    b'c' => b'C',
                    _ => unreachable!(),
                };
                Ok(vec![byte; usize::try_from(end - start + 1).unwrap()])
            })
            .expect("cross-part range");

        assert_eq!(bytes, b"ABBBBC");
        assert_eq!(
            reads,
            vec![
                ("a".repeat(64), 2, 2),
                ("b".repeat(64), 0, 3),
                ("c".repeat(64), 0, 0),
            ]
        );
        assert_eq!(
            source.read_at(9, 1, &|| false, |_, _, _| Ok(Vec::new())),
            Err("invalid_raw_composite_range")
        );
        assert_eq!(
            source.read_at(0, 1, &|| true, |_, _, _| Ok(Vec::new())),
            Err("raw_composite_cancelled")
        );
    }

    #[test]
    fn maps_one_verified_materialization_without_rehashing_each_range() {
        let identity = "a".repeat(64);
        let source = RawCompositeSource::from_materialization(
            identity.clone(),
            6,
            ImmutableFileIdentity {
                device: 1,
                inode: 2,
                size: 6,
                mode: 0o100400,
                links: 1,
                modified_seconds: 1,
                modified_nanoseconds: 0,
                changed_seconds: 1,
                changed_nanoseconds: 0,
            },
        )
        .expect("single immutable source");

        let bytes = source
            .read_at(2, 3, &|| false, |part, start, end| {
                assert_eq!(part.content_identity, identity);
                assert_eq!((start, end), (2, 4));
                Ok(b"CDE".to_vec())
            })
            .expect("read immutable source");

        assert_eq!(bytes, b"CDE");
    }

    #[test]
    fn maps_bounded_ranges_inside_verified_materializations() {
        let source = RawCompositeSource::from_ranges(
            "f".repeat(64),
            vec![
                RawCompositePart {
                    content_identity: "a".repeat(64),
                    source_offset: 10,
                    exact_size: 3,
                    backing: super::RawCompositeBacking::Materialization(ImmutableFileIdentity {
                        device: 1,
                        inode: 2,
                        size: 20,
                        mode: 0o100400,
                        links: 1,
                        modified_seconds: 1,
                        modified_nanoseconds: 0,
                        changed_seconds: 1,
                        changed_nanoseconds: 0,
                    }),
                },
                RawCompositePart {
                    content_identity: "b".repeat(64),
                    source_offset: 20,
                    exact_size: 4,
                    backing: super::RawCompositeBacking::Materialization(ImmutableFileIdentity {
                        device: 1,
                        inode: 3,
                        size: 30,
                        mode: 0o100400,
                        links: 1,
                        modified_seconds: 1,
                        modified_nanoseconds: 0,
                        changed_seconds: 1,
                        changed_nanoseconds: 0,
                    }),
                },
            ],
        )
        .expect("bounded composite");
        let mut reads = Vec::new();

        let bytes = source
            .read_at(2, 4, &|| false, |part, start, end| {
                reads.push((part.content_identity.clone(), start, end));
                Ok(vec![b'X'; usize::try_from(end - start + 1).unwrap()])
            })
            .expect("range crossing materializations");

        assert_eq!(bytes, b"XXXX");
        assert_eq!(
            reads,
            vec![("a".repeat(64), 12, 12), ("b".repeat(64), 20, 22)]
        );
    }

    #[test]
    fn registry_is_idempotent_bounded_and_reader_leased() {
        let now = Instant::now();
        let mut registry = RawCompositeRegistry::new(super::MIN_COMPOSITE_METADATA_BUDGET_BYTES);
        assert_eq!(registry.insert(source(), now), Ok(("f".repeat(64), 9)));
        assert_eq!(registry.insert(source(), now), Ok(("f".repeat(64), 9)));
        let leases = (0..8)
            .map(|_| registry.get(&"f".repeat(64), now).expect("reader lease"))
            .collect::<Vec<_>>();
        assert_eq!(
            registry.get(&"f".repeat(64), now).err(),
            Some("raw_composite_reader_capacity")
        );
        assert_eq!(
            registry.remove(&"f".repeat(64), now),
            Err("raw_composite_busy")
        );
        drop(leases);
        registry
            .remove(&"f".repeat(64), now)
            .expect("remove idle composite");
        registry.insert(source(), now).expect("reinsert composite");
        let later = now + Duration::from_secs(6 * 60 * 60);
        assert!(registry.get(&"f".repeat(64), later).is_err());
        assert_eq!(registry.len(), 0);
    }

    #[test]
    fn persistent_reader_holds_the_composite_between_chunk_requests() {
        let identity = "f".repeat(64);
        let now = Instant::now();
        let mut registry = RawCompositeRegistry::new(super::MIN_COMPOSITE_METADATA_BUDGET_BYTES);
        registry.insert(source(), now).expect("insert composite");
        let reader = registry
            .open_reader(&identity, now)
            .expect("open persistent reader");
        let lease = registry
            .get_with_reader(&identity, &reader, now)
            .expect("acquire persistent reader");

        assert_eq!(
            registry.get_with_reader(&identity, &reader, now).err(),
            Some("raw_composite_reader_busy")
        );
        assert_eq!(registry.close_reader(&identity, &reader, now), Ok(()));
        assert_eq!(
            registry.get_with_reader(&identity, &reader, now).err(),
            Some("raw_composite_reader_unavailable")
        );
        assert_eq!(registry.remove(&identity, now), Ok(()));
        drop(lease);
    }

    #[test]
    fn persistent_reader_keeps_composite_available_past_ttl() {
        let identity = "f".repeat(64);
        let now = Instant::now();
        let mut registry = RawCompositeRegistry::new(super::MIN_COMPOSITE_METADATA_BUDGET_BYTES);
        registry.insert(source(), now).expect("insert composite");
        let reader = registry
            .open_reader(&identity, now)
            .expect("open persistent reader");
        let lease = registry
            .get_with_reader(&identity, &reader, now)
            .expect("acquire persistent reader");
        drop(lease);

        let additional_reader = registry
            .open_reader(&identity, now + super::COMPOSITE_IDLE_TTL)
            .expect("persistent reader keeps the composite live");

        assert_eq!(
            registry.close_reader(
                &identity,
                &additional_reader,
                now + super::COMPOSITE_IDLE_TTL,
            ),
            Ok(())
        );
        assert_eq!(
            registry.close_reader(&identity, &reader, now + super::COMPOSITE_IDLE_TTL),
            Ok(())
        );
    }
}
