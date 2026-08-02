use crate::limits;
use crate::yenc::DecodedPart;
use sha2::{Digest, Sha256};
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap, VecDeque};
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

mod checkpoint;
mod confined;
mod disk;
mod singleflight;

pub use checkpoint::SessionCheckpointStore;
pub use disk::{DiskCacheStats, DiskSegmentCache};
pub use singleflight::NetworkSingleflight;
pub(crate) use singleflight::{FlightCancellation, FlightPriority, NetworkFlightWaiter};

const MAX_VERIFIED_SEGMENT_BYTES: usize = limits::MAX_DECLARED_POSTING_BYTES as usize;
// CacheNode, the hash-table entry, Arc control blocks, allocator metadata and
// spare vector/hash capacity remain resident alongside each payload.
const SEGMENT_CACHE_ENTRY_OVERHEAD: usize = 512;
const NEGATIVE_CACHE_DEFAULT_CAPACITY: usize = 500_000;
const NEGATIVE_CACHE_HARD_CAPACITY: usize = 2_000_000;
const MISSING_TTL: Duration = Duration::from_secs(10 * 60);
const CORRUPT_TTL: Duration = Duration::from_secs(5 * 60);

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct SegmentCacheKey([u8; 32]);

impl SegmentCacheKey {
    pub fn acquisition(
        account_partition: [u8; 32],
        provider_set_generation: &str,
        provider_configuration_id: &str,
        canonical_bare_message_id: &str,
    ) -> Self {
        let mut payload = Vec::with_capacity(
            16 + provider_set_generation.len()
                + provider_configuration_id.len()
                + canonical_bare_message_id.len(),
        );
        payload.push(0x84);
        encode_cbor_text(&mut payload, "article-v2");
        encode_cbor_text(&mut payload, provider_set_generation);
        encode_cbor_text(&mut payload, provider_configuration_id);
        encode_cbor_text(&mut payload, canonical_bare_message_id);
        Self(hmac_sha256(&account_partition, &payload))
    }

    #[cfg(test)]
    fn bytes(self) -> [u8; 32] {
        self.0
    }
}

fn encode_cbor_text(output: &mut Vec<u8>, value: &str) {
    let length = value.len() as u64;
    match length {
        0..=23 => output.push(0x60 | length as u8),
        24..=0xff => {
            output.push(0x78);
            output.push(length as u8);
        }
        0x100..=0xffff => {
            output.push(0x79);
            output.extend_from_slice(&(length as u16).to_be_bytes());
        }
        0x1_0000..=0xffff_ffff => {
            output.push(0x7a);
            output.extend_from_slice(&(length as u32).to_be_bytes());
        }
        _ => {
            output.push(0x7b);
            output.extend_from_slice(&length.to_be_bytes());
        }
    }
    output.extend_from_slice(value.as_bytes());
}

fn hmac_sha256(key: &[u8], message: &[u8]) -> [u8; 32] {
    const BLOCK_SIZE: usize = 64;
    let mut block = [0u8; BLOCK_SIZE];
    if key.len() > BLOCK_SIZE {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner_pad = [0x36u8; BLOCK_SIZE];
    let mut outer_pad = [0x5cu8; BLOCK_SIZE];
    for index in 0..BLOCK_SIZE {
        inner_pad[index] ^= block[index];
        outer_pad[index] ^= block[index];
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(message);
    let inner = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner);
    outer.finalize().into()
}

#[derive(Clone, Debug)]
pub struct VerifiedSegment {
    bytes: Arc<[u8]>,
    pub begin: u64,
    pub end: u64,
    pub total_size: u64,
    pub part_crc32: u32,
    pub whole_crc32: Option<u32>,
}

impl VerifiedSegment {
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }

    pub fn from_decoded(part: DecodedPart) -> Result<Self, &'static str> {
        let part_crc32 = part.expected_crc32.ok_or("segment_integrity_unverified")?;
        let byte_size = part.bytes.len() as u64;
        if part.bytes.is_empty()
            || part.bytes.len() > MAX_VERIFIED_SEGMENT_BYTES
            || part.begin == 0
            || part.end < part.begin
            || part.end > part.total_size
            || part.end - part.begin + 1 != byte_size
        {
            return Err("invalid_verified_segment");
        }
        Ok(Self {
            bytes: part.bytes.into(),
            begin: part.begin,
            end: part.end,
            total_size: part.total_size,
            part_crc32,
            whole_crc32: part.expected_whole_crc32,
        })
    }

    #[cfg(test)]
    pub fn to_decoded(&self) -> DecodedPart {
        DecodedPart {
            bytes: self.bytes.to_vec(),
            begin: self.begin,
            end: self.end,
            total_size: self.total_size,
            expected_crc32: Some(self.part_crc32),
            expected_whole_crc32: self.whole_crc32,
        }
    }

    pub fn read_logical_range_into(
        &self,
        begin: u64,
        end: u64,
        output: &mut Vec<u8>,
    ) -> Result<(), &'static str> {
        if begin < self.begin || end < begin || end > self.end {
            return Err("segment_range_unavailable");
        }
        let start = (begin - self.begin) as usize;
        let finish = (end - self.begin + 1) as usize;
        output.extend_from_slice(&self.bytes[start..finish]);
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Queue {
    Probationary = 0,
    Protected = 1,
}

struct CacheNode {
    key: SegmentCacheKey,
    segment: Arc<VerifiedSegment>,
    pins: Arc<AtomicUsize>,
    weight: usize,
    accessed: bool,
    queue: Queue,
    previous: Option<usize>,
    next: Option<usize>,
}

pub struct SegmentLease {
    segment: Arc<VerifiedSegment>,
    pins: Arc<AtomicUsize>,
}

impl SegmentLease {
    pub fn segment(&self) -> &VerifiedSegment {
        &self.segment
    }

    pub fn detached(segment: Arc<VerifiedSegment>) -> Self {
        Self {
            segment,
            pins: Arc::new(AtomicUsize::new(1)),
        }
    }
}

impl Clone for SegmentLease {
    fn clone(&self) -> Self {
        self.pins.fetch_add(1, Ordering::Relaxed);
        Self {
            segment: Arc::clone(&self.segment),
            pins: Arc::clone(&self.pins),
        }
    }
}

impl Drop for SegmentLease {
    fn drop(&mut self) {
        let previous = self.pins.fetch_sub(1, Ordering::Release);
        debug_assert!(previous > 0);
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Admission {
    Admitted,
    Replaced,
    Bypassed,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SegmentCacheStats {
    pub entries: usize,
    pub used_bytes: usize,
    pub probationary_bytes: usize,
    pub protected_bytes: usize,
}

pub struct VerifiedSegmentCache {
    budget: usize,
    used: usize,
    queue_weights: [usize; 2],
    heads: [Option<usize>; 2],
    tails: [Option<usize>; 2],
    nodes: Vec<Option<CacheNode>>,
    free: Vec<usize>,
    entries: HashMap<SegmentCacheKey, usize>,
}

impl VerifiedSegmentCache {
    pub fn new(budget: usize) -> Result<Self, &'static str> {
        if budget < 8 {
            return Err("segment_cache_budget_too_small");
        }
        Ok(Self {
            budget,
            used: 0,
            queue_weights: [0, 0],
            heads: [None, None],
            tails: [None, None],
            nodes: Vec::new(),
            free: Vec::new(),
            entries: HashMap::new(),
        })
    }

    pub fn budget(&self) -> usize {
        self.budget
    }

    pub fn get(&mut self, key: SegmentCacheKey) -> Option<SegmentLease> {
        let index = *self.entries.get(&key)?;
        let (queue, promote) = {
            let node = self.node_mut(index);
            let promote = node.queue == Queue::Probationary && node.accessed;
            node.accessed = true;
            (node.queue, promote)
        };
        if promote {
            self.move_to_queue_tail(index, Queue::Protected);
            self.rebalance_protected();
        } else {
            self.move_to_queue_tail(index, queue);
        }
        let node = self.node(index);
        node.pins.fetch_add(1, Ordering::Relaxed);
        Some(SegmentLease {
            segment: Arc::clone(&node.segment),
            pins: Arc::clone(&node.pins),
        })
    }

    pub fn insert(&mut self, key: SegmentCacheKey, segment: VerifiedSegment) -> Admission {
        let weight = segment.bytes.len() + SEGMENT_CACHE_ENTRY_OVERHEAD;
        if weight > self.budget / 8 {
            return Admission::Bypassed;
        }
        let replaced = self.entries.get(&key).copied();
        if replaced.is_some_and(|index| self.node(index).pins.load(Ordering::Acquire) != 0) {
            return Admission::Bypassed;
        }
        let replaced_weight = replaced.map_or(0, |index| self.node(index).weight);
        let projected = self.used - replaced_weight + weight;
        let required = if projected > self.budget {
            let target = self.budget / 10 * 9 + self.budget % 10 * 9 / 10;
            projected - target
        } else {
            0
        };
        let Some(victims) = self.select_victims(required, replaced) else {
            return Admission::Bypassed;
        };
        for victim in victims {
            self.remove(victim);
        }
        if let Some(index) = replaced {
            self.remove(index);
        }
        let node = CacheNode {
            key,
            segment: Arc::new(segment),
            pins: Arc::new(AtomicUsize::new(0)),
            weight,
            accessed: false,
            queue: Queue::Probationary,
            previous: None,
            next: None,
        };
        let index = self.allocate(node);
        self.entries.insert(key, index);
        self.attach_tail(index, Queue::Probationary);
        self.used += weight;
        self.queue_weights[Queue::Probationary as usize] += weight;
        self.assert_invariants();
        if replaced.is_some() {
            Admission::Replaced
        } else {
            Admission::Admitted
        }
    }

    pub fn purge(&mut self) {
        for index in 0..self.nodes.len() {
            if self.nodes[index]
                .as_ref()
                .is_some_and(|node| node.pins.load(Ordering::Acquire) == 0)
            {
                self.remove(index);
            }
        }
        self.assert_invariants();
    }

    pub fn stats(&self) -> SegmentCacheStats {
        SegmentCacheStats {
            entries: self.entries.len(),
            used_bytes: self.used,
            probationary_bytes: self.queue_weights[Queue::Probationary as usize],
            protected_bytes: self.queue_weights[Queue::Protected as usize],
        }
    }

    fn select_victims(&self, required: usize, excluded: Option<usize>) -> Option<Vec<usize>> {
        if required == 0 {
            return Some(Vec::new());
        }
        let minimum_weight = SEGMENT_CACHE_ENTRY_OVERHEAD + 1;
        let maximum_victims = required.div_ceil(minimum_weight).min(self.entries.len());
        let mut victims = Vec::with_capacity(maximum_victims);
        let mut recovered = 0usize;
        for queue in [Queue::Probationary, Queue::Protected] {
            let mut current = self.heads[queue as usize];
            while let Some(index) = current {
                let node = self.node(index);
                current = node.next;
                if Some(index) == excluded || node.pins.load(Ordering::Acquire) != 0 {
                    continue;
                }
                victims.push(index);
                recovered += node.weight;
                if recovered >= required {
                    return Some(victims);
                }
            }
        }
        None
    }

    fn rebalance_protected(&mut self) {
        let maximum = self.budget / 4 * 3 + self.budget % 4 * 3 / 4;
        while self.queue_weights[Queue::Protected as usize] > maximum {
            let Some(index) = self.heads[Queue::Protected as usize] else {
                break;
            };
            self.move_to_queue_tail(index, Queue::Probationary);
        }
        self.assert_invariants();
    }

    fn move_to_queue_tail(&mut self, index: usize, queue: Queue) {
        let old_queue = self.node(index).queue;
        let weight = self.node(index).weight;
        self.detach(index);
        if old_queue != queue {
            self.queue_weights[old_queue as usize] -= weight;
            self.queue_weights[queue as usize] += weight;
            self.node_mut(index).queue = queue;
        }
        self.attach_tail(index, queue);
    }

    fn detach(&mut self, index: usize) {
        let queue = self.node(index).queue;
        let previous = self.node(index).previous;
        let next = self.node(index).next;
        if let Some(previous) = previous {
            self.node_mut(previous).next = next;
        } else {
            self.heads[queue as usize] = next;
        }
        if let Some(next) = next {
            self.node_mut(next).previous = previous;
        } else {
            self.tails[queue as usize] = previous;
        }
        let node = self.node_mut(index);
        node.previous = None;
        node.next = None;
    }

    fn attach_tail(&mut self, index: usize, queue: Queue) {
        let tail = self.tails[queue as usize];
        if let Some(tail) = tail {
            self.node_mut(tail).next = Some(index);
        } else {
            self.heads[queue as usize] = Some(index);
        }
        let node = self.node_mut(index);
        node.previous = tail;
        node.next = None;
        self.tails[queue as usize] = Some(index);
    }

    fn allocate(&mut self, node: CacheNode) -> usize {
        if let Some(index) = self.free.pop() {
            self.nodes[index] = Some(node);
            index
        } else {
            let index = self.nodes.len();
            self.nodes.push(Some(node));
            index
        }
    }

    fn remove(&mut self, index: usize) {
        self.detach(index);
        let node = self.nodes[index].take().expect("cache node exists");
        self.entries.remove(&node.key);
        self.used -= node.weight;
        self.queue_weights[node.queue as usize] -= node.weight;
        self.free.push(index);
    }

    fn node(&self, index: usize) -> &CacheNode {
        self.nodes[index].as_ref().expect("cache node exists")
    }

    fn node_mut(&mut self, index: usize) -> &mut CacheNode {
        self.nodes[index].as_mut().expect("cache node exists")
    }

    fn assert_invariants(&self) {
        debug_assert!(self.used <= self.budget);
        debug_assert_eq!(
            self.used,
            self.nodes
                .iter()
                .filter_map(Option::as_ref)
                .map(|node| node.weight)
                .sum::<usize>()
        );
        debug_assert_eq!(
            self.used,
            self.queue_weights[Queue::Probationary as usize]
                + self.queue_weights[Queue::Protected as usize]
        );
        debug_assert_eq!(self.entries.len(), self.nodes.iter().flatten().count());
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NegativeKind {
    Missing,
    Corrupt,
}

#[derive(Clone, Copy)]
struct NegativeRecord {
    kind: NegativeKind,
    expires_at: Instant,
    sequence: u64,
}

pub struct NegativeSegmentCache {
    capacity: usize,
    next_sequence: u64,
    records: HashMap<SegmentCacheKey, NegativeRecord>,
    order: VecDeque<(SegmentCacheKey, u64)>,
    expirations: BinaryHeap<Reverse<(Instant, u64, SegmentCacheKey)>>,
}

impl NegativeSegmentCache {
    pub fn new(capacity: usize) -> Result<Self, &'static str> {
        if capacity == 0 || capacity > NEGATIVE_CACHE_HARD_CAPACITY {
            return Err("invalid_negative_cache_capacity");
        }
        Ok(Self {
            capacity,
            next_sequence: 0,
            records: HashMap::new(),
            order: VecDeque::new(),
            expirations: BinaryHeap::new(),
        })
    }

    pub fn with_default_capacity() -> Self {
        Self::new(NEGATIVE_CACHE_DEFAULT_CAPACITY).expect("valid default negative cache capacity")
    }

    pub fn insert(&mut self, key: SegmentCacheKey, kind: NegativeKind, now: Instant) {
        self.prune(now);
        self.next_sequence += 1;
        let sequence = self.next_sequence;
        let ttl = match kind {
            NegativeKind::Missing => MISSING_TTL,
            NegativeKind::Corrupt => CORRUPT_TTL,
        };
        let expires_at = now
            .checked_add(ttl)
            .expect("negative cache TTL fits the monotonic clock");
        self.records.insert(
            key,
            NegativeRecord {
                kind,
                expires_at,
                sequence,
            },
        );
        self.order.push_back((key, sequence));
        self.expirations.push(Reverse((expires_at, sequence, key)));
        self.enforce_capacity();
        self.compact_auxiliary_indexes();
    }

    pub fn get(&mut self, key: SegmentCacheKey, now: Instant) -> Option<NegativeKind> {
        self.prune(now);
        self.records.get(&key).map(|record| record.kind)
    }

    pub fn record_success(&mut self, key: SegmentCacheKey) {
        self.records.remove(&key);
        self.compact_auxiliary_indexes();
    }

    pub fn len(&self) -> usize {
        self.records.len()
    }

    fn prune(&mut self, now: Instant) {
        while let Some(Reverse((expires_at, sequence, key))) = self.expirations.peek().copied() {
            if expires_at > now {
                break;
            }
            self.expirations.pop();
            if self
                .records
                .get(&key)
                .is_some_and(|record| record.sequence == sequence)
            {
                self.records.remove(&key);
            }
        }
    }

    fn enforce_capacity(&mut self) {
        while self.records.len() > self.capacity {
            let (key, sequence) = self.order.pop_front().expect("cache insertion is ordered");
            if self
                .records
                .get(&key)
                .is_some_and(|record| record.sequence == sequence)
            {
                self.records.remove(&key);
            }
        }
    }

    fn compact_auxiliary_indexes(&mut self) {
        let maximum = self.capacity * 2;
        if self.order.len() <= maximum && self.expirations.len() <= maximum {
            return;
        }
        let mut ordered = self
            .records
            .iter()
            .map(|(key, record)| (record.sequence, *key))
            .collect::<Vec<_>>();
        ordered.sort_unstable();
        self.order = ordered
            .into_iter()
            .map(|(sequence, key)| (key, sequence))
            .collect();
        self.expirations = self
            .records
            .iter()
            .map(|(key, record)| Reverse((record.expires_at, record.sequence, *key)))
            .collect();
    }
}

#[cfg(test)]
mod tests {
    use super::{
        Admission, NegativeKind, NegativeSegmentCache, SEGMENT_CACHE_ENTRY_OVERHEAD,
        SegmentCacheKey, VerifiedSegment, VerifiedSegmentCache,
    };
    use crate::yenc::DecodedPart;
    use std::time::{Duration, Instant};

    fn key(value: u8) -> SegmentCacheKey {
        SegmentCacheKey([value; 32])
    }

    fn segment(value: u8, length: usize) -> VerifiedSegment {
        VerifiedSegment::from_decoded(DecodedPart {
            bytes: vec![value; length],
            begin: 1,
            end: length as u64,
            total_size: length as u64,
            expected_crc32: Some(u32::from(value)),
            expected_whole_crc32: Some(u32::from(value)),
        })
        .unwrap()
    }

    fn cache_budget(payload_bytes: usize) -> usize {
        (payload_bytes + SEGMENT_CACHE_ENTRY_OVERHEAD) * 8
    }

    #[test]
    fn derives_partitioned_deterministic_acquisition_keys() {
        let first =
            SegmentCacheKey::acquisition([7; 32], "generation", "primary", "id@example.test");
        let same =
            SegmentCacheKey::acquisition([7; 32], "generation", "primary", "id@example.test");

        assert_eq!(first, same);
        assert_ne!(
            first,
            SegmentCacheKey::acquisition([8; 32], "generation", "primary", "id@example.test")
        );
        assert_ne!(
            first,
            SegmentCacheKey::acquisition([7; 32], "other", "primary", "id@example.test")
        );
        assert_ne!(
            first,
            SegmentCacheKey::acquisition([7; 32], "generation", "backup", "id@example.test")
        );
        assert_ne!(
            first,
            SegmentCacheKey::acquisition([7; 32], "generation", "primary", "other@example.test")
        );
        assert_eq!(
            first
                .bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>(),
            "ff28c5eabd643dd3add2770aa00eae9aff0e0b0ffc8b00db37b76509aa9fc428"
        );
    }

    #[test]
    fn verified_segments_keep_exact_ranges_at_the_logical_limit() {
        let segment = VerifiedSegment::from_decoded(DecodedPart {
            bytes: vec![7],
            begin: u64::MAX,
            end: u64::MAX,
            total_size: u64::MAX,
            expected_crc32: Some(7),
            expected_whole_crc32: None,
        })
        .unwrap();
        let mut output = Vec::new();
        segment
            .read_logical_range_into(u64::MAX, u64::MAX, &mut output)
            .unwrap();
        assert_eq!(output, [7]);
        assert_eq!(
            VerifiedSegment::from_decoded(DecodedPart {
                bytes: vec![7],
                begin: 0,
                end: u64::MAX,
                total_size: u64::MAX,
                expected_crc32: Some(7),
                expected_whole_crc32: None,
            })
            .unwrap_err(),
            "invalid_verified_segment"
        );
    }

    #[test]
    fn promotes_on_the_second_hit_and_evicts_probationary_entries_first() {
        let weight = 10 + SEGMENT_CACHE_ENTRY_OVERHEAD;
        let mut cache = VerifiedSegmentCache::new(cache_budget(10)).unwrap();
        for value in 1..=8 {
            assert_eq!(
                cache.insert(key(value), segment(value, 10)),
                Admission::Admitted
            );
        }
        drop(cache.get(key(1)).unwrap());
        drop(cache.get(key(1)).unwrap());
        drop(cache.get(key(2)).unwrap());
        drop(cache.get(key(2)).unwrap());

        assert_eq!(cache.insert(key(9), segment(9, 10)), Admission::Admitted);
        assert!(cache.get(key(1)).is_some());
        assert!(cache.get(key(2)).is_some());
        assert!(cache.get(key(3)).is_none());
        assert!(cache.get(key(4)).is_none());
        assert_eq!(cache.stats().used_bytes, 7 * weight);
    }

    #[test]
    fn pinned_entries_make_admission_bypass_without_changing_accounting() {
        let mut cache = VerifiedSegmentCache::new(cache_budget(10)).unwrap();
        for value in 1..=8 {
            cache.insert(key(value), segment(value, 10));
        }
        let leases = (1..=8)
            .map(|value| cache.get(key(value)).unwrap())
            .collect::<Vec<_>>();
        let before = cache.stats();

        assert_eq!(cache.insert(key(9), segment(9, 10)), Admission::Bypassed);
        assert_eq!(cache.stats(), before);
        assert!(cache.get(key(9)).is_none());
        drop(leases);
    }

    #[test]
    fn victim_selection_reserves_by_entry_count() {
        let payload_bytes = 8 * 1024 * 1024;
        let mut cache = VerifiedSegmentCache::new(cache_budget(payload_bytes)).unwrap();
        cache.insert(key(1), segment(1, payload_bytes));

        let victims = cache.select_victims(payload_bytes, None).unwrap();

        assert_eq!(victims, [0]);
        assert_eq!(victims.capacity(), 1);
    }

    #[test]
    fn failed_replacement_restores_the_old_entry_exactly() {
        let budget =
            2 * (10 + SEGMENT_CACHE_ENTRY_OVERHEAD) + 7 * (20 + SEGMENT_CACHE_ENTRY_OVERHEAD);
        let mut cache = VerifiedSegmentCache::new(budget).unwrap();
        cache.insert(key(1), segment(1, 10));
        for value in 2..=8 {
            cache.insert(key(value), segment(value, 20));
        }
        cache.insert(key(9), segment(9, 10));
        let leases = (2..=9)
            .map(|value| cache.get(key(value)).unwrap())
            .collect::<Vec<_>>();
        let before = cache.stats();

        assert_eq!(cache.insert(key(1), segment(99, 20)), Admission::Bypassed);
        assert_eq!(cache.stats(), before);
        assert_eq!(
            cache.get(key(1)).unwrap().segment().to_decoded().bytes[0],
            1
        );
        drop(leases);
    }

    #[test]
    fn replacement_and_purge_keep_capacity_equal_to_resident_weights() {
        let mut cache = VerifiedSegmentCache::new(cache_budget(20)).unwrap();
        assert_eq!(cache.insert(key(1), segment(1, 20)), Admission::Admitted);
        assert_eq!(cache.insert(key(1), segment(2, 10)), Admission::Replaced);
        assert_eq!(cache.stats().used_bytes, 10 + SEGMENT_CACHE_ENTRY_OVERHEAD);
        assert_eq!(
            cache.get(key(1)).unwrap().segment().to_decoded().bytes[0],
            2
        );

        cache.purge();
        assert_eq!(cache.stats().used_bytes, 0);
        assert_eq!(cache.stats().entries, 0);
    }

    #[test]
    fn replacing_a_pinned_entry_bypasses_and_keeps_the_old_value() {
        let mut cache = VerifiedSegmentCache::new(cache_budget(20)).unwrap();
        cache.insert(key(1), segment(1, 10));
        let lease = cache.get(key(1)).unwrap();
        let before = cache.stats();

        assert_eq!(cache.insert(key(1), segment(2, 10)), Admission::Bypassed);
        assert_eq!(cache.stats(), before);
        assert_eq!(lease.segment().to_decoded().bytes[0], 1);
        drop(lease);
        assert_eq!(
            cache.get(key(1)).unwrap().segment().to_decoded().bytes[0],
            1
        );
    }

    #[test]
    fn oversized_entries_bypass_each_tier_without_eviction() {
        let mut cache = VerifiedSegmentCache::new(cache_budget(20)).unwrap();
        cache.insert(key(1), segment(1, 20));
        let before = cache.stats();

        assert_eq!(cache.insert(key(2), segment(2, 21)), Admission::Bypassed);
        assert_eq!(cache.stats(), before);
    }

    #[test]
    fn tiny_segments_cannot_escape_the_resident_memory_budget() {
        let weight = 1 + SEGMENT_CACHE_ENTRY_OVERHEAD;
        let budget = weight * 8;
        let mut cache = VerifiedSegmentCache::new(budget).unwrap();
        for value in 1..=64 {
            assert_eq!(
                cache.insert(key(value), segment(value, 1)),
                Admission::Admitted
            );
            assert!(cache.stats().entries <= budget / weight);
            assert!(cache.stats().used_bytes <= budget);
        }
    }

    #[test]
    fn negative_entries_are_bounded_scoped_and_expire_by_kind() {
        let now = Instant::now();
        let mut cache = NegativeSegmentCache::new(2).unwrap();
        cache.insert(key(1), NegativeKind::Missing, now);
        cache.insert(key(2), NegativeKind::Corrupt, now);
        assert_eq!(cache.get(key(1), now), Some(NegativeKind::Missing));
        assert_eq!(cache.get(key(2), now), Some(NegativeKind::Corrupt));

        cache.insert(key(3), NegativeKind::Missing, now);
        assert_eq!(cache.len(), 2);
        assert_eq!(cache.get(key(1), now), None);
        assert_eq!(cache.get(key(2), now + Duration::from_secs(5 * 60)), None);
        assert_eq!(
            cache.get(key(3), now + Duration::from_secs(5 * 60)),
            Some(NegativeKind::Missing)
        );
        assert_eq!(cache.get(key(3), now + Duration::from_secs(10 * 60)), None);
    }

    #[test]
    fn success_removes_only_the_matching_negative() {
        let now = Instant::now();
        let mut cache = NegativeSegmentCache::with_default_capacity();
        cache.insert(key(1), NegativeKind::Missing, now);
        cache.insert(key(2), NegativeKind::Corrupt, now);

        cache.record_success(key(1));

        assert_eq!(cache.get(key(1), now), None);
        assert_eq!(cache.get(key(2), now), Some(NegativeKind::Corrupt));
    }

    #[test]
    fn successful_churn_keeps_negative_cache_indexes_bounded() {
        let now = Instant::now();
        let mut cache = NegativeSegmentCache::new(2).unwrap();
        for value in 0..20 {
            let cache_key = key(value);
            cache.insert(cache_key, NegativeKind::Missing, now);
            cache.record_success(cache_key);
        }

        assert_eq!(cache.len(), 0);
        assert!(cache.order.len() <= 4);
        assert!(cache.expirations.len() <= 4);
    }
}
