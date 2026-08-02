use std::ffi::CString;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const MIN_SPOOL_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_SPOOL_BYTES: u64 = 2 * 1024 * 1024 * 1024 * 1024;
const MAX_SPOOL_ENTRIES: usize = 25_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Stats {
    pub resident_bytes: u64,
    pub reserved_bytes: u64,
    pub archive_jobs_active: usize,
    pub repair_jobs_active: usize,
    pub archive_busy_rejections: u64,
    pub repair_busy_rejections: u64,
    pub spool_rejections: u64,
}

#[derive(Default)]
struct State {
    reserved_bytes: u64,
    archive_jobs_active: usize,
    repair_jobs_active: usize,
    archive_busy_rejections: u64,
    repair_busy_rejections: u64,
    spool_rejections: u64,
}

pub(crate) struct NativeResources {
    root: PathBuf,
    maximum_spool_bytes: u64,
    minimum_free_disk_bytes: u64,
    maximum_archive_jobs: usize,
    maximum_repair_jobs: usize,
    state: Mutex<State>,
}

pub(crate) struct Reservation<'a> {
    resources: &'a NativeResources,
    bytes: u64,
    archive_job: bool,
    repair_job: bool,
}

impl NativeResources {
    pub(crate) fn new(
        root: &Path,
        maximum_spool_bytes: u64,
        minimum_free_disk_bytes: u64,
        maximum_archive_jobs: usize,
        maximum_repair_jobs: usize,
    ) -> Result<Self, &'static str> {
        if !(MIN_SPOOL_BYTES..=MAX_SPOOL_BYTES).contains(&maximum_spool_bytes)
            || maximum_archive_jobs == 0
            || maximum_repair_jobs == 0
        {
            return Err("invalid_native_resource_budget");
        }
        let root = fs::canonicalize(root).map_err(|_| "native_resource_unavailable")?;
        let metadata = fs::symlink_metadata(&root).map_err(|_| "native_resource_unavailable")?;
        if !metadata.file_type().is_dir() {
            return Err("native_resource_unavailable");
        }
        Ok(Self {
            root,
            maximum_spool_bytes,
            minimum_free_disk_bytes,
            maximum_archive_jobs,
            maximum_repair_jobs,
            state: Mutex::new(State::default()),
        })
    }

    pub(crate) fn reserve_materialization(
        &self,
        bytes: u64,
        publication_root: &Path,
    ) -> Result<Reservation<'_>, &'static str> {
        let staging_device = fs::metadata(&self.root)
            .map_err(|_| "native_resource_unavailable")?
            .dev();
        let publication_device = fs::metadata(publication_root)
            .map_err(|_| "native_resource_unavailable")?
            .dev();
        let peak_bytes = if staging_device == publication_device {
            bytes
        } else {
            bytes.checked_mul(2).ok_or("native_spool_full")?
        };
        self.reserve(peak_bytes, false, false, true)
    }

    pub(crate) fn reserve_archive(&self, bytes: u64) -> Result<Reservation<'_>, &'static str> {
        self.reserve(bytes, true, false, true)
    }

    pub(crate) fn reserve_archive_job(&self) -> Result<Reservation<'_>, &'static str> {
        self.reserve(0, true, false, false)
    }

    pub(crate) fn reserve_repair(&self, bytes: u64) -> Result<Reservation<'_>, &'static str> {
        self.reserve(bytes, false, true, true)
    }

    pub(crate) fn ensure_spool_available(&self, bytes: u64) -> Result<(), &'static str> {
        let mut state = self.state.lock().expect("native resource state lock");
        self.reserve_spool_bytes(&mut state, bytes)?;
        state.reserved_bytes -= bytes;
        Ok(())
    }

    pub(crate) fn stats(&self) -> Result<Stats, &'static str> {
        let state = self.state.lock().expect("native resource state lock");
        let resident = resident_bytes(&self.root)?;
        Ok(Stats {
            resident_bytes: resident,
            reserved_bytes: state.reserved_bytes,
            archive_jobs_active: state.archive_jobs_active,
            repair_jobs_active: state.repair_jobs_active,
            archive_busy_rejections: state.archive_busy_rejections,
            repair_busy_rejections: state.repair_busy_rejections,
            spool_rejections: state.spool_rejections,
        })
    }

    fn reserve(
        &self,
        bytes: u64,
        archive_job: bool,
        repair_job: bool,
        spool: bool,
    ) -> Result<Reservation<'_>, &'static str> {
        let mut state = self.state.lock().expect("native resource state lock");
        if archive_job && state.archive_jobs_active >= self.maximum_archive_jobs {
            state.archive_busy_rejections = state.archive_busy_rejections.saturating_add(1);
            return Err("archive_busy");
        }
        if repair_job && state.repair_jobs_active >= self.maximum_repair_jobs {
            state.repair_busy_rejections = state.repair_busy_rejections.saturating_add(1);
            return Err("repair_busy");
        }
        if spool {
            self.reserve_spool_bytes(&mut state, bytes)?;
        }
        if archive_job {
            state.archive_jobs_active += 1;
        }
        if repair_job {
            state.repair_jobs_active += 1;
        }
        Ok(Reservation {
            resources: self,
            bytes,
            archive_job,
            repair_job,
        })
    }

    fn reserve_spool_bytes(&self, state: &mut State, bytes: u64) -> Result<(), &'static str> {
        let Some(reserved) = state.reserved_bytes.checked_add(bytes) else {
            state.spool_rejections = state.spool_rejections.saturating_add(1);
            return Err("native_spool_full");
        };
        let resident = resident_bytes(&self.root)?;
        if resident
            .checked_add(reserved)
            .is_none_or(|used| used > self.maximum_spool_bytes)
        {
            state.spool_rejections = state.spool_rejections.saturating_add(1);
            return Err("native_spool_full");
        }
        let Some(required_free) = self.minimum_free_disk_bytes.checked_add(reserved) else {
            state.spool_rejections = state.spool_rejections.saturating_add(1);
            return Err("native_disk_pressure");
        };
        if available_bytes(&self.root)? < required_free {
            state.spool_rejections = state.spool_rejections.saturating_add(1);
            return Err("native_disk_pressure");
        }
        state.reserved_bytes = reserved;
        Ok(())
    }
}

impl Reservation<'_> {
    pub(crate) fn grow(&mut self, bytes: u64) -> Result<(), &'static str> {
        let mut state = self
            .resources
            .state
            .lock()
            .expect("native resource state lock");
        self.resources.reserve_spool_bytes(&mut state, bytes)?;
        self.bytes = self
            .bytes
            .checked_add(bytes)
            .expect("native spool reservation growth");
        Ok(())
    }
}

impl Drop for Reservation<'_> {
    fn drop(&mut self) {
        let mut state = self
            .resources
            .state
            .lock()
            .expect("native resource state lock");
        state.reserved_bytes = state
            .reserved_bytes
            .checked_sub(self.bytes)
            .expect("native spool reservation accounting");
        if self.archive_job {
            state.archive_jobs_active = state
                .archive_jobs_active
                .checked_sub(1)
                .expect("native archive reservation accounting");
        }
        if self.repair_job {
            state.repair_jobs_active = state
                .repair_jobs_active
                .checked_sub(1)
                .expect("native repair reservation accounting");
        }
    }
}

fn resident_bytes(root: &Path) -> Result<u64, &'static str> {
    let directory = root.join("materialized");
    let metadata = match fs::symlink_metadata(&directory) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(_) => return Err("native_resource_unavailable"),
    };
    if !metadata.file_type().is_dir() {
        return Err("native_resource_unavailable");
    }
    let entries = fs::read_dir(directory).map_err(|_| "native_resource_unavailable")?;
    let mut total = 0_u64;
    for (index, entry) in entries.enumerate() {
        if index >= MAX_SPOOL_ENTRIES {
            return Err("native_resource_unavailable");
        }
        let entry = entry.map_err(|_| "native_resource_unavailable")?;
        let metadata =
            fs::symlink_metadata(entry.path()).map_err(|_| "native_resource_unavailable")?;
        if !metadata.file_type().is_file() {
            return Err("native_resource_unavailable");
        }
        total = total
            .checked_add(metadata.len())
            .ok_or("native_resource_unavailable")?;
    }
    Ok(total)
}

fn available_bytes(root: &Path) -> Result<u64, &'static str> {
    let encoded =
        CString::new(root.as_os_str().as_bytes()).map_err(|_| "native_resource_unavailable")?;
    let mut statistics = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: encoded is NUL-terminated and statistics points to writable storage.
    if unsafe { libc::statvfs(encoded.as_ptr(), statistics.as_mut_ptr()) } != 0 {
        return Err("native_resource_unavailable");
    }
    // SAFETY: statvfs returned success and initialized the structure.
    let statistics = unsafe { statistics.assume_init() };
    statistics
        .f_bavail
        .checked_mul(statistics.f_frsize)
        .ok_or("native_resource_unavailable")
}

#[cfg(test)]
mod tests {
    use super::NativeResources;
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQUENCE: AtomicU64 = AtomicU64::new(0);

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let root = std::env::temp_dir().join(format!(
                "comet-native-resources-{}-{sequence}",
                std::process::id()
            ));
            fs::create_dir(&root).expect("create native resource fixture");
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .expect("secure native resource fixture");
            Self(root)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn reservations_are_atomic_bounded_and_released_exactly_once() {
        let fixture = Fixture::new();
        let resources =
            NativeResources::new(&fixture.0, 1024 * 1024 * 1024, 0, 1, 1).expect("resources");
        let first = resources
            .reserve_archive(600 * 1024 * 1024)
            .expect("first archive reservation");

        assert_eq!(resources.reserve_archive(1).err(), Some("archive_busy"));
        assert_eq!(
            resources
                .reserve_materialization(500 * 1024 * 1024, &fixture.0)
                .err(),
            Some("native_spool_full")
        );
        let stats = resources.stats().expect("resource stats");
        assert_eq!(stats.reserved_bytes, 600 * 1024 * 1024);
        assert_eq!(stats.archive_jobs_active, 1);
        assert_eq!(stats.archive_busy_rejections, 1);
        assert_eq!(stats.spool_rejections, 1);

        drop(first);
        let archive_job = resources
            .reserve_archive_job()
            .expect("job-only archive reservation");
        assert_eq!(resources.stats().expect("resource stats").reserved_bytes, 0);
        drop(archive_job);
        let repair = resources
            .reserve_repair(1)
            .expect("first repair reservation");
        assert_eq!(resources.reserve_repair(1).err(), Some("repair_busy"));
        let mut repair = repair;
        repair.grow(1024).expect("grow repair reservation");
        let stats = resources.stats().expect("resource stats");
        assert_eq!(stats.reserved_bytes, 1025);
        assert_eq!(stats.repair_jobs_active, 1);
        assert_eq!(stats.repair_busy_rejections, 1);
        drop(repair);
        resources
            .ensure_spool_available(1024 * 1024 * 1024)
            .expect("full available spool capacity");
        assert_eq!(resources.stats().expect("resource stats").reserved_bytes, 0);
        assert_eq!(
            resources.ensure_spool_available(1024 * 1024 * 1024 + 1),
            Err("native_spool_full")
        );
        let materialization = resources
            .reserve_materialization(512 * 1024 * 1024, &fixture.0)
            .expect("materialization peak reservation");
        assert_eq!(
            resources.stats().expect("resource stats").reserved_bytes,
            512 * 1024 * 1024
        );
        drop(materialization);
    }

    #[test]
    fn accepts_operator_concurrency_above_the_legacy_caps() {
        let fixture = Fixture::new();
        let resources = NativeResources::new(&fixture.0, 1024 * 1024 * 1024, 0, 16, 16)
            .expect("operator concurrency");

        let archive = resources.reserve_archive_job().expect("archive job");
        let repair = resources.reserve_repair(1).expect("repair job");
        assert_eq!(
            resources
                .stats()
                .expect("resource stats")
                .archive_jobs_active,
            1
        );
        assert_eq!(
            resources
                .stats()
                .expect("resource stats")
                .repair_jobs_active,
            1
        );
        drop((archive, repair));
    }

    #[test]
    fn repair_reservation_growth_is_atomic_and_released_once() {
        let fixture = Fixture::new();
        let resources =
            NativeResources::new(&fixture.0, 1024 * 1024 * 1024, 0, 1, 1).expect("resources");
        let mut repair = resources
            .reserve_repair(600 * 1024 * 1024)
            .expect("repair reservation");

        assert_eq!(repair.grow(500 * 1024 * 1024), Err("native_spool_full"));
        assert_eq!(
            resources.stats().expect("resource stats").reserved_bytes,
            600 * 1024 * 1024
        );
        repair.grow(100 * 1024 * 1024).expect("bounded growth");
        assert_eq!(
            resources.stats().expect("resource stats").reserved_bytes,
            700 * 1024 * 1024
        );
        drop(repair);
        assert_eq!(resources.stats().expect("resource stats").reserved_bytes, 0);
    }

    #[test]
    fn resident_files_and_free_space_participate_in_admission() {
        let fixture = Fixture::new();
        let materialized = fixture.0.join("materialized");
        fs::create_dir(&materialized).expect("create materialized fixture");
        fs::write(materialized.join("resident.bin"), vec![0; 64 * 1024])
            .expect("write resident fixture");
        let resources = NativeResources::new(&fixture.0, 1024 * 1024 * 1024, u64::MAX, 1, 1)
            .expect("resources");

        assert_eq!(
            resources.reserve_materialization(1, &fixture.0).err(),
            Some("native_disk_pressure")
        );
        assert_eq!(
            resources.stats().expect("resource stats").resident_bytes,
            64 * 1024
        );
    }

    #[test]
    fn refuses_non_regular_resident_spool_entries() {
        let fixture = Fixture::new();
        let materialized = fixture.0.join("materialized");
        fs::create_dir(&materialized).expect("create materialized fixture");
        symlink("/dev/null", materialized.join("escaped.bin")).expect("create resident symlink");
        let resources =
            NativeResources::new(&fixture.0, 1024 * 1024 * 1024, 0, 1, 1).expect("resources");

        assert_eq!(
            resources.reserve_materialization(1, &fixture.0).err(),
            Some("native_resource_unavailable")
        );
    }

    #[test]
    fn validates_native_resource_configuration_again_at_the_engine_boundary() {
        let fixture = Fixture::new();

        assert_eq!(
            NativeResources::new(&fixture.0, 1024 * 1024 * 1024 - 1, 0, 1, 1).err(),
            Some("invalid_native_resource_budget")
        );
        assert_eq!(
            NativeResources::new(&fixture.0, 1024 * 1024 * 1024, 0, 0, 1).err(),
            Some("invalid_native_resource_budget")
        );
        assert_eq!(
            NativeResources::new(&fixture.0, 1024 * 1024 * 1024, 0, 1, 0).err(),
            Some("invalid_native_resource_budget")
        );
    }
}
