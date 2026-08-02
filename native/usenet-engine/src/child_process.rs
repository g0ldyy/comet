use std::fs::OpenOptions;
use std::io;
use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::process::{Child, ExitStatus};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone, Copy)]
pub(crate) struct Limits {
    pub cpu_seconds: u64,
    pub memory_bytes: u64,
    pub file_size_bytes: u64,
    pub open_files: u64,
}

pub(crate) struct Sandbox {
    limits: Limits,
    filesystem_ruleset: OwnedFd,
    system_call_filter: Vec<libc::sock_filter>,
}

impl Sandbox {
    pub(crate) fn new(limits: Limits, writable_root: Option<&Path>) -> Result<Self, io::Error> {
        Ok(Self {
            limits,
            filesystem_ruleset: filesystem_ruleset(writable_root)?,
            system_call_filter: system_call_filter(),
        })
    }

    pub(crate) fn apply(&self) -> io::Result<()> {
        // Every value and the seccomp program are prepared before fork. This
        // hook performs only async-signal-safe libc operations.
        if unsafe { libc::setpgid(0, 0) } != 0 {
            return Err(io::Error::last_os_error());
        }
        unsafe {
            libc::umask(0o077);
        }
        set_limit(libc::RLIMIT_CORE, 0)?;
        set_limit(libc::RLIMIT_CPU, self.limits.cpu_seconds)?;
        set_limit(libc::RLIMIT_AS, self.limits.memory_bytes)?;
        set_limit(libc::RLIMIT_FSIZE, self.limits.file_size_bytes)?;
        set_limit(libc::RLIMIT_NOFILE, self.limits.open_files)?;
        if unsafe { libc::prctl(libc::PR_SET_DUMPABLE, 0, 0, 0, 0) } != 0
            || unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) } != 0
        {
            return Err(io::Error::last_os_error());
        }
        mark_unrelated_descriptors_close_on_exec()?;
        restrict_filesystem(self.filesystem_ruleset.as_raw_fd())?;
        install_system_call_filter(&self.system_call_filter)
    }
}

pub(crate) fn terminate_group(child: &mut Child, grace: Duration) -> io::Result<ExitStatus> {
    signal_group(child.id(), libc::SIGTERM);
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    // The direct child deliberately remains unreaped until the process-group
    // kill, preventing its process-group ID from being reused in this window.
    signal_group(child.id(), libc::SIGKILL);
    child.wait()
}

pub(crate) fn try_wait_group(child: &mut Child) -> io::Result<Option<ExitStatus>> {
    let pid = libc::id_t::try_from(child.id()).expect("child PID fits id_t");
    loop {
        let mut information = MaybeUninit::<libc::siginfo_t>::zeroed();
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                pid,
                information.as_mut_ptr(),
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if result == 0 {
            let information = unsafe { information.assume_init() };
            if unsafe { information.si_pid() } == 0 {
                return Ok(None);
            }
            // Keep the exited leader unreaped until every process still in its
            // sandbox group has been killed, so the group ID cannot be reused.
            signal_group(child.id(), libc::SIGKILL);
            return child.wait().map(Some);
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn signal_group(pid: u32, signal: libc::c_int) {
    let pid = libc::pid_t::try_from(pid).expect("child PID fits pid_t");
    // SAFETY: a negative PID targets only the child-created process group.
    unsafe {
        libc::kill(-pid, signal);
    }
}

fn set_limit(resource: libc::__rlimit_resource_t, value: u64) -> io::Result<()> {
    let limit = libc::rlimit {
        rlim_cur: value,
        rlim_max: value,
    };
    if unsafe { libc::setrlimit(resource, &limit) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

fn mark_unrelated_descriptors_close_on_exec() -> io::Result<()> {
    const CLOSE_RANGE_CLOEXEC: libc::c_uint = 1 << 2;
    if unsafe { libc::syscall(libc::SYS_close_range, 3_u32, u32::MAX, CLOSE_RANGE_CLOEXEC) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

#[repr(C)]
struct LandlockRulesetAttr {
    handled_access_fs: u64,
}

#[repr(C, packed)]
struct LandlockPathBeneathAttr {
    allowed_access: u64,
    parent_fd: libc::c_int,
}

const LANDLOCK_CREATE_RULESET_VERSION: libc::c_uint = 1;
const LANDLOCK_RULE_PATH_BENEATH: libc::c_int = 1;
const LANDLOCK_ACCESS_FS_WRITE_FILE: u64 = 1 << 1;
const LANDLOCK_ACCESS_FS_REMOVE_DIR: u64 = 1 << 4;
const LANDLOCK_ACCESS_FS_REMOVE_FILE: u64 = 1 << 5;
const LANDLOCK_ACCESS_FS_MAKE_CHAR: u64 = 1 << 6;
const LANDLOCK_ACCESS_FS_MAKE_DIR: u64 = 1 << 7;
const LANDLOCK_ACCESS_FS_MAKE_REG: u64 = 1 << 8;
const LANDLOCK_ACCESS_FS_MAKE_SOCK: u64 = 1 << 9;
const LANDLOCK_ACCESS_FS_MAKE_FIFO: u64 = 1 << 10;
const LANDLOCK_ACCESS_FS_MAKE_BLOCK: u64 = 1 << 11;
const LANDLOCK_ACCESS_FS_MAKE_SYM: u64 = 1 << 12;
const LANDLOCK_ACCESS_FS_REFER: u64 = 1 << 13;
const LANDLOCK_ACCESS_FS_TRUNCATE: u64 = 1 << 14;
// Runtime and loader reads remain available, but every operation that can
// create, remove, rename, link, truncate or write a filesystem object is
// denied unless the pinned stage hierarchy grants it.
const LANDLOCK_WRITE_ACCESS: u64 = LANDLOCK_ACCESS_FS_WRITE_FILE
    | LANDLOCK_ACCESS_FS_REMOVE_DIR
    | LANDLOCK_ACCESS_FS_REMOVE_FILE
    | LANDLOCK_ACCESS_FS_MAKE_CHAR
    | LANDLOCK_ACCESS_FS_MAKE_DIR
    | LANDLOCK_ACCESS_FS_MAKE_REG
    | LANDLOCK_ACCESS_FS_MAKE_SOCK
    | LANDLOCK_ACCESS_FS_MAKE_FIFO
    | LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | LANDLOCK_ACCESS_FS_MAKE_SYM
    | LANDLOCK_ACCESS_FS_REFER
    | LANDLOCK_ACCESS_FS_TRUNCATE;

fn filesystem_ruleset(writable_root: Option<&Path>) -> io::Result<OwnedFd> {
    let abi = unsafe {
        libc::syscall(
            libc::SYS_landlock_create_ruleset,
            std::ptr::null::<LandlockRulesetAttr>(),
            0usize,
            LANDLOCK_CREATE_RULESET_VERSION,
        )
    };
    if abi < 0 {
        return Err(io::Error::last_os_error());
    }
    // ABI 3 added truncate mediation. Earlier versions would leave a
    // path-based destructive operation outside this write boundary.
    if abi < 3 {
        return Err(io::Error::from_raw_os_error(libc::EOPNOTSUPP));
    }
    let ruleset_attr = LandlockRulesetAttr {
        handled_access_fs: LANDLOCK_WRITE_ACCESS,
    };
    let ruleset_fd = unsafe {
        libc::syscall(
            libc::SYS_landlock_create_ruleset,
            &ruleset_attr,
            std::mem::size_of::<LandlockRulesetAttr>(),
            0u32,
        )
    };
    if ruleset_fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let ruleset_fd = libc::c_int::try_from(ruleset_fd).expect("kernel file descriptor fits c_int");
    // SAFETY: the successful syscall returned a new descriptor owned here.
    let ruleset = unsafe { OwnedFd::from_raw_fd(ruleset_fd) };
    if let Some(writable_root) = writable_root {
        let root = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_DIRECTORY | libc::O_NOFOLLOW)
            .open(writable_root)?;
        let rule = LandlockPathBeneathAttr {
            allowed_access: LANDLOCK_WRITE_ACCESS,
            parent_fd: root.as_raw_fd(),
        };
        if unsafe {
            libc::syscall(
                libc::SYS_landlock_add_rule,
                ruleset.as_raw_fd(),
                LANDLOCK_RULE_PATH_BENEATH,
                &rule,
                0u32,
            )
        } != 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(ruleset)
}

fn restrict_filesystem(ruleset_fd: libc::c_int) -> io::Result<()> {
    if unsafe { libc::syscall(libc::SYS_landlock_restrict_self, ruleset_fd, 0u32) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

#[cfg(target_arch = "x86_64")]
const AUDIT_ARCH: u32 = 0xc000_003e;
#[cfg(target_arch = "aarch64")]
const AUDIT_ARCH: u32 = 0xc000_00b7;

// Linux assigns fchmodat2 number 452 on both architectures supported here.
// libc does not expose the constant on AArch64 yet, but glibc can issue it
// when fchmodat flags require the newer syscall.
const SYS_FCHMODAT2: libc::c_long = 452;

#[cfg(target_arch = "x86_64")]
const ARCHITECTURE_DENIED: &[libc::c_long] = &[
    // AArch64 omitted these legacy path-based syscalls and implements the
    // corresponding libc operations through the common *at variants below.
    libc::SYS_chmod,
    libc::SYS_chown,
    libc::SYS_lchown,
    libc::SYS_utime,
    libc::SYS_utimes,
    libc::SYS_futimesat,
];
#[cfg(target_arch = "aarch64")]
const ARCHITECTURE_DENIED: &[libc::c_long] = &[];

fn system_call_filter() -> Vec<libc::sock_filter> {
    const BPF_LD_W_ABS: u16 = 0x20;
    const BPF_JMP_JEQ_K: u16 = 0x15;
    const BPF_RET_K: u16 = 0x06;
    const SECCOMP_RET_KILL_PROCESS: u32 = 0x8000_0000;
    const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;
    const SECCOMP_RET_ERRNO: u32 = 0x0005_0000;
    const COMMON_DENIED: &[libc::c_long] = &[
        libc::SYS_socket,
        libc::SYS_socketpair,
        libc::SYS_connect,
        libc::SYS_bind,
        libc::SYS_listen,
        libc::SYS_accept,
        libc::SYS_accept4,
        libc::SYS_sendto,
        libc::SYS_sendmsg,
        libc::SYS_sendmmsg,
        libc::SYS_recvfrom,
        libc::SYS_recvmsg,
        libc::SYS_recvmmsg,
        libc::SYS_shutdown,
        libc::SYS_getsockname,
        libc::SYS_getpeername,
        libc::SYS_setsockopt,
        libc::SYS_getsockopt,
        libc::SYS_setpgid,
        libc::SYS_setsid,
        // Landlock does not yet mediate metadata-only filesystem mutations.
        libc::SYS_fchmod,
        libc::SYS_fchmodat,
        SYS_FCHMODAT2,
        libc::SYS_fchown,
        libc::SYS_fchownat,
        libc::SYS_setxattr,
        libc::SYS_lsetxattr,
        libc::SYS_fsetxattr,
        libc::SYS_removexattr,
        libc::SYS_lremovexattr,
        libc::SYS_fremovexattr,
        libc::SYS_utimensat,
    ];
    let denied_count = COMMON_DENIED.len() + ARCHITECTURE_DENIED.len();
    let mut filters = Vec::<libc::sock_filter>::with_capacity(5 + denied_count * 2);
    filters.push(filter(BPF_LD_W_ABS, 0, 0, 4));
    filters.push(filter(BPF_JMP_JEQ_K, 1, 0, AUDIT_ARCH));
    filters.push(filter(BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS));
    filters.push(filter(BPF_LD_W_ABS, 0, 0, 0));
    for system_call in COMMON_DENIED.iter().chain(ARCHITECTURE_DENIED) {
        filters.push(filter(BPF_JMP_JEQ_K, 0, 1, *system_call as u32));
        filters.push(filter(
            BPF_RET_K,
            0,
            0,
            SECCOMP_RET_ERRNO | libc::EPERM as u32,
        ));
    }
    filters.push(filter(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW));
    filters
}

fn install_system_call_filter(filters: &[libc::sock_filter]) -> io::Result<()> {
    const SECCOMP_MODE_FILTER: libc::c_ulong = 2;
    let program = libc::sock_fprog {
        len: filters.len() as u16,
        filter: filters.as_ptr().cast_mut(),
    };
    if unsafe {
        libc::prctl(
            libc::PR_SET_SECCOMP,
            SECCOMP_MODE_FILTER,
            &program as *const libc::sock_fprog,
            0,
            0,
        )
    } == 0
    {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

const fn filter(code: u16, jt: u8, jf: u8, k: u32) -> libc::sock_filter {
    libc::sock_filter { code, jt, jf, k }
}
