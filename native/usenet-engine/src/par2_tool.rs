use md5::{Digest, Md5};
use std::ffi::{OsStr, OsString};
use std::fs;
use std::io::Read;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const MEMORY_BYTES: u64 = 512 * 1024 * 1024;
const MAX_STAGE_ENTRIES: usize = 25_000;
const MAX_OPEN_FILES: u64 = 64;
const CPU_SECONDS: u64 = 30 * 60;

pub(crate) struct Tool {
    binary: PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum RepairFailureKind {
    Cancelled,
    TimedOut,
    Insufficient,
    Corrupt,
    DiskExhausted,
    OutOfMemory,
    Io,
    Crashed,
}

#[derive(Clone, Copy)]
struct ExecutionLimits {
    wall_time: Duration,
    termination_grace: Duration,
    stage_bytes: u64,
}

impl Tool {
    pub(crate) fn validate(binary: &Path) -> Result<Self, &'static str> {
        if !binary.is_absolute() {
            return Err("par2_binary_invalid");
        }
        let metadata = fs::metadata(binary).map_err(|_| "par2_binary_unavailable")?;
        let mode = metadata.permissions().mode();
        if !metadata.file_type().is_file()
            || mode & 0o111 == 0
            || mode & 0o022 != 0
            || mode & 0o6000 != 0
        {
            return Err("par2_binary_invalid");
        }

        Ok(Self {
            binary: binary.to_path_buf(),
        })
    }

    pub(crate) fn repair<F>(
        &self,
        stage: &Path,
        stage_bytes: u64,
        cancelled: &F,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        self.repair_with_limits(
            stage,
            cancelled,
            ExecutionLimits {
                wall_time: Duration::from_secs(30 * 60),
                termination_grace: Duration::from_secs(2),
                stage_bytes,
            },
        )
        .map_err(|failure| match failure {
            RepairFailureKind::Cancelled => "repair_cancelled",
            RepairFailureKind::TimedOut => "repair_timed_out",
            RepairFailureKind::Insufficient => "repair_insufficient",
            RepairFailureKind::Corrupt => "repair_corrupt",
            RepairFailureKind::DiskExhausted => "repair_disk_exhausted",
            RepairFailureKind::OutOfMemory => "repair_memory_exhausted",
            RepairFailureKind::Io => "repair_io_failed",
            RepairFailureKind::Crashed => "repair_worker_failed",
        })
    }

    fn repair_with_limits<F>(
        &self,
        stage: &Path,
        cancelled: F,
        limits: ExecutionLimits,
    ) -> Result<(), RepairFailureKind>
    where
        F: Fn() -> bool,
    {
        validate_stage(stage, limits.stage_bytes)?;
        let threads = std::thread::available_parallelism()
            .map_err(|_| RepairFailureKind::Crashed)?
            .get()
            .min(2);
        let index = stage.join("index.par2");
        let mut base = OsString::from("-B");
        base.push(stage);
        let mut command = Command::new(&self.binary);
        command
            .arg("repair")
            .arg("-q")
            .arg("-q")
            .arg("-m512")
            .arg(format!("-t{threads}"))
            .arg("-T1")
            .arg(base)
            .arg("--")
            .arg(&index)
            .current_dir(stage)
            .env_clear()
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let sandbox = crate::child_process::Sandbox::new(
            crate::child_process::Limits {
                cpu_seconds: CPU_SECONDS,
                memory_bytes: MEMORY_BYTES,
                file_size_bytes: limits.stage_bytes,
                open_files: MAX_OPEN_FILES,
            },
            Some(stage),
        )
        .map_err(|_| RepairFailureKind::Crashed)?;
        // SAFETY: the hook performs only async-signal-safe libc operations.
        unsafe {
            command.pre_exec(move || sandbox.apply());
        }
        let mut child = command.spawn().map_err(|_| RepairFailureKind::Crashed)?;
        let output = OutputDrain::new(&mut child);
        let started = Instant::now();
        let outcome = loop {
            match crate::child_process::try_wait_group(&mut child) {
                Ok(Some(status)) => break Ok(status),
                Ok(None) if cancelled() => {
                    break match crate::child_process::terminate_group(
                        &mut child,
                        limits.termination_grace,
                    ) {
                        Ok(_) => Err(RepairFailureKind::Cancelled),
                        Err(_) => Err(RepairFailureKind::Crashed),
                    };
                }
                Ok(None) if started.elapsed() >= limits.wall_time => {
                    break match crate::child_process::terminate_group(
                        &mut child,
                        limits.termination_grace,
                    ) {
                        Ok(_) => Err(RepairFailureKind::TimedOut),
                        Err(_) => Err(RepairFailureKind::Crashed),
                    };
                }
                Ok(None) => thread::sleep(Duration::from_millis(10)),
                Err(_) => {
                    break match crate::child_process::terminate_group(
                        &mut child,
                        limits.termination_grace,
                    ) {
                        Ok(_) | Err(_) => Err(RepairFailureKind::Crashed),
                    };
                }
            }
        };
        output.finish()?;
        let status = outcome?;
        if status.success() {
            return Ok(());
        }
        Err(classify_failure(status))
    }

    pub(crate) fn verify_repair_engine(&self, local_data: &Path) -> Result<(), &'static str> {
        cleanup_readiness_stages(local_data)?;
        let mut nonce = [0u8; 8];
        getrandom::fill(&mut nonce).map_err(|_| "par2_readiness_failed")?;
        let stage = local_data.join(format!(
            ".par2-readiness-{}-{:016x}",
            std::process::id(),
            u64::from_ne_bytes(nonce)
        ));
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&stage)
            .map_err(|_| "par2_readiness_failed")?;
        let result = (|| {
            let index = stage.join("index.par2");
            let mut output = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(index)
                .map_err(|_| "par2_readiness_failed")?;
            use std::io::Write;
            output
                .write_all(&readiness_recovery_set())
                .map_err(|_| "par2_readiness_failed")?;
            self.repair_with_limits(
                &stage,
                || false,
                ExecutionLimits {
                    wall_time: Duration::from_secs(5),
                    termination_grace: Duration::from_secs(2),
                    stage_bytes: 1024 * 1024,
                },
            )
            .map_err(|_| "par2_readiness_failed")?;
            let repaired =
                fs::read(stage.join("readiness.bin")).map_err(|_| "par2_readiness_failed")?;
            if repaired != b"COMET-PAR2-READY" {
                return Err("par2_readiness_failed");
            }
            Ok(())
        })();
        fs::remove_dir_all(&stage).map_err(|_| "par2_readiness_failed")?;
        result
    }
}

fn readiness_stage_name(name: &OsStr) -> bool {
    let Some(name) = name.to_str() else {
        return false;
    };
    let Some((process_id, nonce)) = name
        .strip_prefix(".par2-readiness-")
        .and_then(|suffix| suffix.split_once('-'))
    else {
        return false;
    };
    !process_id.is_empty()
        && process_id.bytes().all(|byte| byte.is_ascii_digit())
        && nonce.len() == 16
        && nonce
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn cleanup_readiness_stages(local_data: &Path) -> Result<(), &'static str> {
    for entry in fs::read_dir(local_data).map_err(|_| "par2_readiness_failed")? {
        let entry = entry.map_err(|_| "par2_readiness_failed")?;
        if readiness_stage_name(&entry.file_name())
            && entry
                .file_type()
                .map_err(|_| "par2_readiness_failed")?
                .is_dir()
        {
            fs::remove_dir_all(entry.path()).map_err(|_| "par2_readiness_failed")?;
        }
    }
    Ok(())
}

fn readiness_recovery_set() -> Vec<u8> {
    const CONTENT: &[u8] = b"COMET-PAR2-READY";
    const NAME: &[u8] = b"readiness.bin";
    const SLICE_SIZE: u64 = 16;
    let content_md5: [u8; 16] = Md5::digest(CONTENT).into();
    let mut file_identity = Md5::new();
    file_identity.update(content_md5);
    file_identity.update((CONTENT.len() as u64).to_le_bytes());
    file_identity.update(NAME);
    let file_id: [u8; 16] = file_identity.finalize().into();

    let mut main = SLICE_SIZE.to_le_bytes().to_vec();
    main.extend_from_slice(&1u32.to_le_bytes());
    main.extend_from_slice(&file_id);
    let set_id: [u8; 16] = Md5::digest(&main).into();

    let mut description = file_id.to_vec();
    description.extend_from_slice(&content_md5);
    description.extend_from_slice(&content_md5);
    description.extend_from_slice(&(CONTENT.len() as u64).to_le_bytes());
    description.extend_from_slice(NAME);
    while !(64 + description.len()).is_multiple_of(4) {
        description.push(0);
    }
    let mut ifsc = file_id.to_vec();
    ifsc.extend_from_slice(&content_md5);
    ifsc.extend_from_slice(&crc32fast::hash(CONTENT).to_le_bytes());
    let mut recovery = 0u32.to_le_bytes().to_vec();
    recovery.extend_from_slice(CONTENT);

    let mut output = readiness_packet(b"PAR 2.0\0Main\0\0\0\0", set_id, &main);
    output.extend_from_slice(&readiness_packet(
        b"PAR 2.0\0FileDesc",
        set_id,
        &description,
    ));
    output.extend_from_slice(&readiness_packet(b"PAR 2.0\0IFSC\0\0\0\0", set_id, &ifsc));
    output.extend_from_slice(&readiness_packet(b"PAR 2.0\0RecvSlic", set_id, &recovery));
    output
}

fn readiness_packet(kind: &[u8; 16], set_id: [u8; 16], body: &[u8]) -> Vec<u8> {
    let mut packet = Vec::with_capacity(64 + body.len());
    packet.extend_from_slice(b"PAR2\0PKT");
    packet.extend_from_slice(&(64u64 + body.len() as u64).to_le_bytes());
    packet.extend_from_slice(&[0; 16]);
    packet.extend_from_slice(&set_id);
    packet.extend_from_slice(kind);
    packet.extend_from_slice(body);
    let digest: [u8; 16] = Md5::digest(&packet[32..]).into();
    packet[16..32].copy_from_slice(&digest);
    packet
}

fn validate_stage(stage: &Path, maximum_bytes: u64) -> Result<(), RepairFailureKind> {
    if !stage.is_absolute()
        || stage
            .components()
            .any(|component| !matches!(component, Component::RootDir | Component::Normal(_)))
    {
        return Err(RepairFailureKind::Io);
    }
    let user = unsafe { libc::getuid() };
    let group = unsafe { libc::getgid() };
    let root = fs::symlink_metadata(stage).map_err(|_| RepairFailureKind::Io)?;
    if !root.file_type().is_dir()
        || root.uid() != user
        || root.gid() != group
        || root.permissions().mode() & 0o777 != 0o700
    {
        return Err(RepairFailureKind::Io);
    }
    let index = stage.join("index.par2");
    let mut index_found = false;
    let mut directories = vec![stage.to_path_buf()];
    let mut entries = 0usize;
    let mut bytes = 0u64;
    while let Some(directory) = directories.pop() {
        for entry in fs::read_dir(directory).map_err(|_| RepairFailureKind::Io)? {
            let entry = entry.map_err(|_| RepairFailureKind::Io)?;
            entries += 1;
            if entries > MAX_STAGE_ENTRIES {
                return Err(RepairFailureKind::Io);
            }
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path).map_err(|_| RepairFailureKind::Io)?;
            if metadata.uid() != user || metadata.gid() != group {
                return Err(RepairFailureKind::Io);
            }
            if metadata.file_type().is_dir() {
                if metadata.permissions().mode() & 0o777 != 0o700 {
                    return Err(RepairFailureKind::Io);
                }
                directories.push(path);
            } else if metadata.file_type().is_file()
                && metadata.nlink() == 1
                && metadata.permissions().mode() & 0o6022 == 0
            {
                index_found |= path == index;
                bytes = bytes
                    .checked_add(metadata.len())
                    .ok_or(RepairFailureKind::Io)?;
                if bytes > maximum_bytes {
                    return Err(RepairFailureKind::Io);
                }
            } else {
                return Err(RepairFailureKind::Io);
            }
        }
    }
    if !index_found {
        return Err(RepairFailureKind::Io);
    }
    Ok(())
}

struct OutputDrain {
    readers: Vec<thread::JoinHandle<std::io::Result<()>>>,
}

impl OutputDrain {
    fn new(child: &mut Child) -> Self {
        let stdout = child.stdout.take().expect("PAR2 stdout is piped");
        let stderr = child.stderr.take().expect("PAR2 stderr is piped");
        let readers = [Box::new(stdout) as Box<dyn Read + Send>, Box::new(stderr)]
            .into_iter()
            .map(|mut reader| {
                thread::spawn(move || std::io::copy(&mut reader, &mut std::io::sink()).map(|_| ()))
            })
            .collect();
        Self { readers }
    }

    fn finish(self) -> Result<(), RepairFailureKind> {
        for reader in self.readers {
            reader
                .join()
                .map_err(|_| RepairFailureKind::Crashed)?
                .map_err(|_| RepairFailureKind::Crashed)?;
        }
        Ok(())
    }
}

fn classify_failure(status: ExitStatus) -> RepairFailureKind {
    if status.signal() == Some(libc::SIGXCPU) {
        RepairFailureKind::TimedOut
    } else if status.signal() == Some(libc::SIGXFSZ) {
        RepairFailureKind::DiskExhausted
    } else {
        match status.code() {
            Some(2) => RepairFailureKind::Insufficient,
            Some(4 | 5) => RepairFailureKind::Corrupt,
            Some(6) => RepairFailureKind::Io,
            Some(8) => RepairFailureKind::OutOfMemory,
            _ => RepairFailureKind::Crashed,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{ExecutionLimits, RepairFailureKind, Tool, readiness_recovery_set};
    use std::fs;
    use std::os::unix::ffi::OsStringExt;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::path::PathBuf;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    struct TemporaryDirectory(PathBuf);

    impl TemporaryDirectory {
        fn new(label: &str) -> Self {
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock")
                .as_nanos();
            let path = std::env::temp_dir()
                .join(format!("comet-par2-{label}-{}-{nonce}", std::process::id()));
            fs::create_dir(&path).expect("create temporary PAR2 directory");
            Self(path)
        }
    }

    impl Drop for TemporaryDirectory {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    struct Descriptor(libc::c_int);

    impl Drop for Descriptor {
        fn drop(&mut self) {
            unsafe {
                libc::close(self.0);
            }
        }
    }

    fn script(directory: &TemporaryDirectory, name: &str, body: &str) -> PathBuf {
        let path = directory.0.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write calculator fixture");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755))
            .expect("make calculator fixture executable");
        path
    }

    fn calculator(directory: &TemporaryDirectory, repair_body: &str) -> PathBuf {
        script(directory, "par2", repair_body)
    }

    fn stage(directory: &TemporaryDirectory) -> PathBuf {
        let stage = directory.0.join("stage");
        fs::create_dir(&stage).expect("create repair stage");
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o700))
            .expect("secure repair stage");
        fs::write(stage.join("index.par2"), b"PAR2").expect("write repair index");
        fs::set_permissions(stage.join("index.par2"), fs::Permissions::from_mode(0o600))
            .expect("secure repair index");
        stage
    }

    fn test_limits(wall_time: Duration) -> ExecutionLimits {
        ExecutionLimits {
            wall_time,
            termination_grace: Duration::from_millis(100),
            stage_bytes: 1024 * 1024,
        }
    }

    #[test]
    fn validation_accepts_a_safe_executable() {
        let directory = TemporaryDirectory::new("valid");
        let binary = script(&directory, "par2", "exit 0");

        Tool::validate(&binary).expect("validate calculator");
    }

    #[test]
    fn validation_accepts_supported_paths_but_rejects_unsafe_files() {
        let directory = TemporaryDirectory::new("invalid");
        let wrong = script(&directory, "wrong", "exit 0");
        assert!(Tool::validate(&wrong).is_ok());

        let writable = script(&directory, "writable", "exit 0");
        fs::set_permissions(&writable, fs::Permissions::from_mode(0o777))
            .expect("make unsafe calculator fixture");
        assert_eq!(Tool::validate(&writable).err(), Some("par2_binary_invalid"));

        let link = directory.0.join("link");
        symlink(&wrong, &link).expect("create calculator symlink");
        assert!(Tool::validate(&link).is_ok());
        let subdirectory = directory.0.join("subdirectory");
        fs::create_dir(&subdirectory).expect("create path component fixture");
        assert!(Tool::validate(&subdirectory.join("..").join("wrong")).is_ok());
        assert_eq!(
            Tool::validate(std::path::Path::new("relative/par2")).err(),
            Some("par2_binary_invalid")
        );
    }

    #[test]
    fn repair_uses_the_exact_argument_vector_and_sandbox() {
        let directory = TemporaryDirectory::new("sandbox");
        let inherited = unsafe { libc::open(c"/dev/null".as_ptr(), libc::O_RDONLY) };
        assert!(inherited >= 0);
        let inherited_high = unsafe { libc::fcntl(inherited, libc::F_DUPFD, 1000) };
        assert!(inherited_high >= 1000);
        unsafe {
            libc::close(inherited);
        }
        let _inherited = Descriptor(inherited_high);
        let outside_metadata = directory.0.join("outside-metadata");
        fs::write(&outside_metadata, b"unchanged").expect("write outside metadata fixture");
        fs::set_permissions(&outside_metadata, fs::Permissions::from_mode(0o600))
            .expect("secure outside metadata fixture");
        let binary = calculator(
            &directory,
            &format!(
                "printf '%s\\n' \"$@\" > invocation\n\
             printf 'pwd=%s\\n' \"$PWD\" >> invocation\n\
             /usr/bin/grep '^NoNewPrivs:' /proc/self/status >> invocation\n\
             printf 'umask=%s\\n' \"$(umask)\" >> invocation\n\
             if [ -n \"$COMET_TEST_SECRET\" ]; then exit 9; fi\n\
             if [ -e /proc/self/fd/{inherited_high} ]; then exit 10; fi\n\
             /usr/bin/python3 -c 'exec(\"import socket\\ntry:\\n socket.socket()\\nexcept OSError as error:\\n open(\\\"network\\\", \\\"w\\\").write(str(error.errno))\")'\n\
             /usr/bin/python3 -c 'import os; os.setsid()' 2>setsid-error && exit 11\n\
             if /usr/bin/touch ../outside-write 2>outside-write-error; then exit 12; fi\n\
             if /usr/bin/chmod 000 ../outside-metadata 2>chmod-error; then exit 13; fi\n\
             if /usr/bin/touch -m ../outside-metadata 2>touch-error; then exit 14; fi\n\
             : > created"
            ),
        );
        let stage = stage(&directory);
        let tool = Tool::validate(&binary).expect("validate calculator");

        tool.repair_with_limits(&stage, || false, test_limits(Duration::from_secs(5)))
            .expect("run sandboxed calculator");
        let invocation =
            fs::read_to_string(stage.join("invocation")).expect("read calculator invocation");
        let threads = std::thread::available_parallelism()
            .expect("query available parallelism")
            .get()
            .min(2);
        assert_eq!(
            invocation,
            format!(
                "repair\n-q\n-q\n-m512\n-t{threads}\n-T1\n-B{}\n--\n{}/index.par2\npwd={}\nNoNewPrivs:\t1\numask=0077\n",
                stage.display(),
                stage.display(),
                stage.display(),
            )
        );
        assert_eq!(
            fs::metadata(stage.join("created"))
                .expect("created sandbox file")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            fs::read_to_string(stage.join("network")).expect("read network denial"),
            libc::EPERM.to_string()
        );
        assert!(!directory.0.join("outside-write").exists());
        assert_eq!(
            fs::metadata(outside_metadata)
                .expect("stat unchanged outside fixture")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }

    #[test]
    fn repair_cancellation_and_timeout_terminate_the_process_group() {
        let directory = TemporaryDirectory::new("termination");
        let binary = calculator(&directory, "exec /usr/bin/sleep 30");
        let stage = stage(&directory);
        let tool = Tool::validate(&binary).expect("validate calculator");
        let started = std::time::Instant::now();
        let cancelled = tool
            .repair_with_limits(&stage, || true, test_limits(Duration::from_secs(5)))
            .expect_err("cancel calculator");
        assert_eq!(cancelled, RepairFailureKind::Cancelled);
        assert!(started.elapsed() < Duration::from_secs(1));

        let timed_out = tool
            .repair_with_limits(&stage, || false, test_limits(Duration::from_millis(50)))
            .expect_err("time out calculator");
        assert_eq!(timed_out, RepairFailureKind::TimedOut);
    }

    #[test]
    fn repair_preserves_non_utf8_stage_paths() {
        let directory = TemporaryDirectory::new("non-utf8-stage");
        let binary = calculator(&directory, ": > created");
        let stage = directory
            .0
            .join(std::ffi::OsString::from_vec(b"stage-\xff".to_vec()));
        fs::create_dir(&stage).expect("create non-UTF-8 repair stage");
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o700))
            .expect("secure non-UTF-8 repair stage");
        fs::write(stage.join("index.par2"), b"PAR2").expect("write repair index");
        fs::set_permissions(stage.join("index.par2"), fs::Permissions::from_mode(0o600))
            .expect("secure repair index");
        let tool = Tool::validate(&binary).expect("validate calculator");

        tool.repair_with_limits(&stage, || false, test_limits(Duration::from_secs(5)))
            .expect("repair under non-UTF-8 stage path");

        assert!(stage.join("created").is_file());
    }

    #[test]
    fn successful_repair_reaps_descendants_that_inherit_diagnostic_pipes() {
        let directory = TemporaryDirectory::new("repair-descendant");
        let binary = calculator(&directory, "/usr/bin/sleep 30 &\nexit 0");
        let stage = stage(&directory);
        let tool = Tool::validate(&binary).expect("validate calculator");
        let started = Instant::now();

        tool.repair_with_limits(&stage, || false, test_limits(Duration::from_secs(5)))
            .expect("complete repair while reaping descendant");

        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn repair_output_is_drained_and_failures_are_typed() {
        let directory = TemporaryDirectory::new("diagnostics");
        let binary = calculator(
            &directory,
            "/usr/bin/head -c 70000 /dev/zero\n\
             printf 'No space left on device' >&2\n\
             exit 6",
        );
        let stage = stage(&directory);
        let tool = Tool::validate(&binary).expect("validate calculator");

        let failure = tool
            .repair_with_limits(&stage, || false, test_limits(Duration::from_secs(5)))
            .expect_err("calculator must fail");

        assert_eq!(failure, RepairFailureKind::Io);
    }

    #[test]
    fn repair_rejects_symlink_and_hardlink_stage_inputs() {
        let directory = TemporaryDirectory::new("stage-inputs");
        let binary = calculator(&directory, "exit 0");
        let tool = Tool::validate(&binary).expect("validate calculator");
        let stage = stage(&directory);
        symlink("/dev/null", stage.join("escape")).expect("create stage symlink");
        assert_eq!(
            tool.repair_with_limits(&stage, || false, test_limits(Duration::from_secs(1)))
                .expect_err("reject stage symlink"),
            RepairFailureKind::Io
        );
        fs::remove_file(stage.join("escape")).expect("remove stage symlink");
        fs::hard_link(stage.join("index.par2"), stage.join("hardlink"))
            .expect("create stage hardlink");
        assert_eq!(
            tool.repair_with_limits(&stage, || false, test_limits(Duration::from_secs(1)))
                .expect_err("reject stage hardlink"),
            RepairFailureKind::Io
        );
    }

    #[test]
    fn readiness_fixture_is_a_valid_independently_verifiable_recovery_set() {
        let set = crate::par2::parse_recovery_set(&readiness_recovery_set())
            .expect("parse readiness recovery set");

        assert_eq!(set.slice_size, 16);
        assert_eq!(set.source_file_ids.len(), 1);
        assert_eq!(set.recovery_exponents.len(), 1);
        crate::par2::verify_complete_source(
            &set,
            set.source_file_ids[0],
            &mut std::io::Cursor::new(b"COMET-PAR2-READY"),
            16,
            &|| false,
        )
        .expect("verify readiness source");
    }

    #[test]
    fn engine_readiness_executes_repair_and_cleans_owned_stages() {
        let directory = TemporaryDirectory::new("engine-readiness");
        let stale = directory.0.join(".par2-readiness-1234-0000000000000000");
        fs::create_dir(&stale).expect("create stale readiness stage");
        let unrelated = directory.0.join(".par2-readiness-user");
        fs::create_dir(&unrelated).expect("create unrelated prefix directory");
        let binary = calculator(&directory, "printf 'COMET-PAR2-READY' > readiness.bin");
        let tool = Tool::validate(&binary).expect("validate calculator");

        tool.verify_repair_engine(&directory.0)
            .expect("execute calculator readiness repair");

        assert!(!stale.exists());
        assert!(unrelated.exists());
    }
}
