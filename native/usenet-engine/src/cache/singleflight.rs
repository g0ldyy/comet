use super::{SegmentCacheKey, VerifiedSegment};
use std::collections::HashMap;
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const MAX_ACTIVE_FLIGHTS: usize = 64;
const MAX_WAITERS_PER_FLIGHT: usize = 1024;

type SharedResult = Result<Arc<VerifiedSegment>, &'static str>;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
#[repr(u8)]
pub enum FlightPriority {
    Interactive,
    Preparation,
    Background,
}

struct FlightState {
    waiters: usize,
    result: Option<SharedResult>,
    result_claimed: bool,
}

struct Flight {
    state: Mutex<FlightState>,
    ready: Condvar,
    cancellation: FlightCancellation,
}

struct Inner {
    flights: Mutex<HashMap<SegmentCacheKey, Arc<Flight>>>,
}

pub struct NetworkSingleflight {
    inner: Arc<Inner>,
}

impl NetworkSingleflight {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Inner {
                flights: Mutex::new(HashMap::new()),
            }),
        }
    }

    #[cfg(test)]
    pub fn acquire<F>(
        &self,
        key: SegmentCacheKey,
        operation: F,
    ) -> Result<NetworkFlightWaiter, &'static str>
    where
        F: FnOnce(FlightCancellation) -> Result<VerifiedSegment, &'static str> + Send + 'static,
    {
        self.acquire_prioritized(key, FlightPriority::Background, operation)
    }

    pub(crate) fn acquire_prioritized<F>(
        &self,
        key: SegmentCacheKey,
        priority: FlightPriority,
        operation: F,
    ) -> Result<NetworkFlightWaiter, &'static str>
    where
        F: FnOnce(FlightCancellation) -> Result<VerifiedSegment, &'static str> + Send + 'static,
    {
        let flight = {
            let mut flights = self.inner.flights.lock().expect("singleflight map lock");
            if let Some(flight) = flights.get(&key) {
                flight.cancellation.promote(priority);
                let mut state = flight.state.lock().expect("singleflight state lock");
                if state.waiters >= MAX_WAITERS_PER_FLIGHT {
                    return Err("nntp_singleflight_capacity");
                }
                state.waiters += 1;
                drop(state);
                return Ok(NetworkFlightWaiter {
                    key,
                    flight: Arc::clone(flight),
                    owner: Arc::clone(&self.inner),
                });
            }
            if flights.len() >= MAX_ACTIVE_FLIGHTS {
                return Err("nntp_singleflight_capacity");
            }
            let flight = Arc::new(Flight {
                state: Mutex::new(FlightState {
                    waiters: 1,
                    result: None,
                    result_claimed: false,
                }),
                ready: Condvar::new(),
                cancellation: FlightCancellation::with_priority(priority),
            });
            flights.insert(key, Arc::clone(&flight));
            flight
        };
        let worker = Arc::clone(&flight);
        let worker_owner = Arc::clone(&self.inner);
        if thread::Builder::new()
            .name("usenet-network-fill".into())
            .spawn(move || {
                let (result, panic) =
                    match catch_unwind(AssertUnwindSafe(|| operation(worker.cancellation.clone())))
                    {
                        Ok(result) => (result.map(Arc::new), None),
                        Err(panic) => (Err("nntp_singleflight_panicked"), Some(panic)),
                    };
                let mut state = worker.state.lock().expect("singleflight state lock");
                state.result = Some(result);
                let abandoned = state.waiters == 0;
                drop(state);
                worker.ready.notify_all();
                if abandoned {
                    let mut flights = worker_owner.flights.lock().expect("singleflight map lock");
                    if flights
                        .get(&key)
                        .is_some_and(|current| Arc::ptr_eq(current, &worker))
                        && worker
                            .state
                            .lock()
                            .expect("singleflight state lock")
                            .waiters
                            == 0
                    {
                        flights.remove(&key);
                    }
                }
                if let Some(panic) = panic {
                    resume_unwind(panic);
                }
            })
            .is_err()
        {
            let mut state = flight.state.lock().expect("singleflight state lock");
            state.result = Some(Err("nntp_singleflight_unavailable"));
            drop(state);
            flight.ready.notify_all();
            self.inner
                .flights
                .lock()
                .expect("singleflight map lock")
                .remove(&key);
            return Err("nntp_singleflight_unavailable");
        }
        Ok(NetworkFlightWaiter {
            key,
            flight,
            owner: Arc::clone(&self.inner),
        })
    }

    pub fn active(&self) -> usize {
        self.inner
            .flights
            .lock()
            .expect("singleflight map lock")
            .len()
    }

    #[cfg(test)]
    pub fn waiters(&self) -> usize {
        self.inner
            .flights
            .lock()
            .expect("singleflight map lock")
            .values()
            .map(|flight| {
                flight
                    .state
                    .lock()
                    .expect("singleflight state lock")
                    .waiters
            })
            .sum()
    }
}

#[derive(Clone)]
pub struct FlightCancellation {
    cancelled: Arc<AtomicBool>,
    priority: Arc<AtomicU8>,
}

impl FlightCancellation {
    #[cfg(test)]
    pub(crate) fn new() -> Self {
        Self::with_priority(FlightPriority::Background)
    }

    fn with_priority(priority: FlightPriority) -> Self {
        Self {
            cancelled: Arc::new(AtomicBool::new(false)),
            priority: Arc::new(AtomicU8::new(priority as u8)),
        }
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }

    pub fn checkpoint(&self) -> Result<(), &'static str> {
        if self.is_cancelled() {
            Err("nntp_cancelled")
        } else {
            Ok(())
        }
    }

    pub(crate) fn priority(&self) -> FlightPriority {
        match self.priority.load(Ordering::Acquire) {
            value if value == FlightPriority::Interactive as u8 => FlightPriority::Interactive,
            value if value == FlightPriority::Preparation as u8 => FlightPriority::Preparation,
            value if value == FlightPriority::Background as u8 => FlightPriority::Background,
            _ => unreachable!("invalid singleflight priority"),
        }
    }

    pub(crate) fn promote(&self, priority: FlightPriority) {
        self.priority.fetch_min(priority as u8, Ordering::AcqRel);
    }

    #[cfg(test)]
    pub(crate) fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }
}

pub struct NetworkFlightWaiter {
    key: SegmentCacheKey,
    flight: Arc<Flight>,
    owner: Arc<Inner>,
}

impl NetworkFlightWaiter {
    pub fn claim_result(&self) -> bool {
        let mut state = self.flight.state.lock().expect("singleflight state lock");
        if state.result.is_none() || state.result_claimed {
            return false;
        }
        state.result_claimed = true;
        true
    }

    pub fn try_result(&self) -> Option<SharedResult> {
        self.flight
            .state
            .lock()
            .expect("singleflight state lock")
            .result
            .clone()
    }

    pub fn wait_cancellable_for<F>(&self, cancelled: &F, timeout: Duration) -> Option<SharedResult>
    where
        F: Fn() -> bool,
    {
        let deadline = Instant::now() + timeout;
        let mut state = self.flight.state.lock().expect("singleflight state lock");
        while state.result.is_none() {
            if cancelled() {
                return Some(Err("nntp_cancelled"));
            }
            let now = Instant::now();
            if now >= deadline {
                return None;
            }
            state = self
                .flight
                .ready
                .wait_timeout(state, (deadline - now).min(Duration::from_millis(25)))
                .expect("singleflight state lock")
                .0;
        }
        state.result.clone()
    }

    pub fn wait_cancellable<F>(&self, cancelled: F) -> SharedResult
    where
        F: Fn() -> bool,
    {
        let mut state = self.flight.state.lock().expect("singleflight state lock");
        while state.result.is_none() {
            if cancelled() {
                return Err("nntp_cancelled");
            }
            state = self
                .flight
                .ready
                .wait_timeout(state, Duration::from_millis(25))
                .expect("singleflight state lock")
                .0;
        }
        state.result.as_ref().expect("singleflight result").clone()
    }
}

impl Drop for NetworkFlightWaiter {
    fn drop(&mut self) {
        let mut flights = self.owner.flights.lock().expect("singleflight map lock");
        let mut state = self.flight.state.lock().expect("singleflight state lock");
        debug_assert!(state.waiters > 0);
        state.waiters -= 1;
        if state.waiters == 0 {
            if state.result.is_none() {
                self.flight
                    .cancellation
                    .cancelled
                    .store(true, Ordering::Release);
            } else {
                drop(state);
                flights.remove(&self.key);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{FlightPriority, NetworkSingleflight};
    use crate::cache::{SegmentCacheKey, VerifiedSegment};
    use crate::yenc::DecodedPart;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Barrier, mpsc};
    use std::thread;
    use std::time::{Duration, Instant};

    fn key(value: u8) -> SegmentCacheKey {
        SegmentCacheKey([value; 32])
    }

    fn segment(value: u8) -> VerifiedSegment {
        VerifiedSegment::from_decoded(DecodedPart {
            bytes: vec![value],
            begin: 1,
            end: 1,
            total_size: 1,
            expected_crc32: Some(u32::from(value)),
            expected_whole_crc32: None,
        })
        .unwrap()
    }

    fn wait_for_drain(singleflight: &NetworkSingleflight) {
        let deadline = Instant::now() + Duration::from_secs(1);
        while singleflight.active() != 0 && Instant::now() < deadline {
            thread::yield_now();
        }
        assert_eq!(singleflight.active(), 0);
    }

    #[test]
    fn coalesces_waiters_without_running_work_on_the_first_callers_thread() {
        let singleflight = NetworkSingleflight::new();
        let caller = thread::current().id();
        let runs = Arc::new(AtomicUsize::new(0));
        let gate = Arc::new(Barrier::new(2));
        let worker_gate = Arc::clone(&gate);
        let worker_runs = Arc::clone(&runs);
        let first = singleflight
            .acquire(key(1), move |_| {
                assert_ne!(thread::current().id(), caller);
                worker_runs.fetch_add(1, Ordering::Relaxed);
                worker_gate.wait();
                Ok(segment(7))
            })
            .unwrap();
        let second = singleflight
            .acquire(key(1), |_| panic!("duplicate fill"))
            .unwrap();
        gate.wait();

        assert_eq!(
            first.wait_cancellable(|| false).unwrap().to_decoded().bytes,
            vec![7]
        );
        assert_eq!(
            second
                .wait_cancellable(|| false)
                .unwrap()
                .to_decoded()
                .bytes,
            vec![7]
        );
        assert!(first.claim_result());
        assert!(!second.claim_result());
        assert_eq!(runs.load(Ordering::Relaxed), 1);
        assert_eq!(singleflight.active(), 1);
        drop(first);
        assert_eq!(singleflight.active(), 1);
        drop(second);
        assert_eq!(singleflight.active(), 0);
    }

    #[test]
    fn one_waiter_can_leave_without_cancelling_the_remaining_waiter() {
        let singleflight = NetworkSingleflight::new();
        let (release, released) = mpsc::sync_channel(0);
        let first = singleflight
            .acquire(key(1), move |cancellation| {
                released.recv().unwrap();
                cancellation.checkpoint()?;
                Ok(segment(7))
            })
            .unwrap();
        let second = singleflight
            .acquire(key(1), |_| panic!("duplicate fill"))
            .unwrap();

        drop(first);
        release.send(()).unwrap();

        assert!(second.wait_cancellable(|| false).is_ok());
    }

    #[test]
    fn a_joining_waiter_promotes_the_shared_flight_priority() {
        let singleflight = NetworkSingleflight::new();
        let (observed, receive_observed) = mpsc::sync_channel(0);
        let (finish, receive_finish) = mpsc::sync_channel(0);
        let background = singleflight
            .acquire_prioritized(key(1), FlightPriority::Background, move |cancellation| {
                observed.send(cancellation.clone()).unwrap();
                receive_finish.recv().unwrap();
                Ok(segment(7))
            })
            .unwrap();
        let cancellation = receive_observed
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        assert_eq!(cancellation.priority(), FlightPriority::Background);

        let interactive = singleflight
            .acquire_prioritized(key(1), FlightPriority::Interactive, |_| {
                panic!("priority promotion must not duplicate the fill")
            })
            .unwrap();

        assert_eq!(cancellation.priority(), FlightPriority::Interactive);
        finish.send(()).unwrap();
        assert!(interactive.wait_cancellable(|| false).is_ok());
        drop(background);
        drop(interactive);
    }

    #[test]
    fn last_waiter_cancels_an_unfinished_fill() {
        let singleflight = NetworkSingleflight::new();
        let (observed, receive_observed) = mpsc::sync_channel(0);
        let waiter = singleflight
            .acquire(key(1), move |cancellation| {
                while !cancellation.is_cancelled() {
                    thread::park_timeout(Duration::from_millis(1));
                }
                observed.send(()).unwrap();
                Err("nntp_cancelled")
            })
            .unwrap();

        drop(waiter);

        receive_observed
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        wait_for_drain(&singleflight);
    }

    #[test]
    fn cancelled_wait_stops_the_fill_when_the_disconnected_peer_was_last() {
        let singleflight = NetworkSingleflight::new();
        let (observed, receive_observed) = mpsc::sync_channel(0);
        let waiter = singleflight
            .acquire(key(1), move |cancellation| {
                while !cancellation.is_cancelled() {
                    thread::park_timeout(Duration::from_millis(1));
                }
                observed.send(()).unwrap();
                Err("nntp_cancelled")
            })
            .unwrap();

        assert_eq!(
            waiter.wait_cancellable(|| true).unwrap_err(),
            "nntp_cancelled"
        );
        drop(waiter);

        receive_observed
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        wait_for_drain(&singleflight);
    }

    #[test]
    fn abandoned_worker_remains_bounded_until_it_finishes() {
        let singleflight = NetworkSingleflight::new();
        let (observed, receive_observed) = mpsc::sync_channel(0);
        let (finish, receive_finish) = mpsc::sync_channel(0);
        let first = singleflight
            .acquire(key(1), move |cancellation| {
                while !cancellation.is_cancelled() {
                    thread::park_timeout(Duration::from_millis(1));
                }
                observed.send(()).unwrap();
                receive_finish.recv().unwrap();
                Err("nntp_cancelled")
            })
            .unwrap();

        drop(first);
        receive_observed
            .recv_timeout(Duration::from_secs(1))
            .unwrap();
        assert_eq!(singleflight.active(), 1);
        let joined = singleflight
            .acquire(key(1), |_| {
                panic!("abandoned flight must remain registered")
            })
            .unwrap();
        finish.send(()).unwrap();

        assert_eq!(
            joined.wait_cancellable(|| false).unwrap_err(),
            "nntp_cancelled"
        );
        drop(joined);
        assert_eq!(singleflight.active(), 0);
    }

    #[test]
    fn shares_failures_but_allows_a_later_retry_after_waiters_leave() {
        let singleflight = NetworkSingleflight::new();
        let (release, released) = mpsc::sync_channel(0);
        let first = singleflight
            .acquire(key(1), move |_| {
                released.recv().unwrap();
                Err("nntp_article_missing")
            })
            .unwrap();
        let second = singleflight
            .acquire(key(1), |_| panic!("duplicate fill"))
            .unwrap();
        release.send(()).unwrap();

        assert_eq!(
            first.wait_cancellable(|| false).unwrap_err(),
            "nntp_article_missing"
        );
        assert_eq!(
            second.wait_cancellable(|| false).unwrap_err(),
            "nntp_article_missing"
        );
        assert!(first.claim_result());
        assert!(!second.claim_result());
        drop(first);
        drop(second);

        let retry = singleflight.acquire(key(1), |_| Ok(segment(9))).unwrap();
        assert_eq!(
            retry.wait_cancellable(|| false).unwrap().to_decoded().bytes,
            vec![9]
        );
    }

    #[test]
    fn exposes_worker_panics_and_allows_a_later_retry() {
        let singleflight = NetworkSingleflight::new();
        let waiter = singleflight
            .acquire(key(1), |_| panic!("fill implementation failed"))
            .unwrap();

        assert_eq!(
            waiter.wait_cancellable(|| false).unwrap_err(),
            "nntp_singleflight_panicked"
        );
        drop(waiter);
        wait_for_drain(&singleflight);

        let retry = singleflight.acquire(key(1), |_| Ok(segment(9))).unwrap();
        assert_eq!(
            retry.wait_cancellable(|| false).unwrap().to_decoded().bytes,
            vec![9]
        );
    }
}
