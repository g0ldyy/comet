use crate::cache::{
    Admission, NetworkSingleflight, SegmentCacheKey, SegmentLease, VerifiedSegment,
    VerifiedSegmentCache,
};
use crate::resources::NativeResources;
use crate::session::{RandomAccessSession, SessionPosting, SessionRegistry};
use crate::yenc::DecodedPart;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const SESSION_ID: &str = "AAAAAAAAAAAAAAAAAAAAAA";

#[derive(Clone, Copy, Debug)]
struct ProcessSnapshot {
    file_descriptors: usize,
    tasks: usize,
    rss_bytes: u64,
}

struct TemporaryDirectory(PathBuf);

impl TemporaryDirectory {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system time")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "comet-quality-stress-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&path).expect("create stress directory");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700))
            .expect("secure stress directory");
        Self(path)
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn count_directory(path: &str) -> usize {
    fs::read_dir(path)
        .expect("read process directory")
        .fold(0, |count, entry| {
            entry.expect("read process directory entry");
            count + 1
        })
}

fn snapshot() -> ProcessSnapshot {
    let status = fs::read_to_string("/proc/self/status").expect("read process status");
    let rss_kib = status
        .lines()
        .find_map(|line| {
            line.strip_prefix("VmRSS:")
                .and_then(|value| value.split_ascii_whitespace().next())
                .and_then(|value| value.parse::<u64>().ok())
        })
        .expect("VmRSS is available");
    ProcessSnapshot {
        file_descriptors: count_directory("/proc/self/fd"),
        tasks: count_directory("/proc/self/task"),
        rss_bytes: rss_kib * 1024,
    }
}

fn cache_key(iteration: u64, lane: u64) -> SegmentCacheKey {
    SegmentCacheKey::acquisition(
        [7; 32],
        "stress-generation",
        "stress-provider",
        &format!("{iteration}-{lane}@example.invalid"),
    )
}

fn segment(value: u8) -> VerifiedSegment {
    VerifiedSegment::from_decoded(DecodedPart {
        bytes: vec![value; 4096],
        begin: 1,
        end: 4096,
        total_size: 4096,
        expected_crc32: Some(u32::from(value)),
        expected_whole_crc32: Some(u32::from(value)),
    })
    .unwrap()
}

fn session() -> RandomAccessSession {
    let first = segment(1);
    RandomAccessSession::new(
        SESSION_ID.to_owned(),
        vec![SessionPosting {
            number: 1,
            declared_encoded_bytes: 4096,
            message_id: "slow-reader@example.invalid".into(),
            fallback_postings: Vec::new(),
        }],
        &first,
    )
    .unwrap()
}

fn cancelled_warmups(singleflight: &NetworkSingleflight, iteration: u64) {
    for lane in 0..4 {
        let waiter = singleflight
            .acquire(cache_key(iteration, lane), |cancellation| {
                while !cancellation.is_cancelled() {
                    thread::sleep(Duration::from_millis(1));
                }
                Err("nntp_cancelled")
            })
            .expect("start cancellable warm-up");
        drop(waiter);
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while singleflight.active() != 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(1));
    }
    assert_eq!(singleflight.active(), 0, "cancelled warm-ups must drain");
}

fn provider_outage(singleflight: &NetworkSingleflight, iteration: u64) {
    let waiter = singleflight
        .acquire(cache_key(iteration, 9), |_cancellation| {
            Err("nntp_provider_unavailable")
        })
        .expect("start outage attempt");
    assert_eq!(
        waiter.wait_cancellable(|| false).unwrap_err(),
        "nntp_provider_unavailable"
    );
    drop(waiter);
    assert_eq!(singleflight.active(), 0);
}

fn slow_client(registry: &mut SessionRegistry<()>) {
    let now = Instant::now();
    let reader = registry
        .open_reader(SESSION_ID, now)
        .expect("open slow reader");
    let lease = registry
        .get_with_reader(SESSION_ID, &reader, now)
        .expect("lease slow reader");
    let bytes = RandomAccessSession::read_at(&lease.session, 0, 4096, |_posting| {
        Ok(SegmentLease::detached(Arc::new(segment(1))))
    })
    .expect("read slow client bytes");
    assert_eq!(bytes.len(), 4096);
    thread::sleep(Duration::from_micros(100));
    drop(lease);
    registry
        .close_reader(SESSION_ID, &reader, Instant::now())
        .expect("close slow reader");
}

#[test]
#[ignore = "run through scripts/run_usenet_stress.sh"]
fn quality_stress_native_lifecycle_and_resource_plateaus() {
    let seconds = std::env::var_os("USENET_STRESS_SECONDS")
        .map(|value| {
            value
                .into_string()
                .expect("UTF-8 stress duration")
                .parse::<u64>()
                .expect("integer stress duration")
        })
        .unwrap_or(120);
    assert!((5..=900).contains(&seconds));
    let directory = TemporaryDirectory::new();
    let resources = NativeResources::new(&directory.0, 1024 * 1024 * 1024, u64::MAX, 1, 1).unwrap();
    let singleflight = NetworkSingleflight::new();
    let mut cache = VerifiedSegmentCache::new(8 * 1024 * 1024).unwrap();
    let mut sessions = SessionRegistry::new(1024 * 1024);
    sessions
        .insert(session(), (), "b".repeat(64), Instant::now())
        .unwrap();

    // Initialize every lazy runtime allocation before plateau sampling.
    let initial = snapshot();
    let started = Instant::now();
    let deadline = started + Duration::from_secs(seconds);
    let warmup_deadline = started + Duration::from_secs((seconds / 4).max(1));
    let mut first_plateau = None;
    let mut peak_fds = 0;
    let mut peak_tasks = 0;
    let mut plateau_samples = 0;
    let mut iteration = 0_u64;
    while Instant::now() < deadline {
        iteration += 1;

        // Cache churn must stay admitted/evicted inside the fixed memory bound.
        for lane in 0..256 {
            let key = cache_key(iteration, 1000 + lane);
            assert!(matches!(
                cache.insert(key, segment(lane as u8)),
                Admission::Admitted | Admission::Replaced
            ));
            let lease = cache.get(key).expect("cache hit after admission");
            assert_eq!(lease.segment().bytes().len(), 4096);
        }
        assert!(cache.stats().used_bytes <= 8 * 1024 * 1024);

        cancelled_warmups(&singleflight, iteration);
        provider_outage(&singleflight, iteration);
        slow_client(&mut sessions);

        // The minimum-free guard deterministically rejects work before any
        // allocation and leaves all reservations at zero.
        assert_eq!(
            resources.reserve_materialization(4096, &directory.0).err(),
            Some("native_disk_pressure")
        );
        assert_eq!(resources.stats().unwrap().reserved_bytes, 0);

        // Rebuild/drop the same parser runtime repeatedly to exercise engine
        // restart ownership, SQLite handles, pool state and descriptor cleanup.
        {
            let restart = crate::EngineState::new(&directory.0, 16 * 1024 * 1024, 0, 0, 8)
                .expect("restart parser runtime");
            assert_eq!(
                restart
                    .segment_cache
                    .lock()
                    .expect("segment cache lock")
                    .stats()
                    .entries,
                0
            );
        }

        if Instant::now() >= warmup_deadline {
            let current = snapshot();
            first_plateau.get_or_insert(current);
            peak_fds = peak_fds.max(current.file_descriptors);
            peak_tasks = peak_tasks.max(current.tasks);
            plateau_samples += 1;
        }
    }

    assert!(iteration >= 5, "stress gate did not execute enough cycles");
    assert!(plateau_samples >= 3, "stress gate lacks plateau samples");
    cache.purge();
    assert_eq!(singleflight.active(), 0);
    assert_eq!(sessions.len(Instant::now()), 1);
    let final_snapshot = snapshot();
    let first_plateau = first_plateau.expect("first plateau sample");

    assert!(
        peak_fds <= first_plateau.file_descriptors + 4
            && final_snapshot.file_descriptors <= initial.file_descriptors + 4,
        "file descriptors did not plateau: {initial:?} -> {first_plateau:?} -> {final_snapshot:?}"
    );
    assert!(
        peak_tasks <= first_plateau.tasks + 4 && final_snapshot.tasks <= initial.tasks + 4,
        "tasks did not plateau: {initial:?} -> {first_plateau:?} -> {final_snapshot:?}"
    );
    assert!(
        final_snapshot.rss_bytes <= first_plateau.rss_bytes + 64 * 1024 * 1024,
        "memory did not plateau: {first_plateau:?} -> {final_snapshot:?}"
    );
    println!(
        "USENET_STRESS seconds={seconds} iterations={iteration} initial={initial:?} plateau={first_plateau:?} final={final_snapshot:?}"
    );
}
