use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[derive(Debug)]
pub enum ReaderLeaseError {
    Busy,
    Capacity,
    RandomUnavailable,
    Unavailable,
}

struct PersistentReader {
    active: AtomicBool,
    closed: AtomicBool,
    generation: u64,
    generation_clock: Arc<AtomicU64>,
    prefetch_state: AtomicU64,
    prefetch_target: AtomicUsize,
    created_at: Instant,
    last_access: Mutex<Instant>,
}

enum Release {
    Persistent(Arc<PersistentReader>),
    Transient(Arc<AtomicUsize>),
}

pub struct ReaderPermit {
    release: Release,
}

#[derive(Clone)]
pub struct ReaderGeneration {
    reader: Arc<PersistentReader>,
}

pub struct ReaderPrefetchPermit {
    reader: Arc<PersistentReader>,
    released: bool,
}

pub struct ReaderLeases {
    readers: Arc<AtomicUsize>,
    persistent: HashMap<String, Arc<PersistentReader>>,
    generation_clock: Arc<AtomicU64>,
    maximum: usize,
    idle_ttl: Duration,
    absolute_ttl: Duration,
}

impl ReaderLeases {
    pub fn new(maximum: usize, idle_ttl: Duration, absolute_ttl: Duration) -> Self {
        Self {
            readers: Arc::new(AtomicUsize::new(0)),
            persistent: HashMap::new(),
            generation_clock: Arc::new(AtomicU64::new(0)),
            maximum,
            idle_ttl,
            absolute_ttl,
        }
    }

    pub fn acquire_transient(&self) -> Result<ReaderPermit, ReaderLeaseError> {
        increment(&self.readers, self.maximum)?;
        Ok(ReaderPermit {
            release: Release::Transient(Arc::clone(&self.readers)),
        })
    }

    pub fn open(&mut self, now: Instant) -> Result<String, ReaderLeaseError> {
        self.remove_expired(now);
        let lease_id = loop {
            let candidate = random_lease_id()?;
            if !self.persistent.contains_key(&candidate) {
                break candidate;
            }
        };
        increment(&self.readers, self.maximum)?;
        let generation = match self.generation_clock.fetch_update(
            Ordering::AcqRel,
            Ordering::Acquire,
            |generation| generation.checked_add(1),
        ) {
            Ok(generation) => generation + 1,
            Err(_) => {
                self.readers.fetch_sub(1, Ordering::AcqRel);
                return Err(ReaderLeaseError::Capacity);
            }
        };
        self.persistent.insert(
            lease_id.clone(),
            Arc::new(PersistentReader {
                active: AtomicBool::new(false),
                closed: AtomicBool::new(false),
                generation,
                generation_clock: Arc::clone(&self.generation_clock),
                prefetch_state: AtomicU64::new(0),
                prefetch_target: AtomicUsize::new(0),
                created_at: now,
                last_access: Mutex::new(now),
            }),
        );
        Ok(lease_id)
    }

    pub fn acquire(
        &mut self,
        lease_id: &str,
        now: Instant,
    ) -> Result<ReaderPermit, ReaderLeaseError> {
        self.remove_expired(now);
        let reader = Arc::clone(
            self.persistent
                .get(lease_id)
                .ok_or(ReaderLeaseError::Unavailable)?,
        );
        reader
            .active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| ReaderLeaseError::Busy)?;
        *reader.last_access.lock().expect("reader lease lock") = now;
        Ok(ReaderPermit {
            release: Release::Persistent(reader),
        })
    }

    pub fn close(&mut self, lease_id: &str, now: Instant) -> Result<(), ReaderLeaseError> {
        self.remove_expired(now);
        let reader = self
            .persistent
            .remove(lease_id)
            .ok_or(ReaderLeaseError::Unavailable)?;
        reader.closed.store(true, Ordering::Release);
        self.readers.fetch_sub(1, Ordering::AcqRel);
        Ok(())
    }

    pub fn is_busy(&self) -> bool {
        self.readers.load(Ordering::Acquire) != 0
    }

    pub fn remove_expired(&mut self, now: Instant) {
        self.persistent.retain(|_, reader| {
            let last_access = *reader.last_access.lock().expect("reader lease lock");
            let expired = now.duration_since(last_access) >= self.idle_ttl
                || now.duration_since(reader.created_at) >= self.absolute_ttl;
            if expired && !reader.active.load(Ordering::Acquire) {
                reader.closed.store(true, Ordering::Release);
                self.readers.fetch_sub(1, Ordering::AcqRel);
                false
            } else {
                true
            }
        });
    }
}

impl ReaderPermit {
    pub fn generation(&self) -> Option<ReaderGeneration> {
        match &self.release {
            Release::Persistent(reader) => Some(ReaderGeneration {
                reader: Arc::clone(reader),
            }),
            Release::Transient(_) => None,
        }
    }
}

impl ReaderGeneration {
    pub fn is_obsolete(&self) -> bool {
        self.reader.closed.load(Ordering::Acquire)
            || self.reader.generation_clock.load(Ordering::Acquire) != self.reader.generation
    }

    pub fn request_prefetch(&self, posting_index: usize) -> Option<ReaderPrefetchPermit> {
        if self.is_obsolete() {
            return None;
        }
        self.reader
            .prefetch_target
            .store(posting_index, Ordering::Release);
        let state = self
            .reader
            .prefetch_state
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |state| {
                state.checked_add(2)
            })
            .expect("reader prefetch revision overflow")
            + 2;
        if state & 1 != 0
            || self
                .reader
                .prefetch_state
                .compare_exchange(state, state | 1, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return None;
        }
        Some(ReaderPrefetchPermit {
            reader: Arc::clone(&self.reader),
            released: false,
        })
    }
}

impl ReaderPrefetchPermit {
    pub fn request(&self) -> (u64, usize) {
        (
            self.reader.prefetch_state.load(Ordering::Acquire) & !1,
            self.reader.prefetch_target.load(Ordering::Acquire),
        )
    }

    pub fn finish(&mut self, revision: u64) -> bool {
        if self
            .reader
            .prefetch_state
            .compare_exchange(revision | 1, revision, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return false;
        }
        self.released = true;
        true
    }
}

impl Drop for ReaderPrefetchPermit {
    fn drop(&mut self) {
        if !self.released {
            self.reader.prefetch_state.fetch_and(!1, Ordering::AcqRel);
        }
    }
}

impl Drop for ReaderPermit {
    fn drop(&mut self) {
        match &self.release {
            Release::Persistent(reader) => {
                reader.active.store(false, Ordering::Release);
            }
            Release::Transient(readers) => {
                readers.fetch_sub(1, Ordering::AcqRel);
            }
        }
    }
}

fn increment(readers: &AtomicUsize, maximum: usize) -> Result<(), ReaderLeaseError> {
    readers
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |readers| {
            (readers < maximum).then_some(readers + 1)
        })
        .map(drop)
        .map_err(|_| ReaderLeaseError::Capacity)
}

pub fn random_lease_id() -> Result<String, ReaderLeaseError> {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut random = [0_u8; 16];
    getrandom::fill(&mut random).map_err(|_| ReaderLeaseError::RandomUnavailable)?;
    let mut encoded = String::with_capacity(22);
    for chunk in random[..15].chunks_exact(3) {
        encoded.push(char::from(ALPHABET[usize::from(chunk[0] >> 2)]));
        encoded.push(char::from(
            ALPHABET[usize::from(((chunk[0] & 0x03) << 4) | (chunk[1] >> 4))],
        ));
        encoded.push(char::from(
            ALPHABET[usize::from(((chunk[1] & 0x0f) << 2) | (chunk[2] >> 6))],
        ));
        encoded.push(char::from(ALPHABET[usize::from(chunk[2] & 0x3f)]));
    }
    encoded.push(char::from(ALPHABET[usize::from(random[15] >> 2)]));
    encoded.push(char::from(ALPHABET[usize::from((random[15] & 0x03) << 4)]));
    Ok(encoded)
}

#[cfg(test)]
mod tests {
    use super::ReaderLeases;
    use std::time::{Duration, Instant};

    #[test]
    fn newer_readers_cancel_only_obsolete_prefetch_generations() {
        let now = Instant::now();
        let mut readers = ReaderLeases::new(8, Duration::from_secs(60), Duration::from_secs(600));
        let first = readers.open(now).unwrap();
        let first_permit = readers.acquire(&first, now).unwrap();
        let first_generation = first_permit.generation().unwrap();
        let first_prefetch = first_generation.request_prefetch(1).unwrap();
        drop(first_permit);

        let second = readers.open(now).unwrap();
        assert!(first_generation.is_obsolete());
        assert!(first_generation.request_prefetch(2).is_none());
        drop(first_prefetch);
        assert!(first_generation.request_prefetch(3).is_none());

        let second_permit = readers.acquire(&second, now).unwrap();
        let second_generation = second_permit.generation().unwrap();
        assert!(!second_generation.is_obsolete());
        assert!(second_generation.request_prefetch(1).is_some());
        drop(second_permit);
        readers.close(&second, now).unwrap();
        assert!(second_generation.is_obsolete());
    }

    #[test]
    fn rejected_reader_admission_does_not_cancel_the_current_generation() {
        let now = Instant::now();
        let mut readers = ReaderLeases::new(1, Duration::from_secs(60), Duration::from_secs(600));
        let first = readers.open(now).unwrap();
        let permit = readers.acquire(&first, now).unwrap();
        let generation = permit.generation().unwrap();
        drop(permit);

        assert!(readers.open(now).is_err());
        assert!(!generation.is_obsolete());
        assert!(generation.request_prefetch(1).is_some());
    }

    #[test]
    fn active_prefetch_observes_progress_without_losing_the_final_request() {
        let now = Instant::now();
        let mut readers = ReaderLeases::new(1, Duration::from_secs(60), Duration::from_secs(600));
        let lease_id = readers.open(now).unwrap();
        let permit = readers.acquire(&lease_id, now).unwrap();
        let generation = permit.generation().unwrap();
        let mut prefetch = generation.request_prefetch(1).unwrap();
        let (first_revision, first_target) = prefetch.request();
        assert_eq!(first_target, 1);

        assert!(generation.request_prefetch(7).is_none());
        assert!(!prefetch.finish(first_revision));
        let (latest_revision, latest_target) = prefetch.request();
        assert_eq!(latest_target, 7);
        assert!(prefetch.finish(latest_revision));
        assert!(generation.request_prefetch(8).is_some());
    }

    #[test]
    fn close_revokes_an_active_reader_without_waiting_for_its_request() {
        let now = Instant::now();
        let mut readers = ReaderLeases::new(1, Duration::from_secs(60), Duration::from_secs(600));
        let lease_id = readers.open(now).unwrap();
        let permit = readers.acquire(&lease_id, now).unwrap();
        let generation = permit.generation().unwrap();

        readers.close(&lease_id, now).unwrap();

        assert!(generation.is_obsolete());
        assert!(!readers.is_busy());
        assert!(readers.acquire(&lease_id, now).is_err());
        drop(permit);
    }
}
