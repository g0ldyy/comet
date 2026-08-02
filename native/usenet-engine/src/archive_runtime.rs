use crate::materialization::{
    ImmutableFileIdentity, immutable_file_identity, sealed_file_metadata,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::mem;
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::ptr;
use std::thread;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

const ARCHIVE_OK: c_int = 0;
const ARCHIVE_EOF: c_int = 1;
const ARCHIVE_WARN: c_int = -20;
const REGULAR_FILE: u32 = 0o100000;
pub(crate) const MAX_ARCHIVE_ENTRIES: usize = crate::nzb::MAX_FILES;
pub(crate) const MAX_ARCHIVE_OUTPUT_BYTES: u64 = crate::limits::MAX_LOGICAL_BYTES;
pub(crate) const MAX_COMPRESSION_RATIO: u64 = 100;
pub(crate) const MAX_ARCHIVE_CATALOG_BYTES: u64 = 1024 * 1024;
pub(crate) const MAX_ARCHIVE_PASSPHRASE_BYTES: usize = 4 * 1024;

type ArchiveReadNew = unsafe extern "C" fn() -> *mut c_void;
type ArchiveReadSupport = unsafe extern "C" fn(*mut c_void) -> c_int;
type ArchiveReadFree = unsafe extern "C" fn(*mut c_void) -> c_int;
type ArchiveReadOpenFd = unsafe extern "C" fn(*mut c_void, c_int, usize) -> c_int;
type ArchiveReadAddPassphrase = unsafe extern "C" fn(*mut c_void, *const c_char) -> c_int;
type ArchiveReadNextHeader = unsafe extern "C" fn(*mut c_void, *mut *mut c_void) -> c_int;
type ArchiveReadData = unsafe extern "C" fn(*mut c_void, *mut c_void, usize) -> isize;
type ArchiveReadDataSkip = unsafe extern "C" fn(*mut c_void) -> c_int;
type ArchiveEntryString = unsafe extern "C" fn(*mut c_void) -> *const c_char;
type ArchiveEntryInt = unsafe extern "C" fn(*mut c_void) -> c_int;
type ArchiveEntryFiletype = unsafe extern "C" fn(*mut c_void) -> u32;
type ArchiveEntrySize = unsafe extern "C" fn(*mut c_void) -> i64;

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct ExtractedEntry {
    pub identity: String,
    pub size: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CatalogEntry {
    pub relative_path: String,
    pub exact_size: u64,
    pub kind: crate::inspect::AssetKind,
}

struct ArchiveScan {
    catalog: Vec<CatalogEntry>,
    extracted: Option<ExtractedEntry>,
    entries: usize,
    logical_bytes: u64,
}

#[derive(Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct CatalogCensus {
    pub catalog: Vec<CatalogEntry>,
    pub entries: usize,
    pub logical_bytes: u64,
}

// All libarchive function pointers resolved once from the admitted library at
// validation time. Function pointer types are `Copy` and `Send + Sync`, so the
// aggregate is automatically `Send + Sync` and safe to share across worker
// threads alongside the immutable handle.
struct Symbols {
    new_reader: ArchiveReadNew,
    free_reader: ArchiveReadFree,
    open_fd: ArchiveReadOpenFd,
    add_passphrase: ArchiveReadAddPassphrase,
    next_header: ArchiveReadNextHeader,
    read_data: ArchiveReadData,
    skip_data: ArchiveReadDataSkip,
    pathname: ArchiveEntryString,
    hardlink: ArchiveEntryString,
    symlink: ArchiveEntryString,
    filetype: ArchiveEntryFiletype,
    size_is_set: ArchiveEntryInt,
    entry_size: ArchiveEntrySize,
    entry_encrypted: ArchiveEntryInt,
    support_filter_all: ArchiveReadSupport,
    support_format_all: ArchiveReadSupport,
}

pub(crate) struct Runtime {
    handle: *mut c_void,
    library: PathBuf,
    symbols: Symbols,
}

// The admitted library is initialized synchronously before worker threads
// start. The retained handle is immutable and exists only to keep that exact
// library loaded for the lifetime of EngineState.
unsafe impl Send for Runtime {}
unsafe impl Sync for Runtime {}

impl Runtime {
    pub(crate) fn validate(library: &Path) -> Result<Self, &'static str> {
        validate_library_path(library)?;
        let encoded = CString::new(library.as_os_str().as_bytes())
            .map_err(|_| "libarchive_library_invalid")?;
        // SAFETY: the NUL-terminated path remains live for the duration of the
        // call and the returned handle is closed by Runtime::drop.
        let handle = unsafe { libc::dlopen(encoded.as_ptr(), libc::RTLD_NOW | libc::RTLD_LOCAL) };
        if handle.is_null() {
            return Err("libarchive_library_unavailable");
        }
        // Resolve every symbol once up front. On resolution failure the handle
        // must be closed manually because Runtime (which owns it via Drop) has
        // not been constructed yet.
        // SAFETY: handle is live for the duration of resolution; each symbol is
        // resolved using its public libarchive C ABI declaration.
        let symbols = match unsafe { resolve_symbols(handle) } {
            Ok(symbols) => symbols,
            Err(err) => {
                // SAFETY: handle was just opened and is retained nowhere else.
                unsafe { libc::dlclose(handle) };
                return Err(err);
            }
        };
        Ok(Self {
            handle,
            library: library.to_path_buf(),
            symbols,
        })
    }

    #[cfg(test)]
    pub(crate) fn extract_selected(
        &self,
        input: &Path,
        output: &Path,
        selected_path: &str,
    ) -> Result<ExtractedEntry, &'static str> {
        let result = self.extract_selected_unsealed(input, output, selected_path, None);
        let result = result.and_then(|extracted| {
            seal_worker_output(
                output,
                extracted.size,
                extracted.size,
                "archive_output_failed",
            )?;
            Ok(extracted)
        });
        cleanup_failed_output(output, result)
    }

    fn extract_selected_unsealed(
        &self,
        input: &Path,
        output: &Path,
        selected_path: &str,
        passphrase: Option<&str>,
    ) -> Result<ExtractedEntry, &'static str> {
        let (input_file, input_identity) = open_archive_input(input)?;
        validate_archive_output(output)?;
        let selected_path = crate::archive::normalize_archive_path(selected_path)
            .map_err(|_| "archive_path_invalid")?;
        let input_size = input_identity.size;
        if input_size == 0 || input_size > MAX_ARCHIVE_OUTPUT_BYTES {
            return Err("archive_input_invalid");
        }
        let result = self
            .scan_opened(
                &input_file,
                input_size,
                Some((output, selected_path.as_str())),
                passphrase,
            )
            .and_then(|scan| scan.extracted.ok_or("archive_selected_entry_missing"))
            .and_then(|extracted| {
                revalidate_archive_input(&input_file, input_identity)?;
                Ok(extracted)
            });
        cleanup_failed_output(output, result)
    }

    fn catalog(
        &self,
        input: &Path,
        passphrase: Option<&str>,
    ) -> Result<CatalogCensus, &'static str> {
        let (input_file, input_identity) = open_archive_input(input)?;
        let input_size = input_identity.size;
        if input_size == 0 || input_size > MAX_ARCHIVE_OUTPUT_BYTES {
            return Err("archive_input_invalid");
        }
        self.scan_opened(&input_file, input_size, None, passphrase)
            .and_then(|scan| {
                revalidate_archive_input(&input_file, input_identity)?;
                Ok(CatalogCensus {
                    catalog: scan.catalog,
                    entries: scan.entries,
                    logical_bytes: scan.logical_bytes,
                })
            })
    }

    #[cfg(test)]
    fn write_catalog(&self, input: &Path, output: &Path) -> Result<CatalogCensus, &'static str> {
        let result = self.write_catalog_unsealed(input, output, None);
        let result = result.and_then(|census| {
            seal_worker_output(
                output,
                1,
                MAX_ARCHIVE_CATALOG_BYTES,
                "archive_output_failed",
            )?;
            Ok(census)
        });
        cleanup_failed_output(output, result)
    }

    fn write_catalog_unsealed(
        &self,
        input: &Path,
        output: &Path,
        passphrase: Option<&str>,
    ) -> Result<CatalogCensus, &'static str> {
        validate_archive_output(output)?;
        let result = (|| {
            let census = self.catalog(input, passphrase)?;
            let encoded = serde_json::to_vec(&census).map_err(|_| "archive_catalog_invalid")?;
            if encoded.len() > MAX_ARCHIVE_CATALOG_BYTES as usize {
                return Err("archive_budget_exceeded");
            }
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(output)
                .map_err(|_| "archive_output_failed")?;
            file.write_all(&encoded)
                .map_err(|_| "archive_output_failed")?;
            file.flush().map_err(|_| "archive_output_failed")?;
            Ok(census)
        })();
        cleanup_failed_output(output, result)
    }

    pub(crate) fn verify_extraction_engine(&self, root: &Path) -> Result<(), &'static str> {
        let mut nonce = [0u8; 8];
        getrandom::fill(&mut nonce).map_err(|_| "libarchive_readiness_failed")?;
        let stage = root.join(format!(
            ".archive-readiness-{}-{:016x}",
            std::process::id(),
            u64::from_ne_bytes(nonce)
        ));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&stage)
            .map_err(|_| "libarchive_readiness_failed")?;
        let cleanup = ReadinessStage(stage.clone());
        let input = stage.join("input.tar");
        let output = stage.join("output.bin");
        let expected = b"comet-archive-readiness-v1";
        let archive = readiness_tar("readiness.bin", expected);
        fs::write(&input, archive).map_err(|_| "libarchive_readiness_failed")?;
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400))
            .map_err(|_| "libarchive_readiness_failed")?;
        self.run_sandboxed(
            &input,
            &output,
            "readiness.bin",
            expected.len() as u64,
            None,
            Duration::from_secs(5),
            &|| false,
        )
        .map_err(|_| "libarchive_readiness_failed")?;
        if fs::read(&output).map_err(|_| "libarchive_readiness_failed")? != expected {
            return Err("libarchive_readiness_failed");
        }
        fs::remove_dir_all(&stage).map_err(|_| "libarchive_readiness_failed")?;
        mem::forget(cleanup);
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn run_sandboxed<F>(
        &self,
        input: &Path,
        output: &Path,
        selected_path: &str,
        expected_output_size: u64,
        passphrase: Option<&str>,
        wall_time: Duration,
        cancelled: &F,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        let result = self.run_worker(
            input,
            output,
            Some(selected_path),
            expected_output_size,
            passphrase,
            wall_time,
            cancelled,
        );
        let result = result.and_then(|()| {
            seal_worker_output(
                output,
                expected_output_size,
                expected_output_size,
                "archive_worker_failed",
            )?;
            Ok(())
        });
        cleanup_failed_output(output, result)
    }

    pub(crate) fn catalog_sandboxed<F>(
        &self,
        input: &Path,
        output: &Path,
        passphrase: Option<&str>,
        wall_time: Duration,
        cancelled: &F,
    ) -> Result<CatalogCensus, &'static str>
    where
        F: Fn() -> bool,
    {
        let result = self
            .run_worker(
                input,
                output,
                None,
                MAX_ARCHIVE_CATALOG_BYTES,
                passphrase,
                wall_time,
                cancelled,
            )
            .and_then(|()| {
                let file = seal_worker_output(
                    output,
                    1,
                    MAX_ARCHIVE_CATALOG_BYTES,
                    "archive_worker_failed",
                )?;
                let metadata = file.metadata().map_err(|_| "archive_worker_failed")?;
                let mut encoded = Vec::with_capacity(metadata.len() as usize);
                file.take(MAX_ARCHIVE_CATALOG_BYTES + 1)
                    .read_to_end(&mut encoded)
                    .map_err(|_| "archive_worker_failed")?;
                let census: CatalogCensus =
                    serde_json::from_slice(&encoded).map_err(|_| "archive_worker_failed")?;
                validate_census(&census)?;
                Ok(census)
            });
        cleanup_failed_output(output, result)
    }

    #[allow(clippy::too_many_arguments)]
    fn run_worker<F>(
        &self,
        input: &Path,
        output: &Path,
        selected_path: Option<&str>,
        maximum_output_size: u64,
        passphrase: Option<&str>,
        wall_time: Duration,
        cancelled: &F,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        let writable_root = output.parent().ok_or("archive_output_invalid")?;
        let sandbox = crate::child_process::Sandbox::new(
            crate::child_process::Limits {
                cpu_seconds: 30 * 60,
                memory_bytes: 512 * 1024 * 1024,
                file_size_bytes: maximum_output_size,
                open_files: 64,
            },
            Some(writable_root),
        )
        .map_err(|_| "archive_worker_failed")?;
        let executable = std::env::current_exe().map_err(|_| "archive_worker_failed")?;
        let mut command = Command::new(executable);
        command
            .arg(if selected_path.is_some() {
                "--archive-worker"
            } else {
                "--archive-catalog-worker"
            })
            .arg("--libarchive-library")
            .arg(&self.library)
            .arg("--input")
            .arg(input)
            .arg("--output")
            .arg(output);
        if let Some(selected_path) = selected_path {
            command.args(["--selected-path", selected_path]);
        }
        if passphrase.is_some() {
            command.arg("--passphrase-stdin");
        }
        command
            .current_dir(writable_root)
            .env_clear()
            .stdin(if passphrase.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        // SAFETY: Sandbox prepares its allocation-backed seccomp program
        // before fork; apply performs only its fixed syscall sequence.
        unsafe {
            command.pre_exec(move || sandbox.apply());
        }
        let mut child = command.spawn().map_err(|_| "archive_worker_failed")?;
        if let Some(passphrase) = passphrase {
            let write_result =
                child
                    .stdin
                    .take()
                    .ok_or("archive_worker_failed")
                    .and_then(|mut input| {
                        input
                            .write_all(passphrase.as_bytes())
                            .map_err(|_| "archive_worker_failed")
                    });
            if let Err(error) = write_result {
                return terminate_worker(&mut child, error);
            }
        }
        let deadline = Instant::now() + wall_time;
        let status = loop {
            match crate::child_process::try_wait_group(&mut child) {
                Ok(Some(status)) => break status,
                Err(_) => {
                    return terminate_worker(&mut child, "archive_worker_failed");
                }
                Ok(None) if cancelled() => {
                    return terminate_worker(&mut child, "archive_cancelled");
                }
                Ok(None) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(10));
                }
                Ok(None) => {
                    return terminate_worker(&mut child, "archive_timed_out");
                }
            }
        };
        let Some(code) = status.code() else {
            return Err("archive_worker_failed");
        };
        if code != 0 {
            return Err(match code {
                22 => "archive_budget_exceeded",
                23 => "archive_path_invalid",
                24 => "archive_selected_entry_missing",
                25 => "archive_integrity_failed",
                26 => "archive_catalog_invalid",
                27 => "archive_password_required",
                28 => "archive_password_invalid",
                _ => "archive_worker_failed",
            });
        }
        Ok(())
    }

    fn scan_opened(
        &self,
        input: &fs::File,
        input_size: u64,
        selected: Option<(&Path, &str)>,
        passphrase: Option<&str>,
    ) -> Result<ArchiveScan, &'static str> {
        // SAFETY: every cached symbol uses its public libarchive C ABI
        // and the reader/entry pointers remain owned by the guarded archive
        // reader.
        unsafe {
            let new_reader = self.symbols.new_reader;
            let free_reader = self.symbols.free_reader;
            let reader = new_reader();
            if reader.is_null() {
                return Err("archive_extraction_failed");
            }
            let reader = ReaderGuard {
                reader,
                free: free_reader,
            };
            register_readers(&self.symbols, reader.reader)?;
            let passphrase = passphrase.map(|value| {
                let mut encoded = Zeroizing::new(Vec::with_capacity(value.len() + 1));
                encoded.extend_from_slice(value.as_bytes());
                encoded.push(0);
                encoded
            });
            if let Some(passphrase) = &passphrase
                && (self.symbols.add_passphrase)(reader.reader, passphrase.as_ptr().cast())
                    != ARCHIVE_OK
            {
                return Err("archive_passphrase_invalid");
            }
            let open = self.symbols.open_fd;
            if open(reader.reader, input.as_raw_fd(), 64 * 1024) < ARCHIVE_WARN {
                return Err("archive_extraction_failed");
            }
            let next_header = self.symbols.next_header;
            let read_data = self.symbols.read_data;
            let skip_data = self.symbols.skip_data;
            let pathname = self.symbols.pathname;
            let hardlink = self.symbols.hardlink;
            let symlink = self.symbols.symlink;
            let filetype = self.symbols.filetype;
            let size_is_set = self.symbols.size_is_set;
            let entry_size = self.symbols.entry_size;
            let entry_encrypted = self.symbols.entry_encrypted;

            let mut paths = BTreeSet::new();
            let mut entries = 0_usize;
            let mut logical_bytes = 0_u64;
            let mut extracted = None;
            let mut catalog = Vec::new();
            loop {
                let mut entry = ptr::null_mut();
                match next_header(reader.reader, &mut entry) {
                    ARCHIVE_EOF => break,
                    ARCHIVE_WARN..=ARCHIVE_OK if !entry.is_null() => {}
                    _ if passphrase.is_some() => return Err("archive_password_invalid"),
                    _ => return Err("archive_integrity_failed"),
                }
                entries += 1;
                if entries > MAX_ARCHIVE_ENTRIES {
                    return Err("archive_budget_exceeded");
                }
                let encrypted = entry_encrypted(entry) != 0;
                let kind = filetype(entry);
                if kind != REGULAR_FILE
                    || size_is_set(entry) == 0
                    || !hardlink(entry).is_null()
                    || !symlink(entry).is_null()
                {
                    if skip_data(reader.reader) < ARCHIVE_WARN {
                        return Err("archive_integrity_failed");
                    }
                    continue;
                }
                let size =
                    u64::try_from(entry_size(entry)).map_err(|_| "archive_budget_exceeded")?;
                logical_bytes = logical_bytes
                    .checked_add(size)
                    .ok_or("archive_budget_exceeded")?;
                if logical_bytes > MAX_ARCHIVE_OUTPUT_BYTES
                    || logical_bytes > input_size * MAX_COMPRESSION_RATIO
                {
                    return Err("archive_budget_exceeded");
                }

                let raw_path = pathname(entry);
                let normalized = if raw_path.is_null() {
                    None
                } else {
                    CStr::from_ptr(raw_path)
                        .to_str()
                        .ok()
                        .and_then(|path| crate::archive::normalize_archive_path(path).ok())
                };
                let Some(normalized) = normalized else {
                    if skip_data(reader.reader) < ARCHIVE_WARN {
                        return Err("archive_integrity_failed");
                    }
                    continue;
                };
                if !paths.insert(normalized.clone()) {
                    return Err("archive_path_conflict");
                }
                let selected_output = selected
                    .filter(|(_, selected_path)| normalized == *selected_path)
                    .map(|(output, _)| output);
                let catalog_kind = (selected.is_none() && size > 0)
                    .then(|| crate::inspect::classify_path(&normalized))
                    .flatten();
                if encrypted
                    && passphrase.is_none()
                    && (selected_output.is_some() || catalog_kind.is_some())
                {
                    return Err("archive_password_required");
                }
                if let Some(kind) = catalog_kind {
                    catalog.push(CatalogEntry {
                        relative_path: normalized.clone(),
                        exact_size: size,
                        kind,
                    });
                }
                if let Some(output) = selected_output {
                    extracted = Some(extract_entry(
                        reader.reader,
                        read_data,
                        output,
                        size,
                        encrypted,
                    )?);
                } else if skip_data(reader.reader) < ARCHIVE_WARN {
                    return Err(if encrypted && passphrase.is_some() {
                        "archive_password_invalid"
                    } else {
                        "archive_integrity_failed"
                    });
                }
            }
            if selected.is_none() {
                catalog.sort_by(|left, right| {
                    left.relative_path
                        .cmp(&right.relative_path)
                        .then_with(|| left.exact_size.cmp(&right.exact_size))
                        .then_with(|| left.kind.cmp(&right.kind))
                });
                validate_catalog(&catalog)?;
            }
            Ok(ArchiveScan {
                catalog,
                extracted,
                entries,
                logical_bytes,
            })
        }
    }
}

fn seal_worker_output(
    output: &Path,
    minimum_size: u64,
    maximum_size: u64,
    error: &'static str,
) -> Result<fs::File, &'static str> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(output)
        .map_err(|_| error)?;
    let metadata = file.metadata().map_err(|_| error)?;
    if !metadata.file_type().is_file()
        || !(minimum_size..=maximum_size).contains(&metadata.len())
        || metadata.permissions().mode() & 0o777 != 0o600
        || metadata.nlink() != 1
        || metadata.uid() != unsafe { libc::getuid() }
        || metadata.gid() != unsafe { libc::getgid() }
    {
        return Err(error);
    }
    file.set_permissions(fs::Permissions::from_mode(0o400))
        .and_then(|_| file.sync_all())
        .map_err(|_| error)?;
    Ok(file)
}

pub(crate) fn worker(arguments: &[String]) -> i32 {
    let (catalog, library, input, output, selected, read_passphrase) = match arguments {
        [
            worker,
            library_flag,
            library,
            input_flag,
            input,
            output_flag,
            output,
            selected_flag,
            selected,
        ] if worker == "--archive-worker"
            && library_flag == "--libarchive-library"
            && input_flag == "--input"
            && output_flag == "--output"
            && selected_flag == "--selected-path" =>
        {
            (false, library, input, output, Some(selected), false)
        }
        [
            worker,
            library_flag,
            library,
            input_flag,
            input,
            output_flag,
            output,
            selected_flag,
            selected,
            passphrase_flag,
        ] if worker == "--archive-worker"
            && library_flag == "--libarchive-library"
            && input_flag == "--input"
            && output_flag == "--output"
            && selected_flag == "--selected-path"
            && passphrase_flag == "--passphrase-stdin" =>
        {
            (false, library, input, output, Some(selected), true)
        }
        [
            worker,
            library_flag,
            library,
            input_flag,
            input,
            output_flag,
            output,
        ] if worker == "--archive-catalog-worker"
            && library_flag == "--libarchive-library"
            && input_flag == "--input"
            && output_flag == "--output" =>
        {
            (true, library, input, output, None, false)
        }
        [
            worker,
            library_flag,
            library,
            input_flag,
            input,
            output_flag,
            output,
            passphrase_flag,
        ] if worker == "--archive-catalog-worker"
            && library_flag == "--libarchive-library"
            && input_flag == "--input"
            && output_flag == "--output"
            && passphrase_flag == "--passphrase-stdin" =>
        {
            (true, library, input, output, None, true)
        }
        _ => return 64,
    };
    let passphrase = if read_passphrase {
        match read_worker_passphrase() {
            Ok(passphrase) => Some(passphrase),
            Err(_) => return 28,
        }
    } else {
        None
    };
    let result = Runtime::validate(Path::new(library)).and_then(|runtime| {
        if catalog {
            runtime
                .write_catalog_unsealed(
                    Path::new(input),
                    Path::new(output),
                    passphrase.as_ref().map(|value| value.as_str()),
                )
                .map(|_| ())
        } else {
            runtime
                .extract_selected_unsealed(
                    Path::new(input),
                    Path::new(output),
                    selected.expect("validated archive worker selection"),
                    passphrase.as_ref().map(|value| value.as_str()),
                )
                .map(|_| ())
        }
    });
    match result {
        Ok(()) => 0,
        Err("archive_budget_exceeded") => 22,
        Err("archive_path_invalid" | "archive_path_conflict") => 23,
        Err("archive_selected_entry_missing") => 24,
        Err("archive_integrity_failed") => 25,
        Err("archive_catalog_invalid") => 26,
        Err("archive_password_required") => 27,
        Err("archive_password_invalid" | "archive_passphrase_invalid") => 28,
        Err(_) => 70,
    }
}

fn read_worker_passphrase() -> Result<Zeroizing<String>, &'static str> {
    read_passphrase(std::io::stdin())
}

fn read_passphrase(input: impl Read) -> Result<Zeroizing<String>, &'static str> {
    let mut encoded = Zeroizing::new(Vec::new());
    input
        .take(MAX_ARCHIVE_PASSPHRASE_BYTES as u64 + 1)
        .read_to_end(&mut encoded)
        .map_err(|_| "archive_passphrase_invalid")?;
    if encoded.len() > MAX_ARCHIVE_PASSPHRASE_BYTES || encoded.contains(&0) {
        return Err("archive_passphrase_invalid");
    }
    std::str::from_utf8(&encoded).map_err(|_| "archive_passphrase_invalid")?;
    let encoded = mem::take(&mut *encoded);
    // SAFETY: the exact bytes were validated as UTF-8 immediately above.
    Ok(Zeroizing::new(unsafe {
        String::from_utf8_unchecked(encoded)
    }))
}

impl Drop for Runtime {
    fn drop(&mut self) {
        // SAFETY: Runtime uniquely owns the non-null handle returned by dlopen.
        unsafe {
            libc::dlclose(self.handle);
        }
    }
}

struct ReaderGuard {
    reader: *mut c_void,
    free: ArchiveReadFree,
}

struct ReadinessStage(std::path::PathBuf);

impl Drop for ReadinessStage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

impl Drop for ReaderGuard {
    fn drop(&mut self) {
        // SAFETY: the guard owns the reader returned by archive_read_new.
        unsafe {
            (self.free)(self.reader);
        }
    }
}

// Resolves every libarchive symbol required by the runtime exactly once so
// later archive operations can invoke the cached function pointers instead of
// repeating the per-call `dlsym` hash lookup.
// SAFETY: `handle` must be a live `dlopen` result for the admitted library.
unsafe fn resolve_symbols(handle: *mut c_void) -> Result<Symbols, &'static str> {
    // SAFETY: each name is statically NUL-terminated and the matching public
    // libarchive C ABI declaration is used for its symbol.
    unsafe {
        Ok(Symbols {
            new_reader: symbol(handle, b"archive_read_new\0")?,
            free_reader: symbol(handle, b"archive_read_free\0")?,
            open_fd: symbol(handle, b"archive_read_open_fd\0")?,
            add_passphrase: symbol(handle, b"archive_read_add_passphrase\0")?,
            next_header: symbol(handle, b"archive_read_next_header\0")?,
            read_data: symbol(handle, b"archive_read_data\0")?,
            skip_data: symbol(handle, b"archive_read_data_skip\0")?,
            pathname: symbol(handle, b"archive_entry_pathname_utf8\0")?,
            hardlink: symbol(handle, b"archive_entry_hardlink\0")?,
            symlink: symbol(handle, b"archive_entry_symlink\0")?,
            filetype: symbol(handle, b"archive_entry_filetype\0")?,
            size_is_set: symbol(handle, b"archive_entry_size_is_set\0")?,
            entry_size: symbol(handle, b"archive_entry_size\0")?,
            entry_encrypted: symbol(handle, b"archive_entry_is_encrypted\0")?,
            support_filter_all: symbol(handle, b"archive_read_support_filter_all\0")?,
            support_format_all: symbol(handle, b"archive_read_support_format_all\0")?,
        })
    }
}

unsafe fn register_readers(symbols: &Symbols, reader: *mut c_void) -> Result<(), &'static str> {
    for register in [symbols.support_filter_all, symbols.support_format_all] {
        // SAFETY: each cached symbol has the common public reader-registration
        // ABI and reader was allocated by archive_read_new for this handle.
        if unsafe { register(reader) } < ARCHIVE_WARN {
            return Err("libarchive_readiness_failed");
        }
    }
    Ok(())
}

fn validate_catalog(catalog: &[CatalogEntry]) -> Result<(), &'static str> {
    if catalog.len() > MAX_ARCHIVE_ENTRIES {
        return Err("archive_budget_exceeded");
    }
    let mut paths = BTreeSet::new();
    for entry in catalog {
        let normalized = crate::archive::normalize_archive_path(&entry.relative_path)
            .map_err(|_| "archive_catalog_invalid")?;
        if entry.exact_size == 0
            || entry.exact_size > MAX_ARCHIVE_OUTPUT_BYTES
            || normalized != entry.relative_path
            || !paths.insert(&entry.relative_path)
        {
            return Err("archive_catalog_invalid");
        }
    }
    Ok(())
}

fn validate_census(census: &CatalogCensus) -> Result<(), &'static str> {
    if census.entries > MAX_ARCHIVE_ENTRIES
        || census.catalog.len() > census.entries
        || census.logical_bytes > MAX_ARCHIVE_OUTPUT_BYTES
    {
        return Err("archive_budget_exceeded");
    }
    let catalog_bytes = census.catalog.iter().try_fold(0_u64, |total, entry| {
        total
            .checked_add(entry.exact_size)
            .ok_or("archive_budget_exceeded")
    })?;
    if catalog_bytes > census.logical_bytes {
        return Err("archive_catalog_invalid");
    }
    validate_catalog(&census.catalog)
}

unsafe fn extract_entry(
    reader: *mut c_void,
    read_data: ArchiveReadData,
    output: &Path,
    expected_size: u64,
    encrypted: bool,
) -> Result<ExtractedEntry, &'static str> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(output)
        .map_err(|_| "archive_output_failed")?;
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        // SAFETY: buffer is writable for its declared length and reader points
        // to the currently selected entry.
        let length = unsafe { read_data(reader, buffer.as_mut_ptr().cast(), buffer.len()) };
        if length == 0 {
            break;
        }
        if length < 0 {
            return Err(if encrypted {
                "archive_password_invalid"
            } else {
                "archive_integrity_failed"
            });
        }
        let length = usize::try_from(length).map_err(|_| "archive_integrity_failed")?;
        size = size
            .checked_add(u64::try_from(length).map_err(|_| "archive_budget_exceeded")?)
            .ok_or("archive_budget_exceeded")?;
        if size > expected_size || size > MAX_ARCHIVE_OUTPUT_BYTES {
            return Err("archive_budget_exceeded");
        }
        file.write_all(&buffer[..length])
            .map_err(|_| "archive_output_failed")?;
        digest.update(&buffer[..length]);
    }
    if size != expected_size {
        return Err("archive_integrity_failed");
    }
    file.flush().map_err(|_| "archive_output_failed")?;
    Ok(ExtractedEntry {
        identity: format!("{:x}", digest.finalize()),
        size,
    })
}

fn terminate_worker(
    child: &mut std::process::Child,
    outcome: &'static str,
) -> Result<(), &'static str> {
    crate::child_process::terminate_group(child, Duration::from_secs(2))
        .map_err(|_| "archive_worker_failed")?;
    Err(outcome)
}

fn cleanup_failed_output<T>(
    output: &Path,
    result: Result<T, &'static str>,
) -> Result<T, &'static str> {
    if result.is_err() {
        drop(fs::remove_file(output));
    }
    result
}

unsafe fn symbol<T: Copy>(handle: *mut c_void, name: &'static [u8]) -> Result<T, &'static str> {
    debug_assert_eq!(name.last(), Some(&0));
    // SAFETY: name is statically NUL-terminated and handle is live.
    let address = unsafe { libc::dlsym(handle, name.as_ptr().cast()) };
    if address.is_null() {
        return Err("libarchive_symbol_missing");
    }
    debug_assert_eq!(mem::size_of::<T>(), mem::size_of::<*mut c_void>());
    // SAFETY: callers choose the function-pointer type matching the named
    // public libarchive symbol.
    Ok(unsafe { mem::transmute_copy(&address) })
}

fn open_archive_input(input: &Path) -> Result<(fs::File, ImmutableFileIdentity), &'static str> {
    if !canonical_absolute(input) {
        return Err("archive_input_invalid");
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(input)
        .map_err(|_| "archive_input_invalid")?;
    let metadata = file.metadata().map_err(|_| "archive_input_invalid")?;
    if !sealed_file_metadata(&metadata, metadata.len())
        || metadata.uid() != unsafe { libc::getuid() }
        || metadata.gid() != unsafe { libc::getgid() }
    {
        return Err("archive_input_invalid");
    }
    let identity = immutable_file_identity(&metadata);
    Ok((file, identity))
}

fn revalidate_archive_input(
    input: &fs::File,
    expected: ImmutableFileIdentity,
) -> Result<(), &'static str> {
    let metadata = input.metadata().map_err(|_| "archive_integrity_failed")?;
    if !sealed_file_metadata(&metadata, expected.size)
        || immutable_file_identity(&metadata) != expected
    {
        return Err("archive_integrity_failed");
    }
    Ok(())
}

fn validate_archive_output(output: &Path) -> Result<(), &'static str> {
    if !output.is_absolute()
        || output
            .components()
            .any(|component| !matches!(component, Component::RootDir | Component::Normal(_)))
    {
        return Err("archive_output_invalid");
    }
    let parent = output.parent().ok_or("archive_output_invalid")?;
    if !canonical_absolute(parent) {
        return Err("archive_output_invalid");
    }
    let metadata = fs::symlink_metadata(parent).map_err(|_| "archive_output_invalid")?;
    if !metadata.file_type().is_dir() || metadata.permissions().mode() & 0o077 != 0 {
        return Err("archive_output_invalid");
    }
    Ok(())
}

fn canonical_absolute(path: &Path) -> bool {
    path.is_absolute()
        && !path
            .components()
            .any(|component| !matches!(component, Component::RootDir | Component::Normal(_)))
        && fs::canonicalize(path).is_ok_and(|canonical| canonical == path)
}

fn readiness_tar(path: &str, content: &[u8]) -> Vec<u8> {
    let mut header = [0_u8; 512];
    header[..path.len()].copy_from_slice(path.as_bytes());
    write_tar_octal(&mut header[100..108], 0o400);
    write_tar_octal(&mut header[108..116], 0);
    write_tar_octal(&mut header[116..124], 0);
    write_tar_octal(&mut header[124..136], content.len() as u64);
    write_tar_octal(&mut header[136..148], 0);
    header[148..156].fill(b' ');
    header[156] = b'0';
    header[257..263].copy_from_slice(b"ustar\0");
    header[263..265].copy_from_slice(b"00");
    let checksum: u64 = header.iter().map(|byte| u64::from(*byte)).sum();
    let encoded = format!("{checksum:06o}\0 ");
    header[148..156].copy_from_slice(encoded.as_bytes());
    let padded = content.len().div_ceil(512) * 512;
    let mut archive = Vec::with_capacity(512 + padded + 1024);
    archive.extend_from_slice(&header);
    archive.extend_from_slice(content);
    archive.resize(512 + padded + 1024, 0);
    archive
}

fn write_tar_octal(field: &mut [u8], value: u64) {
    let encoded = format!("{value:0width$o}\0", width = field.len() - 1);
    assert_eq!(encoded.len(), field.len(), "readiness TAR field fits");
    field.copy_from_slice(encoded.as_bytes());
}

fn validate_library_path(library: &Path) -> Result<(), &'static str> {
    if !library.is_absolute() {
        return Err("libarchive_library_invalid");
    }
    let metadata = fs::metadata(library).map_err(|_| "libarchive_library_unavailable")?;
    let mode = metadata.permissions().mode();
    if !metadata.file_type().is_file() || mode & 0o022 != 0 || mode & 0o6000 != 0 {
        return Err("libarchive_library_invalid");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{Runtime, open_archive_input, revalidate_archive_input, validate_library_path};
    use sha2::Digest;
    use std::fs;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct Fixture {
        root: PathBuf,
    }

    impl Fixture {
        fn new(label: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system time")
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "comet-libarchive-{label}-{}-{nonce}",
                std::process::id()
            ));
            fs::create_dir(&root).expect("create fixture directory");
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .expect("secure fixture directory");
            Self { root }
        }

        fn library(&self, omit_format_all: bool, failure: bool) -> PathBuf {
            self.library_with_encryption(omit_format_all, failure, 0)
        }

        fn library_with_encryption(
            &self,
            omit_format_all: bool,
            failure: bool,
            entry_encryption: i32,
        ) -> PathBuf {
            let source = self.root.join("fixture.c");
            let library = self.root.join("libarchive.so.13");
            let format_all = if omit_format_all {
                ""
            } else {
                "int archive_read_support_format_all(void *p) { return ready(p); }"
            };
            fs::write(
                &source,
                format!(
                    r#"
                    #include <stdlib.h>
                    #include <string.h>
                    static int header_seen;
                    static int data_seen;
                    static int passphrase_ok;
                    void *archive_read_new(void) {{
                        passphrase_ok = 0;
                        return malloc(1);
                    }}
                    int archive_read_free(void *p) {{ free(p); return 0; }}
                    static int ready(void *p) {{ return p ? {result} : -1; }}
                    int archive_read_support_filter_all(void *p) {{ return ready(p); }}
                    {format_all}
                    int archive_read_add_passphrase(void *p, const char *passphrase) {{
                        if (!p || !passphrase) return -30;
                        passphrase_ok = strcmp(passphrase, "archive-secret") == 0;
                        return 0;
                    }}
                    int archive_read_open_fd(void *p, int fd, size_t block) {{
                        header_seen = 0; data_seen = 0;
                        return p && fd >= 0 && block ? 0 : -30;
                    }}
                    int archive_read_next_header(void *p, void **entry) {{
                        if (!p || !entry) return -30;
                        if (header_seen == 2) return 1;
                        header_seen++; data_seen = 0; *entry = (void *)2; return 0;
                    }}
                    const char *archive_entry_pathname_utf8(void *entry) {{
                        if (!entry) return NULL;
                        return header_seen == 1 ? "../ignored.bin" : "Movie.2026.mkv";
                    }}
                    const char *archive_entry_hardlink(void *entry) {{ return NULL; }}
                    const char *archive_entry_symlink(void *entry) {{ return NULL; }}
                    unsigned int archive_entry_filetype(void *entry) {{ return 0100000; }}
                    int archive_entry_size_is_set(void *entry) {{ return entry ? 4 : 0; }}
                    long long archive_entry_size(void *entry) {{ return entry ? 3 : -1; }}
                    int archive_entry_is_encrypted(void *entry) {{ return {entry_encryption}; }}
                    long archive_read_data(void *p, void *buffer, size_t length) {{
                        if (!p || !buffer) return -30;
                        if ({entry_encryption} && !passphrase_ok) return -30;
                        if (data_seen) return 0;
                        if (length < 3) return -30;
                        memcpy(buffer, "ABC", 3); data_seen = 1; return 3;
                    }}
                    int archive_read_data_skip(void *p) {{ return p ? 0 : -30; }}
                    "#,
                    result = if failure { -30 } else { 0 },
                ),
            )
            .expect("write fixture source");
            let status = Command::new("cc")
                .args(["-shared", "-fPIC", "-o"])
                .arg(&library)
                .arg(&source)
                .status()
                .expect("compile fixture library");
            assert!(status.success());
            library
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn admits_a_working_library_with_the_required_reader_set() {
        let fixture = Fixture::new("ready");
        let library = fixture.library(false, false);
        Runtime::validate(&library).expect("valid fixture");
    }

    #[test]
    fn extracts_only_the_exact_selected_entry_to_an_immutable_file() {
        let fixture = Fixture::new("extract");
        let library = fixture.library(false, false);
        let runtime = Runtime::validate(&library).expect("valid fixture");
        let input = fixture.root.join("input.tar");
        fs::write(&input, b"fake archive").expect("write fixture archive");
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400))
            .expect("secure fixture archive");
        let output = fixture.root.join("output.bin");

        let extracted = runtime
            .extract_selected(&input, &output, "Movie.2026.mkv")
            .expect("extract selected fixture");

        assert_eq!(extracted.size, 3);
        assert_eq!(
            extracted.identity,
            format!("{:x}", sha2::Sha256::digest(b"ABC"))
        );
        assert_eq!(fs::read(&output).expect("read extracted fixture"), b"ABC");
        assert_eq!(
            fs::metadata(&output)
                .expect("stat extracted fixture")
                .permissions()
                .mode()
                & 0o777,
            0o400
        );

        let missing = fixture.root.join("missing.bin");
        assert_eq!(
            runtime.extract_selected(&input, &missing, "other.bin"),
            Err("archive_selected_entry_missing")
        );
        assert!(!missing.exists());
    }

    #[test]
    fn encrypted_entries_require_and_verify_a_bounded_passphrase() {
        let fixture = Fixture::new("encrypted-entry");
        let library = fixture.library_with_encryption(false, false, 1);
        let runtime = Runtime::validate(&library).expect("valid fixture");
        let input = fixture.root.join("input.rar");
        fs::write(&input, b"fake archive").expect("write fixture archive");
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400))
            .expect("secure fixture archive");

        let missing = fixture.root.join("missing.bin");
        assert_eq!(
            runtime.extract_selected(&input, &missing, "Movie.2026.mkv"),
            Err("archive_password_required")
        );
        assert!(!missing.exists());

        let wrong = fixture.root.join("wrong.bin");
        assert_eq!(
            runtime.extract_selected_unsealed(
                &input,
                &wrong,
                "Movie.2026.mkv",
                Some("wrong-secret"),
            ),
            Err("archive_password_invalid")
        );
        assert!(!wrong.exists());

        let output = fixture.root.join("output.bin");
        let extracted = runtime
            .extract_selected_unsealed(&input, &output, "Movie.2026.mkv", Some("archive-secret"))
            .expect("extract encrypted entry");
        assert_eq!(extracted.size, 3);
        assert_eq!(fs::read(output).expect("read encrypted output"), b"ABC");
    }

    #[test]
    fn catalogs_only_typed_normalized_members_to_a_bounded_immutable_file() {
        let fixture = Fixture::new("catalog");
        let library = fixture.library(false, false);
        let runtime = Runtime::validate(&library).expect("valid fixture");
        let input = fixture.root.join("input.tar");
        fs::write(&input, b"fake archive").expect("write fixture archive");
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400))
            .expect("secure fixture archive");
        let output = fixture.root.join("catalog.json");

        let catalog = runtime
            .write_catalog(&input, &output)
            .expect("catalog archive fixture");

        assert_eq!(
            catalog.catalog,
            vec![super::CatalogEntry {
                relative_path: "Movie.2026.mkv".to_owned(),
                exact_size: 3,
                kind: crate::inspect::AssetKind::Video,
            }],
        );
        assert_eq!(catalog.entries, 2);
        assert_eq!(catalog.logical_bytes, 6);
        assert_eq!(
            serde_json::from_slice::<super::CatalogCensus>(
                &fs::read(&output).expect("read archive catalog")
            )
            .expect("decode archive catalog"),
            catalog
        );
        let metadata = fs::metadata(output).expect("stat archive catalog");
        assert_eq!(metadata.permissions().mode() & 0o777, 0o400);
        assert!(metadata.len() <= super::MAX_ARCHIVE_CATALOG_BYTES);
    }

    #[test]
    fn resolves_the_generic_reader_set_and_executes_it_only_when_used() {
        let complete = Fixture::new("complete-reader");
        Runtime::validate(&complete.library(false, false)).expect("accept fixture");
        let missing = Fixture::new("missing-reader");
        assert_eq!(
            Runtime::validate(&missing.library(true, false))
                .err()
                .expect("reject missing reader"),
            "libarchive_symbol_missing"
        );
        let failed = Fixture::new("failed-reader");
        let runtime =
            Runtime::validate(&failed.library(false, true)).expect("resolve broken reader fixture");
        let input = failed.root.join("input.tar");
        fs::write(&input, b"fake archive").expect("write fixture archive");
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400))
            .expect("secure fixture archive");
        assert_eq!(
            runtime.write_catalog(&input, &failed.root.join("catalog.json")),
            Err("libarchive_readiness_failed")
        );
    }

    #[test]
    fn accepts_supported_library_paths_but_rejects_relative_and_writable_targets() {
        assert_eq!(
            validate_library_path(Path::new("libarchive.so.13")),
            Err("libarchive_library_invalid")
        );
        let fixture = Fixture::new("unsafe-paths");
        let library = fixture.library(false, false);
        fs::set_permissions(&library, fs::Permissions::from_mode(0o666))
            .expect("make library writable");
        assert_eq!(
            validate_library_path(&library),
            Err("libarchive_library_invalid")
        );
        fs::set_permissions(&library, fs::Permissions::from_mode(0o644)).expect("secure library");
        let linked = fixture.root.join("linked.so");
        symlink(&library, &linked).expect("create symlink");
        validate_library_path(&linked).expect("accept library symlink");
        let nested = fixture.root.join("nested");
        fs::create_dir(&nested).expect("create lexical path fixture");
        validate_library_path(&nested.join("..").join("libarchive.so.13"))
            .expect("accept absolute library path with parent component");
    }

    #[test]
    fn rejects_mutable_or_aliased_archive_inputs_and_detects_identity_changes() {
        let fixture = Fixture::new("unsafe-inputs");
        let input = fixture.root.join("input.tar");
        fs::write(&input, b"archive").expect("write archive input");
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400)).expect("seal archive input");
        let (input_file, identity) =
            open_archive_input(&input).expect("valid sealed archive input");

        let alias = fixture.root.join("input-alias.tar");
        fs::hard_link(&input, &alias).expect("create archive input alias");
        assert_eq!(
            open_archive_input(&input).err(),
            Some("archive_input_invalid")
        );
        assert_eq!(
            revalidate_archive_input(&input_file, identity),
            Err("archive_integrity_failed")
        );
        fs::remove_file(alias).expect("remove archive input alias");

        fs::set_permissions(&input, fs::Permissions::from_mode(0o600))
            .expect("make archive input writable");
        assert_eq!(
            open_archive_input(&input).err(),
            Some("archive_input_invalid")
        );
    }

    #[test]
    fn unused_aggregate_encryption_metadata_is_not_required() {
        let fixture = Fixture::new("without-aggregate-encryption");
        let runtime = Runtime::validate(&fixture.library_with_encryption(false, false, 0))
            .expect("valid encryption-aware fixture");
        let input = fixture.root.join("input.tar");
        fs::write(&input, b"fake archive").expect("write fixture archive");
        fs::set_permissions(&input, fs::Permissions::from_mode(0o400))
            .expect("secure fixture archive");
        let output = fixture.root.join("catalog.json");
        runtime
            .write_catalog(&input, &output)
            .expect("catalog entry without aggregate encryption metadata");
    }

    #[test]
    fn worker_never_accepts_a_passphrase_value_in_its_arguments() {
        assert_eq!(super::worker(&[]), 64);
        assert_eq!(
            super::worker(
                &[
                    "--archive-worker",
                    "--libarchive-library",
                    "/library",
                    "--input",
                    "/input",
                    "--output",
                    "/output",
                    "--password",
                    "secret",
                ]
                .map(str::to_owned)
            ),
            64
        );
        assert_eq!(
            super::worker(
                &[
                    "--archive-catalog-worker",
                    "--libarchive-library",
                    "/library",
                    "--input",
                    "/input",
                    "--output",
                    "/output",
                    "--selected-path",
                    "movie.mkv",
                ]
                .map(str::to_owned)
            ),
            64
        );
    }

    #[test]
    fn passphrase_pipe_reader_is_exact_utf8_and_bounded() {
        assert_eq!(
            super::read_passphrase("archive-secret".as_bytes())
                .expect("read bounded passphrase")
                .as_str(),
            "archive-secret"
        );
        assert!(
            super::read_passphrase("".as_bytes())
                .expect("preserve an empty passphrase")
                .is_empty()
        );
        assert_eq!(
            super::read_passphrase("line one\nline two".as_bytes())
                .expect("preserve passphrase bytes")
                .as_str(),
            "line one\nline two"
        );
        assert_eq!(
            super::read_passphrase("bad\0secret".as_bytes()).err(),
            Some("archive_passphrase_invalid")
        );
        assert_eq!(
            super::read_passphrase(vec![b'x'; super::MAX_ARCHIVE_PASSPHRASE_BYTES + 1].as_slice())
                .err(),
            Some("archive_passphrase_invalid")
        );
    }
}
