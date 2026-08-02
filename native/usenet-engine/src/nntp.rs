use crate::cache::FlightCancellation;
use flate2::{Compress, Compression, Decompress, FlushCompress, FlushDecompress};
use rustls::pki_types::ServerName;
use rustls::{ClientConfig, ClientConnection, RootCertStore, StreamOwned};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::ops::Deref;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, OnceLock, Weak, mpsc};
use std::thread;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

const MAX_LINE_BYTES: usize = crate::nntp_protocol::MAX_LINE_BYTES;
const TIMEOUT: Duration = Duration::from_secs(15);
const ARTICLE_TOTAL_TIMEOUT: Duration = Duration::from_secs(30);
const BODY_STALL_TIMEOUT: Duration = Duration::from_secs(30);
const CANCELLATION_POLL: Duration = Duration::from_millis(100);
const MAX_PHYSICAL_POOL_ENTRIES: usize = 16_384;
const MAX_PIPELINE_QUEUE: usize = 1024;
const PIPELINE_COALESCE_WINDOW: Duration = Duration::from_millis(2);
const DEFAULT_HEDGE_DELAY: Duration = Duration::from_millis(500);
const MIN_HEDGE_DELAY: Duration = Duration::from_millis(100);
const MAX_HEDGE_DELAY: Duration = Duration::from_millis(750);
const DNS_WORKERS: usize = 4;
const MAX_DNS_QUEUE: usize = 64;
const MAX_RESOLVED_ADDRESSES: usize = 64;
const ARTICLE_RESERVATION_QUANTUM: u64 = 64 * 1024;
const MAX_DECLARED_ARTICLE_BYTES: u64 = crate::session::MAX_DECLARED_POSTING_BYTES;
const INTERACTIVE_DECODED_RESERVE: u64 = MAX_DECLARED_ARTICLE_BYTES * 2;
const MAX_PIPELINE_DEPTH: usize = 16;
const MAX_CAPABILITY_LINES: usize = 128;
const COMPRESSED_FRAMING_ALLOWANCE: u64 = 64 * 1024;
const CAPABILITIES_COMPRESSED_READ_LIMIT: u64 =
    (MAX_LINE_BYTES * (MAX_CAPABILITY_LINES + 1) + 3) as u64 + COMPRESSED_FRAMING_ALLOWANCE;

type PartResult = Result<crate::yenc::DecodedPart, &'static str>;
type Resolution = Result<Vec<SocketAddr>, &'static str>;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum WorkClass {
    Interactive,
    Preparation,
    Background,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SchedulingContext {
    pub work_class: WorkClass,
    pub configuration_partition: [u8; 32],
    pub session: String,
    pub declared_encoded_bytes: u64,
}

struct ResolverJob {
    host: String,
    port: u16,
    deadline: Instant,
    abandoned: Arc<AtomicBool>,
    result: mpsc::SyncSender<Resolution>,
}

struct Resolver {
    queue: Mutex<VecDeque<ResolverJob>>,
    changed: Condvar,
}

impl Resolver {
    fn start() -> Result<Arc<Self>, &'static str> {
        let resolver = Arc::new(Self {
            queue: Mutex::new(VecDeque::new()),
            changed: Condvar::new(),
        });
        let mut workers = 0;
        for index in 0..DNS_WORKERS {
            let worker = Arc::clone(&resolver);
            if thread::Builder::new()
                .name(format!("usenet-dns-{index}"))
                .spawn(move || worker.run())
                .is_ok()
            {
                workers += 1;
            }
        }
        if workers == 0 {
            return Err("nntp_dns_unavailable");
        }
        Ok(resolver)
    }

    fn run(&self) {
        loop {
            let job = {
                let mut queue = self.queue.lock().expect("NNTP resolver queue lock");
                while queue.is_empty() {
                    queue = self.changed.wait(queue).expect("NNTP resolver queue wait");
                }
                queue.pop_front().expect("nonempty NNTP resolver queue")
            };
            if job.abandoned.load(Ordering::Acquire) {
                continue;
            }
            if Instant::now() >= job.deadline {
                let _ = job.result.send(Err("nntp_dns_timeout"));
                continue;
            }
            let result = resolve_blocking(&job.host, job.port);
            let _ = job.result.send(result);
        }
    }

    fn submit(&self, job: ResolverJob) -> Result<(), &'static str> {
        let mut queue = self.queue.lock().expect("NNTP resolver queue lock");
        let now = Instant::now();
        queue.retain(|queued| !queued.abandoned.load(Ordering::Acquire) && now < queued.deadline);
        if queue.len() >= MAX_DNS_QUEUE {
            return Err("nntp_dns_busy");
        }
        queue.push_back(job);
        self.changed.notify_one();
        Ok(())
    }
}

static DNS_RESOLVER: OnceLock<Result<Arc<Resolver>, &'static str>> = OnceLock::new();

struct PipelineTask {
    request: BodyRequest,
    group: Option<String>,
    maximum_wire_bytes: usize,
    maximum_decoded_bytes: usize,
    cancellation: FlightCancellation,
    scheduling: SchedulingContext,
    _reservation: Option<PipelineReservation>,
    _pool_reference: PoolReference,
    result: mpsc::SyncSender<PartResult>,
}

impl PipelineTask {
    fn complete(&mut self, result: PartResult) {
        self._reservation.take();
        let _ = self.result.send(result);
    }
}

struct PipelineReservation {
    pool: Weak<PhysicalPool>,
    encoded_bytes: u64,
    decoded_bytes: u64,
    _decoded_memory: DecodedMemoryReservation,
}

impl Drop for PipelineReservation {
    fn drop(&mut self) {
        if let Some(pool) = self.pool.upgrade() {
            pool.release_pipeline_reservation(self.encoded_bytes, self.decoded_bytes);
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum GroupRequirement {
    Unknown,
    NotRequired,
    Required,
}

enum GroupState {
    Unknown,
    Probing,
    NotRequired,
    Required,
}

struct GroupRouting {
    state: Mutex<GroupState>,
    changed: Condvar,
}

impl GroupRouting {
    fn new() -> Self {
        Self {
            state: Mutex::new(GroupState::Unknown),
            changed: Condvar::new(),
        }
    }

    fn snapshot(&self) -> GroupRequirement {
        match *self.state.lock().expect("NNTP group routing lock") {
            GroupState::Unknown | GroupState::Probing => GroupRequirement::Unknown,
            GroupState::NotRequired => GroupRequirement::NotRequired,
            GroupState::Required => GroupRequirement::Required,
        }
    }

    fn dispatch(
        self: &Arc<Self>,
        cancellation: &FlightCancellation,
    ) -> Result<GroupDispatch, &'static str> {
        let mut state = self.state.lock().expect("NNTP group routing lock");
        loop {
            cancellation.checkpoint()?;
            match *state {
                GroupState::Unknown => {
                    *state = GroupState::Probing;
                    return Ok(GroupDispatch {
                        routing: Arc::clone(self),
                        requirement: GroupRequirement::Unknown,
                        probing: true,
                    });
                }
                GroupState::Probing => {
                    state = self
                        .changed
                        .wait_timeout(state, CANCELLATION_POLL)
                        .expect("NNTP group routing wait")
                        .0;
                }
                GroupState::NotRequired => {
                    return Ok(GroupDispatch {
                        routing: Arc::clone(self),
                        requirement: GroupRequirement::NotRequired,
                        probing: false,
                    });
                }
                GroupState::Required => {
                    return Ok(GroupDispatch {
                        routing: Arc::clone(self),
                        requirement: GroupRequirement::Required,
                        probing: false,
                    });
                }
            }
        }
    }

    fn finish_probe(&self, learned: Option<GroupRequirement>) {
        let mut state = self.state.lock().expect("NNTP group routing lock");
        debug_assert!(matches!(*state, GroupState::Probing));
        *state = match learned {
            Some(GroupRequirement::NotRequired) => GroupState::NotRequired,
            Some(GroupRequirement::Required) => GroupState::Required,
            _ => GroupState::Unknown,
        };
        self.changed.notify_all();
    }

    #[cfg(test)]
    fn assume_not_required(&self) {
        *self.state.lock().expect("NNTP group routing lock") = GroupState::NotRequired;
        self.changed.notify_all();
    }

    #[cfg(test)]
    fn assume_required(&self) {
        *self.state.lock().expect("NNTP group routing lock") = GroupState::Required;
        self.changed.notify_all();
    }
}

struct GroupDispatch {
    routing: Arc<GroupRouting>,
    requirement: GroupRequirement,
    probing: bool,
}

impl GroupDispatch {
    fn finish(&mut self, learned: Option<GroupRequirement>) {
        if self.probing {
            self.routing.finish_probe(learned);
            self.probing = false;
        }
    }
}

impl Drop for GroupDispatch {
    fn drop(&mut self) {
        self.finish(None);
    }
}

#[derive(Default)]
struct ProviderTelemetry {
    provider_attempts: AtomicU64,
    provider_suppliers: AtomicU64,
    provider_hits: AtomicU64,
    provider_missing: AtomicU64,
    provider_corrupt: AtomicU64,
    provider_failures: AtomicU64,
    provider_cancellations: AtomicU64,
    provider_failovers: AtomicU64,
    scheduler_busy_rejections: AtomicU64,
}

impl ProviderTelemetry {
    fn increment(counter: &AtomicU64) {
        let _ = counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
            Some(value.saturating_add(1))
        });
    }
}

fn authentication_failure(code: &str) -> bool {
    matches!(code, "nntp_auth_failed" | "nntp_auth_required")
}

pub(crate) fn retryable_provider_failure(code: &str) -> bool {
    matches!(
        code,
        "nntp_dns_failed"
            | "nntp_dns_timeout"
            | "nntp_dns_busy"
            | "nntp_dns_unavailable"
            | "nntp_connect_failed"
            | "nntp_tls_failed"
            | "nntp_greeting_rejected"
            | "nntp_compression_failed"
            | "nntp_compression_desynchronized"
            | "nntp_read_failed"
            | "nntp_write_failed"
            | "nntp_invalid_response"
            | "nntp_body_failed"
            | "nntp_article_timeout"
            | "nntp_pipeline_desynchronized"
            | "nntp_pipeline_unavailable"
    )
}

fn reusable_connection_response(code: &str) -> bool {
    matches!(
        code,
        "nntp_article_missing" | "nntp_group_required" | "nntp_group_failed"
    ) || crate::yenc::integrity_failure(code)
}

fn synchronized_connection_response(code: &str) -> bool {
    code == "nntp_body_failed" || reusable_connection_response(code)
}

fn reconnectable_connection_failure(code: &str) -> bool {
    matches!(
        code,
        "nntp_auth_required" | "nntp_invalid_response" | "nntp_read_failed" | "nntp_write_failed"
    )
}

fn article_result<T>(
    result: Result<T, &'static str>,
    deadline: Instant,
) -> Result<T, &'static str> {
    if Instant::now() >= deadline {
        Err("nntp_article_timeout")
    } else {
        result
    }
}

fn article_remaining(deadline: Instant) -> Result<Duration, &'static str> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|remaining| !remaining.is_zero())
        .map(|remaining| remaining.min(BODY_STALL_TIMEOUT))
        .ok_or("nntp_article_timeout")
}

fn pipeline_deadline(total_deadline: Option<Instant>, timeout: Duration) -> Instant {
    let operation_deadline = Instant::now() + timeout;
    total_deadline.map_or(operation_deadline, |deadline| {
        operation_deadline.min(deadline)
    })
}

fn pipeline_remaining(total_deadline: Option<Instant>) -> Result<Duration, &'static str> {
    total_deadline.map_or(Ok(BODY_STALL_TIMEOUT), article_remaining)
}

fn pipeline_result<T>(
    result: Result<T, &'static str>,
    total_deadline: Option<Instant>,
) -> Result<T, &'static str> {
    match total_deadline {
        Some(deadline) => article_result(result, deadline),
        None => result,
    }
}

#[derive(Clone, Eq, PartialEq)]
pub struct SecretText(Arc<Zeroizing<String>>);

impl<'de> Deserialize<'de> for SecretText {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        String::deserialize(deserializer).map(Self::from)
    }
}

impl Deref for SecretText {
    type Target = str;

    fn deref(&self) -> &Self::Target {
        self.0.as_str()
    }
}

impl From<String> for SecretText {
    fn from(value: String) -> Self {
        Self(Arc::new(Zeroizing::new(value)))
    }
}

impl From<&str> for SecretText {
    fn from(value: &str) -> Self {
        Self::from(value.to_owned())
    }
}

#[derive(Clone, Deserialize, Eq, PartialEq)]
pub struct BodyRequest {
    pub host: String,
    pub port: u16,
    pub tls_mode: String,
    #[serde(default)]
    pub allow_private: bool,
    pub username: Option<SecretText>,
    pub password: Option<SecretText>,
    #[serde(default)]
    pub message_id: String,
}

pub fn validate_body_template(request: &BodyRequest) -> Result<(), &'static str> {
    validate_request(
        &request.host,
        &request.tls_mode,
        request.username.as_deref(),
        request.password.as_deref(),
        "validation@comet.invalid",
    )
}

pub fn valid_group(value: &str) -> bool {
    valid_text(value, 512)
}

fn valid_text(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && !value
            .bytes()
            .any(|byte| byte.is_ascii_control() || byte == b' ')
}

enum NntpStream {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ClientConnection, TcpStream>>),
    Deflate(Box<DeflateStream>),
}

struct DeflateStream {
    inner: Box<NntpStream>,
    compressor: Compress,
    decompressor: Decompress,
    input: [u8; 16 * 1024],
    input_start: usize,
    input_end: usize,
    compressed_read: u64,
    compressed_read_limit: Option<u64>,
}

impl DeflateStream {
    fn new(inner: NntpStream) -> Self {
        Self {
            inner: Box::new(inner),
            compressor: Compress::new(Compression::fast(), false),
            decompressor: Decompress::new(false),
            input: [0; 16 * 1024],
            input_start: 0,
            input_end: 0,
            compressed_read: 0,
            compressed_read_limit: None,
        }
    }

    fn begin_read_budget(&mut self, maximum: u64) {
        self.compressed_read = 0;
        self.compressed_read_limit = Some(maximum);
    }

    fn account_input(&mut self, consumed: usize) -> std::io::Result<()> {
        self.compressed_read = self
            .compressed_read
            .checked_add(u64::try_from(consumed).map_err(std::io::Error::other)?)
            .ok_or_else(|| std::io::Error::other("compressed NNTP input overflow"))?;
        if self
            .compressed_read_limit
            .is_some_and(|maximum| self.compressed_read > maximum)
        {
            return Err(std::io::Error::other(
                "compressed NNTP input exceeds article budget",
            ));
        }
        Ok(())
    }

    fn write_all_cancellable<F>(
        &mut self,
        input: &[u8],
        cancelled: &F,
        deadline: Instant,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        if input.is_empty() {
            return Ok(());
        }
        let mut consumed = 0usize;
        loop {
            cancellation_checkpoint(cancelled)?;
            let mut output = [0u8; 16 * 1024];
            let before_input = self.compressor.total_in();
            let before_output = self.compressor.total_out();
            self.compressor
                .compress(&input[consumed..], &mut output, FlushCompress::Sync)
                .map_err(|_| "nntp_write_failed")?;
            let step = usize::try_from(self.compressor.total_in() - before_input)
                .map_err(|_| "nntp_write_failed")?;
            let produced = usize::try_from(self.compressor.total_out() - before_output)
                .map_err(|_| "nntp_write_failed")?;
            consumed += step;
            self.inner
                .write_all_cancellable_until(&output[..produced], cancelled, deadline)?;
            if consumed == input.len() && produced < output.len() {
                return Ok(());
            }
            if step == 0 && produced == 0 {
                return Err("nntp_write_failed");
            }
        }
    }
}

impl Read for NntpStream {
    fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(stream) => stream.read(buffer),
            Self::Tls(stream) => stream.read(buffer),
            Self::Deflate(stream) => stream.read(buffer),
        }
    }
}

impl Write for NntpStream {
    fn write(&mut self, buffer: &[u8]) -> std::io::Result<usize> {
        match self {
            Self::Plain(stream) => stream.write(buffer),
            Self::Tls(stream) => stream.write(buffer),
            Self::Deflate(stream) => stream.write(buffer),
        }
    }

    fn flush(&mut self) -> std::io::Result<()> {
        match self {
            Self::Plain(stream) => stream.flush(),
            Self::Tls(stream) => stream.flush(),
            Self::Deflate(stream) => stream.flush(),
        }
    }
}

impl Read for DeflateStream {
    fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
        if output.is_empty() {
            return Ok(0);
        }
        loop {
            if self.input_start == self.input_end {
                self.input_end = self.inner.read(&mut self.input)?;
                self.input_start = 0;
                if self.input_end == 0 {
                    return Ok(0);
                }
            }
            let before_input = self.decompressor.total_in();
            let before_output = self.decompressor.total_out();
            self.decompressor
                .decompress(
                    &self.input[self.input_start..self.input_end],
                    output,
                    FlushDecompress::Sync,
                )
                .map_err(|_| std::io::Error::other("invalid NNTP DEFLATE stream"))?;
            let consumed = usize::try_from(self.decompressor.total_in() - before_input)
                .map_err(std::io::Error::other)?;
            let produced = usize::try_from(self.decompressor.total_out() - before_output)
                .map_err(std::io::Error::other)?;
            self.input_start += consumed;
            self.account_input(consumed)?;
            if produced != 0 {
                return Ok(produced);
            }
            if consumed == 0 {
                if self.input_start != self.input_end {
                    return Err(std::io::Error::other("stalled NNTP DEFLATE stream"));
                }
                continue;
            }
        }
    }
}

impl Write for DeflateStream {
    fn write(&mut self, input: &[u8]) -> std::io::Result<usize> {
        self.write_all_cancellable(input, &|| false, Instant::now() + TIMEOUT)
            .map_err(std::io::Error::other)?;
        Ok(input.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

type Reader = BufReader<NntpStream>;

impl NntpStream {
    fn set_read_timeout(&self, timeout: Duration) -> Result<(), &'static str> {
        if let Self::Deflate(stream) = self {
            return stream.inner.set_read_timeout(timeout);
        }
        let result = match self {
            Self::Plain(stream) => stream.set_read_timeout(Some(timeout)),
            Self::Tls(stream) => stream.sock.set_read_timeout(Some(timeout)),
            Self::Deflate(_) => unreachable!("handled compressed NNTP stream"),
        };
        result.map_err(|_| "nntp_timeout_configuration_failed")
    }

    fn set_write_timeout(&self, timeout: Duration) -> Result<(), &'static str> {
        if let Self::Deflate(stream) = self {
            return stream.inner.set_write_timeout(timeout);
        }
        let result = match self {
            Self::Plain(stream) => stream.set_write_timeout(Some(timeout)),
            Self::Tls(stream) => stream.sock.set_write_timeout(Some(timeout)),
            Self::Deflate(_) => unreachable!("handled compressed NNTP stream"),
        };
        result.map_err(|_| "nntp_timeout_configuration_failed")
    }

    #[cfg(test)]
    fn write_all_cancellable<F>(
        &mut self,
        input: &[u8],
        cancelled: &F,
        timeout: Duration,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        self.write_all_cancellable_until(input, cancelled, Instant::now() + timeout)
    }

    fn write_all_cancellable_until<F>(
        &mut self,
        input: &[u8],
        cancelled: &F,
        deadline: Instant,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        match self {
            Self::Plain(stream) => write_and_flush_cancellable(stream, input, cancelled, deadline),
            Self::Tls(stream) => write_and_flush_cancellable(stream, input, cancelled, deadline),
            Self::Deflate(stream) => stream.write_all_cancellable(input, cancelled, deadline),
        }
    }

    fn enable_deflate(self) -> Self {
        Self::Deflate(Box::new(DeflateStream::new(self)))
    }

    fn begin_compressed_read_budget(&mut self, maximum: u64) {
        if let Self::Deflate(stream) = self {
            stream.begin_read_budget(maximum);
        }
    }
}

fn write_and_flush_cancellable<W, F>(
    writer: &mut W,
    input: &[u8],
    cancelled: &F,
    deadline: Instant,
) -> Result<(), &'static str>
where
    W: Write,
    F: Fn() -> bool,
{
    let mut written = 0usize;
    while written < input.len() {
        cancellation_checkpoint(cancelled)?;
        if Instant::now() >= deadline {
            return Err("nntp_write_failed");
        }
        match writer.write(&input[written..]) {
            Ok(0) => return Err("nntp_write_failed"),
            Ok(length) => written += length,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::Interrupted
                ) => {}
            Err(_) => return Err("nntp_write_failed"),
        }
    }
    loop {
        cancellation_checkpoint(cancelled)?;
        if Instant::now() >= deadline {
            return Err("nntp_write_failed");
        }
        match writer.flush() {
            Ok(()) => return Ok(()),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::Interrupted
                ) => {}
            Err(_) => return Err("nntp_write_failed"),
        }
    }
}

struct NntpConnection {
    reader: Reader,
    selected_group: Option<String>,
    pool_epoch: u64,
}

enum PipelineOutcome {
    Healthy,
    Unhealthy,
    ReconnectFrom(usize, &'static str),
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct PhysicalPoolKey {
    host: String,
    port: u16,
    tls_mode: String,
    allow_private: bool,
    username_fingerprint: Option<[u8; 32]>,
    credential_fingerprint: [u8; 32],
}

impl PhysicalPoolKey {
    fn from_request(request: &BodyRequest) -> Self {
        let username_fingerprint = request.username.as_ref().map(|username| {
            let mut digest = Sha256::new();
            digest.update(b"comet-nntp-username-v1\0");
            digest.update(username.as_bytes());
            digest.finalize().into()
        });
        let mut digest = Sha256::new();
        digest.update(b"comet-nntp-credential-v1\0");
        if let Some(username) = &request.username {
            digest.update(username.as_bytes());
        }
        digest.update(b"\0");
        if let Some(password) = &request.password {
            digest.update(password.as_bytes());
        }
        Self {
            host: request.host.to_ascii_lowercase(),
            port: request.port,
            tls_mode: request.tls_mode.clone(),
            allow_private: request.allow_private,
            username_fingerprint,
            credential_fingerprint: digest.finalize().into(),
        }
    }
}

struct GlobalConnectionState {
    open: usize,
    reserved_decoded_bytes: u64,
    reserved_non_interactive_decoded_bytes: u64,
}

struct GlobalConnectionLimiter {
    maximum: usize,
    maximum_decoded_bytes: u64,
    state: Mutex<GlobalConnectionState>,
    changed: Condvar,
}

struct DecodedMemoryReservation {
    limiter: Arc<GlobalConnectionLimiter>,
    bytes: u64,
    non_interactive: bool,
}

impl Drop for DecodedMemoryReservation {
    fn drop(&mut self) {
        let mut state = self.limiter.state.lock().expect("global NNTP limiter lock");
        state.reserved_decoded_bytes = state
            .reserved_decoded_bytes
            .checked_sub(self.bytes)
            .expect("global NNTP decoded reservation");
        if self.non_interactive {
            state.reserved_non_interactive_decoded_bytes = state
                .reserved_non_interactive_decoded_bytes
                .checked_sub(self.bytes)
                .expect("global non-interactive NNTP decoded reservation");
        }
        self.limiter.changed.notify_all();
    }
}

impl GlobalConnectionLimiter {
    fn new(maximum: usize, maximum_decoded_bytes: u64) -> Result<Self, &'static str> {
        if maximum == 0 || maximum_decoded_bytes == 0 {
            return Err("invalid_nntp_connection_limit");
        }
        Ok(Self {
            maximum,
            maximum_decoded_bytes,
            state: Mutex::new(GlobalConnectionState {
                open: 0,
                reserved_decoded_bytes: 0,
                reserved_non_interactive_decoded_bytes: 0,
            }),
            changed: Condvar::new(),
        })
    }

    fn reserve_decoded(
        self: &Arc<Self>,
        bytes: u64,
        work_class: WorkClass,
    ) -> Result<DecodedMemoryReservation, &'static str> {
        let mut state = self.state.lock().expect("global NNTP limiter lock");
        let non_interactive = work_class != WorkClass::Interactive;
        let maximum_non_interactive = self
            .maximum_decoded_bytes
            .saturating_sub(INTERACTIVE_DECODED_RESERVE);
        if state
            .reserved_decoded_bytes
            .checked_add(bytes)
            .is_none_or(|reserved| reserved > self.maximum_decoded_bytes)
            || (non_interactive
                && state
                    .reserved_non_interactive_decoded_bytes
                    .checked_add(bytes)
                    .is_none_or(|reserved| reserved > maximum_non_interactive))
        {
            return Err("native_busy");
        }
        state.reserved_decoded_bytes += bytes;
        if non_interactive {
            state.reserved_non_interactive_decoded_bytes += bytes;
        }
        Ok(DecodedMemoryReservation {
            limiter: Arc::clone(self),
            bytes,
            non_interactive,
        })
    }

    fn acquire<F, R>(&self, cancelled: &F, reclaim_idle: &R) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
        R: Fn() -> bool,
    {
        let mut state = self.state.lock().expect("global NNTP limiter lock");
        loop {
            cancellation_checkpoint(cancelled)?;
            if state.open < self.maximum {
                state.open += 1;
                return Ok(());
            }
            drop(state);
            if reclaim_idle() {
                state = self.state.lock().expect("global NNTP limiter lock");
                continue;
            }
            state = self.state.lock().expect("global NNTP limiter lock");
            state = self
                .changed
                .wait_timeout(state, Duration::from_millis(25))
                .expect("global NNTP limiter wait")
                .0;
        }
    }

    fn release(&self) {
        let mut state = self.state.lock().expect("global NNTP limiter lock");
        state.open = state.open.checked_sub(1).expect("global NNTP permit");
        self.changed.notify_one();
    }

    fn notify_reclaimable(&self) {
        self.changed.notify_all();
    }
}

struct PhysicalPoolState {
    idle: Vec<NntpConnection>,
    open: usize,
    leased: usize,
    connection_limit: usize,
    pipeline_depth: usize,
    epoch: u64,
    pipeline_queue: VecDeque<PipelineTask>,
    reserved_commands: usize,
    reserved_encoded_bytes: u64,
    reserved_decoded_bytes: u64,
    last_partition: HashMap<WorkClass, [u8; 32]>,
    last_session: HashMap<(WorkClass, [u8; 32]), String>,
    dispatchers: usize,
    busy_dispatchers: usize,
    references: HashMap<u64, (usize, usize)>,
}

struct PhysicalPool {
    state: Mutex<PhysicalPoolState>,
    changed: Condvar,
    global: Arc<GlobalConnectionLimiter>,
    poisoned_connections: Arc<AtomicU64>,
}

impl PhysicalPool {
    fn new(global: Arc<GlobalConnectionLimiter>, poisoned_connections: Arc<AtomicU64>) -> Self {
        Self {
            state: Mutex::new(PhysicalPoolState {
                idle: Vec::new(),
                open: 0,
                leased: 0,
                connection_limit: 0,
                pipeline_depth: 1,
                epoch: 1,
                pipeline_queue: VecDeque::new(),
                reserved_commands: 0,
                reserved_encoded_bytes: 0,
                reserved_decoded_bytes: 0,
                last_partition: HashMap::new(),
                last_session: HashMap::new(),
                dispatchers: 0,
                busy_dispatchers: 0,
                references: HashMap::new(),
            }),
            changed: Condvar::new(),
            global,
            poisoned_connections,
        }
    }

    fn add_reference(&self, reference_id: u64, connection_limit: usize, pipeline_depth: usize) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        assert!(
            state
                .references
                .insert(reference_id, (connection_limit, pipeline_depth))
                .is_none(),
            "NNTP pool reference IDs are unique"
        );
        let removed = recompute_pool_limits(&mut state);
        drop(state);
        for _ in 0..removed {
            self.global.release();
        }
        self.changed.notify_all();
    }

    fn remove_reference(&self, reference_id: u64) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        state
            .references
            .remove(&reference_id)
            .expect("registered NNTP pool reference");
        let removed = recompute_pool_limits(&mut state);
        drop(state);
        for _ in 0..removed {
            self.global.release();
        }
        self.changed.notify_all();
    }

    fn enqueue(&self, task: PipelineTask) -> Result<bool, &'static str> {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        if state.pipeline_queue.len() >= MAX_PIPELINE_QUEUE {
            return Err("nntp_pipeline_capacity");
        }
        state.pipeline_queue.push_back(task);
        self.changed.notify_all();
        debug_assert!(state.busy_dispatchers <= state.dispatchers);
        if state.busy_dispatchers == state.dispatchers && state.dispatchers < state.connection_limit
        {
            state.dispatchers += 1;
            Ok(true)
        } else {
            Ok(false)
        }
    }

    fn reserve_pipeline(
        self: &Arc<Self>,
        declared_encoded_bytes: u64,
        work_class: WorkClass,
    ) -> Result<PipelineReservation, &'static str> {
        let article_bytes = rounded_article_cost(declared_encoded_bytes)?;
        let decoded_memory = self.global.reserve_decoded(article_bytes, work_class)?;
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        let byte_capacity = u64::try_from(state.connection_limit)
            .ok()
            .and_then(|connections| {
                u64::try_from(state.pipeline_depth)
                    .ok()
                    .and_then(|depth| connections.checked_mul(depth))
            })
            .and_then(|slots| slots.checked_mul(MAX_DECLARED_ARTICLE_BYTES))
            .ok_or("native_busy")?;
        if state.reserved_commands >= MAX_PIPELINE_QUEUE
            || state
                .reserved_encoded_bytes
                .checked_add(article_bytes)
                .is_none_or(|reserved| reserved > byte_capacity)
        {
            return Err("native_busy");
        }
        state.reserved_commands += 1;
        state.reserved_encoded_bytes += article_bytes;
        state.reserved_decoded_bytes += article_bytes;
        Ok(PipelineReservation {
            pool: Arc::downgrade(self),
            encoded_bytes: article_bytes,
            decoded_bytes: article_bytes,
            _decoded_memory: decoded_memory,
        })
    }

    fn release_pipeline_reservation(&self, encoded_bytes: u64, decoded_bytes: u64) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        state.reserved_commands = state
            .reserved_commands
            .checked_sub(1)
            .expect("physical NNTP command reservation");
        state.reserved_encoded_bytes = state
            .reserved_encoded_bytes
            .checked_sub(encoded_bytes)
            .expect("physical NNTP encoded reservation");
        state.reserved_decoded_bytes = state
            .reserved_decoded_bytes
            .checked_sub(decoded_bytes)
            .expect("physical NNTP decoded reservation");
        self.changed.notify_all();
    }

    fn take_pipeline_batch(&self) -> Option<(Vec<PipelineTask>, bool)> {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        if state.pipeline_queue.len() == 1 && state.pipeline_depth > 1 {
            state = self
                .changed
                .wait_timeout(state, PIPELINE_COALESCE_WINDOW)
                .expect("physical NNTP pipeline wait")
                .0;
        }
        if state.pipeline_queue.is_empty() {
            debug_assert!(state.busy_dispatchers < state.dispatchers);
            state.dispatchers = state
                .dispatchers
                .checked_sub(1)
                .expect("physical NNTP dispatcher");
            return None;
        }
        let depth = state.pipeline_depth;
        state.busy_dispatchers += 1;
        let first = take_fair_task(&mut state);
        let mut batch = Vec::with_capacity(depth);
        batch.push(first);
        while batch.len() < depth {
            let Some(task) = take_compatible_fair_task(&mut state, &batch[0]) else {
                break;
            };
            batch.push(task);
        }
        let start_dispatcher =
            !state.pipeline_queue.is_empty() && state.dispatchers < state.connection_limit;
        if start_dispatcher {
            state.dispatchers += 1;
        }
        Some((batch, start_dispatcher))
    }

    fn complete_pipeline_batch(&self) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        state.busy_dispatchers = state
            .busy_dispatchers
            .checked_sub(1)
            .expect("physical NNTP busy dispatcher");
        self.changed.notify_all();
    }

    fn cancel_dispatcher_reservation(&self) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        debug_assert!(state.busy_dispatchers < state.dispatchers);
        state.dispatchers = state
            .dispatchers
            .checked_sub(1)
            .expect("physical NNTP dispatcher reservation");
        self.changed.notify_all();
    }

    fn checkout<F, R>(
        &self,
        request: &BodyRequest,
        cancelled: &F,
        reclaim_idle: &R,
    ) -> Result<NntpConnection, &'static str>
    where
        F: Fn() -> bool,
        R: Fn() -> bool,
    {
        self.checkout_with_reserved_interactive(request, cancelled, reclaim_idle, false)
    }

    fn checkout_preparation<F, R>(
        &self,
        request: &BodyRequest,
        cancelled: &F,
        reclaim_idle: &R,
    ) -> Result<NntpConnection, &'static str>
    where
        F: Fn() -> bool,
        R: Fn() -> bool,
    {
        self.checkout_with_reserved_interactive(request, cancelled, reclaim_idle, true)
    }

    fn checkout_with_reserved_interactive<F, R>(
        &self,
        request: &BodyRequest,
        cancelled: &F,
        reclaim_idle: &R,
        reserve_interactive: bool,
    ) -> Result<NntpConnection, &'static str>
    where
        F: Fn() -> bool,
        R: Fn() -> bool,
    {
        let (should_connect, epoch) = {
            let mut state = self.state.lock().expect("physical NNTP pool lock");
            loop {
                cancellation_checkpoint(cancelled)?;
                let lease_limit = state.connection_limit
                    - usize::from(reserve_interactive && state.connection_limit > 1);
                if state.leased < lease_limit {
                    state.leased += 1;
                    if let Some(connection) = state.idle.pop() {
                        debug_assert_eq!(connection.pool_epoch, state.epoch);
                        break (Some(connection), state.epoch);
                    }
                    if state.open < state.connection_limit {
                        state.open += 1;
                        break (None, state.epoch);
                    }
                    state.leased -= 1;
                }
                state = self
                    .changed
                    .wait_timeout(state, Duration::from_millis(25))
                    .expect("physical NNTP pool wait")
                    .0;
            }
        };
        if let Some(connection) = should_connect {
            return Ok(connection);
        }
        if let Err(code) = self.global.acquire(cancelled, reclaim_idle) {
            let mut state = self.state.lock().expect("physical NNTP pool lock");
            state.leased -= 1;
            state.open -= 1;
            self.changed.notify_one();
            return Err(code);
        }
        match NntpConnection::open(request, cancelled) {
            Ok(mut connection) => {
                connection.pool_epoch = epoch;
                Ok(connection)
            }
            Err(code) => {
                let mut state = self.state.lock().expect("physical NNTP pool lock");
                state.leased -= 1;
                state.open -= 1;
                drop(state);
                self.global.release();
                if !matches!(
                    code,
                    "nntp_address_denied"
                        | "nntp_dns_failed"
                        | "nntp_dns_timeout"
                        | "nntp_dns_busy"
                        | "nntp_dns_unavailable"
                        | "nntp_connect_failed"
                        | "nntp_cancelled"
                ) {
                    self.poisoned_connections.fetch_add(1, Ordering::Relaxed);
                }
                self.changed.notify_one();
                Err(code)
            }
        }
    }

    fn finish(&self, connection: NntpConnection, healthy: bool) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        state.leased -= 1;
        if healthy && connection.pool_epoch == state.epoch && state.open <= state.connection_limit {
            state.idle.push(connection);
            self.global.notify_reclaimable();
        } else {
            state.open -= 1;
            self.global.release();
            if !healthy {
                self.poisoned_connections.fetch_add(1, Ordering::Relaxed);
            }
        }
        self.changed.notify_one();
    }

    fn retire(&self, _connection: NntpConnection) {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        state.leased -= 1;
        state.open -= 1;
        drop(state);
        self.global.release();
        self.changed.notify_one();
    }

    fn discard_one_idle(&self) -> bool {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        if state.idle.pop().is_none() {
            return false;
        }
        state.open -= 1;
        drop(state);
        self.global.release();
        self.changed.notify_one();
        true
    }

    fn retire_if_idle(&self) -> bool {
        let mut state = self.state.lock().expect("physical NNTP pool lock");
        if state.leased != 0
            || state.dispatchers != 0
            || !state.pipeline_queue.is_empty()
            || !state.references.is_empty()
        {
            return false;
        }
        let removed = state.idle.len();
        state.idle.clear();
        state.open = 0;
        drop(state);
        for _ in 0..removed {
            self.global.release();
        }
        true
    }
}

fn rounded_article_cost(declared_encoded_bytes: u64) -> Result<u64, &'static str> {
    if declared_encoded_bytes == 0 || declared_encoded_bytes > MAX_DECLARED_ARTICLE_BYTES {
        return Err("invalid_nntp_article_cost");
    }
    declared_encoded_bytes
        .checked_add(ARTICLE_RESERVATION_QUANTUM - 1)
        .map(|bytes| bytes / ARTICLE_RESERVATION_QUANTUM * ARTICLE_RESERVATION_QUANTUM)
        .ok_or("invalid_nntp_article_cost")
}

fn article_read_limit(maximum_bytes: usize) -> Result<usize, &'static str> {
    let hard_limit =
        usize::try_from(MAX_DECLARED_ARTICLE_BYTES).map_err(|_| "invalid_nntp_article_cost")?;
    (1..=hard_limit)
        .contains(&maximum_bytes)
        .then_some(maximum_bytes)
        .ok_or("invalid_nntp_article_cost")
}

fn effective_work_class(task: &PipelineTask) -> WorkClass {
    let promoted = match task.cancellation.priority() {
        crate::cache::FlightPriority::Interactive => WorkClass::Interactive,
        crate::cache::FlightPriority::Preparation => WorkClass::Preparation,
        crate::cache::FlightPriority::Background => WorkClass::Background,
    };
    task.scheduling.work_class.min(promoted)
}

fn take_fair_task(state: &mut PhysicalPoolState) -> PipelineTask {
    let work_class = state
        .pipeline_queue
        .iter()
        .map(effective_work_class)
        .min()
        .expect("non-empty physical NNTP pipeline");
    let partitions = state
        .pipeline_queue
        .iter()
        .filter(|task| effective_work_class(task) == work_class)
        .map(|task| task.scheduling.configuration_partition)
        .collect::<BTreeSet<_>>();
    let partition = next_round_robin(&partitions, state.last_partition.get(&work_class));
    state.last_partition.insert(work_class, partition);
    let sessions = state
        .pipeline_queue
        .iter()
        .filter(|task| {
            effective_work_class(task) == work_class
                && task.scheduling.configuration_partition == partition
        })
        .map(|task| task.scheduling.session.clone())
        .collect::<BTreeSet<_>>();
    let cursor = (work_class, partition);
    let session = next_round_robin(&sessions, state.last_session.get(&cursor));
    state.last_session.insert(cursor, session.clone());
    if state.last_session.len() > MAX_PIPELINE_QUEUE {
        state.last_session.retain(|(class, partition), session| {
            state.pipeline_queue.iter().any(|task| {
                effective_work_class(task) == *class
                    && task.scheduling.configuration_partition == *partition
                    && task.scheduling.session == *session
            })
        });
    }
    let index = state
        .pipeline_queue
        .iter()
        .position(|task| {
            effective_work_class(task) == work_class
                && task.scheduling.configuration_partition == partition
                && task.scheduling.session == session
        })
        .expect("selected physical NNTP scheduling key");
    state
        .pipeline_queue
        .remove(index)
        .expect("selected physical NNTP task")
}

fn take_compatible_fair_task(
    state: &mut PhysicalPoolState,
    first: &PipelineTask,
) -> Option<PipelineTask> {
    let work_class = effective_work_class(first);
    let partition = first.scheduling.configuration_partition;
    let requirement = first._pool_reference.inner.group_routing.snapshot();
    let sessions = state
        .pipeline_queue
        .iter()
        .filter(|task| {
            effective_work_class(task) == work_class
                && task.scheduling.configuration_partition == partition
                && pipeline_compatible_with(first, task, requirement)
        })
        .map(|task| task.scheduling.session.clone())
        .collect::<BTreeSet<_>>();
    if sessions.is_empty() {
        return None;
    }
    let cursor = (work_class, partition);
    let session = next_round_robin(&sessions, state.last_session.get(&cursor));
    state.last_session.insert(cursor, session.clone());
    let index = state
        .pipeline_queue
        .iter()
        .position(|task| {
            effective_work_class(task) == work_class
                && task.scheduling.configuration_partition == partition
                && task.scheduling.session == session
                && pipeline_compatible_with(first, task, requirement)
        })
        .expect("selected compatible scheduled task");
    state.pipeline_queue.remove(index)
}

fn next_round_robin<T>(values: &BTreeSet<T>, previous: Option<&T>) -> T
where
    T: Clone + Ord,
{
    previous
        .and_then(|previous| {
            values
                .range((
                    std::ops::Bound::Excluded(previous),
                    std::ops::Bound::Unbounded,
                ))
                .next()
        })
        .or_else(|| values.first())
        .expect("non-empty round-robin values")
        .clone()
}

fn pipeline_compatible_with(
    first: &PipelineTask,
    next: &PipelineTask,
    requirement: GroupRequirement,
) -> bool {
    if first._pool_reference.inner.lane != next._pool_reference.inner.lane
        || !Arc::ptr_eq(
            &first._pool_reference.inner.group_routing,
            &next._pool_reference.inner.group_routing,
        )
    {
        return false;
    }
    match requirement {
        GroupRequirement::Unknown => first.group.is_none() && next.group.is_none(),
        GroupRequirement::NotRequired => true,
        GroupRequirement::Required => first.group == next.group,
    }
}

fn recompute_pool_limits(state: &mut PhysicalPoolState) -> usize {
    let previous_connection_limit = state.connection_limit;
    state.connection_limit = state
        .references
        .values()
        .map(|(connections, _)| *connections)
        .min()
        .unwrap_or(0);
    state.pipeline_depth = state
        .references
        .values()
        .map(|(_, pipeline)| *pipeline)
        .min()
        .unwrap_or(1);
    if state.connection_limit >= previous_connection_limit || state.open <= state.connection_limit {
        return 0;
    }
    let excess = state.open - state.connection_limit;
    let removed = excess.min(state.idle.len());
    state.idle.drain(..removed).for_each(drop);
    state.open = state
        .open
        .checked_sub(removed)
        .expect("physical NNTP idle accounting");
    if state.open > state.connection_limit {
        state.epoch = state.epoch.wrapping_add(1);
    }
    removed
}

#[derive(Clone)]
pub struct PoolReference {
    inner: Arc<PoolReferenceInner>,
}

#[derive(Eq, PartialEq)]
struct PipelineLane {
    generation: String,
    priority: u16,
    backup: bool,
}

#[derive(Default)]
struct ProviderPerformance {
    latency_micros: AtomicU64,
    throughput_bytes_per_second: AtomicU64,
    recent_miss_ppm: AtomicU64,
    capacity_ppm: AtomicU64,
    suppliers: AtomicU64,
    failures: AtomicU64,
    authentication_failures: AtomicU64,
    missing: AtomicU64,
}

#[derive(Clone, Copy)]
pub struct ProviderDiagnostics {
    pub suppliers: u64,
    pub failures: u64,
    pub authentication_failures: u64,
    pub missing: u64,
}

impl ProviderPerformance {
    fn update_ewma(metric: &AtomicU64, sample: u64) {
        let _ = metric.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
            Some(if current == 0 {
                sample.max(1)
            } else {
                (current * 7 + sample + 4) / 8
            })
        });
    }

    fn observe(&self, elapsed: Duration, result: &PartResult) {
        if matches!(result, Err("nntp_cancelled")) {
            return;
        }
        let latency_micros = u64::try_from(elapsed.as_micros())
            .expect("bounded NNTP request duration")
            .max(1);
        Self::update_ewma(&self.latency_micros, latency_micros);
        match result {
            Ok(part) => {
                ProviderTelemetry::increment(&self.suppliers);
                let bytes = u64::try_from(part.bytes.len()).expect("bounded NNTP article");
                let throughput = (bytes * 1_000_000 / latency_micros).max(1);
                Self::update_ewma(&self.throughput_bytes_per_second, throughput);
                Self::update_ewma(&self.recent_miss_ppm, 1);
                Self::update_ewma(&self.capacity_ppm, 1_000_000);
            }
            Err("nntp_article_missing") => {
                ProviderTelemetry::increment(&self.missing);
                Self::update_ewma(&self.recent_miss_ppm, 1_000_000);
                Self::update_ewma(&self.capacity_ppm, 1_000_000);
            }
            Err(code) if crate::yenc::integrity_failure(code) => {
                Self::update_ewma(&self.recent_miss_ppm, 1);
                Self::update_ewma(&self.capacity_ppm, 1_000_000);
            }
            Err("native_busy" | "nntp_pipeline_capacity" | "nntp_pool_capacity") => {
                ProviderTelemetry::increment(&self.failures);
                Self::update_ewma(&self.capacity_ppm, 1);
            }
            Err(code) => {
                ProviderTelemetry::increment(&self.failures);
                if authentication_failure(code) {
                    ProviderTelemetry::increment(&self.authentication_failures);
                }
                Self::update_ewma(&self.capacity_ppm, 1);
            }
        }
    }

    fn hedge_delay(&self, declared_encoded_bytes: u64) -> Duration {
        let latency = self.latency_micros.load(Ordering::Relaxed);
        if latency == 0 {
            return DEFAULT_HEDGE_DELAY;
        }
        let throughput = self.throughput_bytes_per_second.load(Ordering::Relaxed);
        let transfer = if throughput == 0 {
            0
        } else {
            declared_encoded_bytes * 1_000_000 / throughput
        };
        let miss = self.recent_miss_ppm.load(Ordering::Relaxed);
        let capacity = self.capacity_ppm.load(Ordering::Relaxed);
        let confidence = (1_000_000 - miss / 2).min(500_000 + capacity / 2);
        let learned = (latency + transfer) * 2 * confidence / 1_000_000;
        Duration::from_micros(learned).clamp(MIN_HEDGE_DELAY, MAX_HEDGE_DELAY)
    }

    fn diagnostics(&self) -> ProviderDiagnostics {
        ProviderDiagnostics {
            suppliers: self.suppliers.load(Ordering::Relaxed),
            failures: self.failures.load(Ordering::Relaxed),
            authentication_failures: self.authentication_failures.load(Ordering::Relaxed),
            missing: self.missing.load(Ordering::Relaxed),
        }
    }
}

struct PoolReferenceInner {
    pool: Arc<PhysicalPool>,
    reference_id: u64,
    lane: PipelineLane,
    group_routing: Arc<GroupRouting>,
    performance: Arc<ProviderPerformance>,
}

impl PoolReference {
    pub(crate) fn hedge_delay(&self, declared_encoded_bytes: u64) -> Duration {
        self.inner.performance.hedge_delay(declared_encoded_bytes)
    }

    pub(crate) fn diagnostics(&self) -> ProviderDiagnostics {
        self.inner.performance.diagnostics()
    }
}

impl Drop for PoolReferenceInner {
    fn drop(&mut self) {
        self.pool.remove_reference(self.reference_id);
    }
}

pub struct PoolRegistry {
    global: Arc<GlobalConnectionLimiter>,
    pools: Mutex<HashMap<PhysicalPoolKey, Arc<PhysicalPool>>>,
    maximum_pool_entries: usize,
    poisoned_connections: Arc<AtomicU64>,
    provider_telemetry: Arc<ProviderTelemetry>,
    next_reference_id: AtomicU64,
}

pub struct PoolStats {
    pub pools: usize,
    pub open: usize,
    pub active: usize,
    pub idle: usize,
    pub queued_interactive: usize,
    pub queued_preparation: usize,
    pub queued_background: usize,
    pub reserved_commands: usize,
    pub reserved_encoded_bytes: u64,
    pub reserved_decoded_bytes: u64,
    pub scheduler_busy_rejections: u64,
    pub poisoned: u64,
    pub provider_attempts: u64,
    pub provider_suppliers: u64,
    pub provider_hits: u64,
    pub provider_missing: u64,
    pub provider_corrupt: u64,
    pub provider_failures: u64,
    pub provider_cancellations: u64,
    pub provider_failovers: u64,
}

#[cfg(test)]
#[derive(Debug)]
pub struct ConnectionTestError {
    pub phase: &'static str,
    pub code: &'static str,
}

impl PoolRegistry {
    #[cfg(test)]
    pub fn new(maximum_connections: usize) -> Result<Self, &'static str> {
        let maximum_decoded_bytes = u64::try_from(maximum_connections)
            .ok()
            .and_then(|connections| connections.checked_mul(MAX_DECLARED_ARTICLE_BYTES))
            .ok_or("invalid_nntp_connection_limit")?;
        Self::new_with_decoded_budget(maximum_connections, maximum_decoded_bytes)
    }

    pub fn new_with_decoded_budget(
        maximum_connections: usize,
        maximum_decoded_bytes: u64,
    ) -> Result<Self, &'static str> {
        let maximum_pool_entries = maximum_connections
            .saturating_mul(64)
            .clamp(256, MAX_PHYSICAL_POOL_ENTRIES);
        let poisoned_connections = Arc::new(AtomicU64::new(0));
        Ok(Self {
            global: Arc::new(GlobalConnectionLimiter::new(
                maximum_connections,
                maximum_decoded_bytes,
            )?),
            pools: Mutex::new(HashMap::new()),
            maximum_pool_entries,
            poisoned_connections,
            provider_telemetry: Arc::new(ProviderTelemetry::default()),
            next_reference_id: AtomicU64::new(1),
        })
    }

    fn physical_pool(&self, request: &BodyRequest) -> Result<Arc<PhysicalPool>, &'static str> {
        let key = PhysicalPoolKey::from_request(request);
        let mut pools = self.pools.lock().expect("NNTP pool registry lock");
        if let Some(pool) = pools.get(&key) {
            return Ok(Arc::clone(pool));
        }
        if pools.len() >= self.maximum_pool_entries {
            let retired = pools.iter().find_map(|(key, pool)| {
                (Arc::strong_count(pool) == 1 && pool.retire_if_idle()).then(|| key.clone())
            });
            let Some(retired) = retired else {
                return Err("nntp_pool_registry_busy");
            };
            pools.remove(&retired);
        }
        let pool = Arc::new(PhysicalPool::new(
            Arc::clone(&self.global),
            Arc::clone(&self.poisoned_connections),
        ));
        pools.insert(key, Arc::clone(&pool));
        Ok(pool)
    }

    pub fn reference_for_generation(
        &self,
        request: &BodyRequest,
        connection_limit: usize,
        pipeline_depth: usize,
        generation: &str,
        priority: u16,
        backup: bool,
    ) -> Result<PoolReference, &'static str> {
        let pool = self.physical_pool(request)?;
        let reference_id = self
            .next_reference_id
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.checked_add(1)
            })
            .map_err(|_| "nntp_pool_reference_capacity")?;
        pool.add_reference(reference_id, connection_limit, pipeline_depth);
        Ok(PoolReference {
            inner: Arc::new(PoolReferenceInner {
                pool,
                reference_id,
                lane: PipelineLane {
                    generation: generation.to_owned(),
                    priority,
                    backup,
                },
                group_routing: Arc::new(GroupRouting::new()),
                performance: Arc::new(ProviderPerformance::default()),
            }),
        })
    }

    #[cfg(test)]
    pub fn reference(
        &self,
        request: &BodyRequest,
        connection_limit: usize,
        pipeline_depth: usize,
    ) -> Result<PoolReference, &'static str> {
        if connection_limit == 0 || pipeline_depth == 0 || pipeline_depth > MAX_PIPELINE_DEPTH {
            return Err("invalid_nntp_connection_limit");
        }
        validate_body_template(request)?;
        self.reference_for_generation(
            request,
            connection_limit,
            pipeline_depth,
            "test-generation",
            0,
            false,
        )
    }

    pub fn stats(&self) -> PoolStats {
        let pools = self.pools.lock().expect("NNTP pool registry lock");
        let mut result = PoolStats {
            pools: pools.len(),
            open: 0,
            active: 0,
            idle: 0,
            queued_interactive: 0,
            queued_preparation: 0,
            queued_background: 0,
            reserved_commands: 0,
            reserved_encoded_bytes: 0,
            reserved_decoded_bytes: 0,
            scheduler_busy_rejections: self
                .provider_telemetry
                .scheduler_busy_rejections
                .load(Ordering::Relaxed),
            poisoned: self.poisoned_connections.load(Ordering::Relaxed),
            provider_attempts: self
                .provider_telemetry
                .provider_attempts
                .load(Ordering::Relaxed),
            provider_suppliers: self
                .provider_telemetry
                .provider_suppliers
                .load(Ordering::Relaxed),
            provider_hits: self
                .provider_telemetry
                .provider_hits
                .load(Ordering::Relaxed),
            provider_missing: self
                .provider_telemetry
                .provider_missing
                .load(Ordering::Relaxed),
            provider_corrupt: self
                .provider_telemetry
                .provider_corrupt
                .load(Ordering::Relaxed),
            provider_failures: self
                .provider_telemetry
                .provider_failures
                .load(Ordering::Relaxed),
            provider_cancellations: self
                .provider_telemetry
                .provider_cancellations
                .load(Ordering::Relaxed),
            provider_failovers: self
                .provider_telemetry
                .provider_failovers
                .load(Ordering::Relaxed),
        };
        for pool in pools.values() {
            let state = pool.state.lock().expect("physical NNTP pool lock");
            result.open += state.open;
            result.active += state.leased;
            result.idle += state.idle.len();
            result.reserved_commands += state.reserved_commands;
            result.reserved_encoded_bytes += state.reserved_encoded_bytes;
            result.reserved_decoded_bytes += state.reserved_decoded_bytes;
            let mut queued_interactive = 0usize;
            let mut queued_preparation = 0usize;
            let mut queued_background = 0usize;
            for task in &state.pipeline_queue {
                match effective_work_class(task) {
                    WorkClass::Interactive => queued_interactive += 1,
                    WorkClass::Preparation => queued_preparation += 1,
                    WorkClass::Background => queued_background += 1,
                }
            }
            result.queued_interactive += queued_interactive;
            result.queued_preparation += queued_preparation;
            result.queued_background += queued_background;
        }
        result
    }

    pub fn preparation_slots(&self) -> usize {
        self.global.maximum
    }

    pub(crate) fn record_provider_failover(&self) {
        ProviderTelemetry::increment(&self.provider_telemetry.provider_failovers);
    }

    #[cfg(test)]
    pub fn test_reference(
        self: &Arc<Self>,
        pool_reference: &PoolReference,
        request: &BodyRequest,
    ) -> Result<String, ConnectionTestError> {
        self.test_reference_cancellable(pool_reference, request, &|| false)
    }

    #[cfg(test)]
    pub(crate) fn test_reference_cancellable<F>(
        self: &Arc<Self>,
        pool_reference: &PoolReference,
        request: &BodyRequest,
        cancelled: &F,
    ) -> Result<String, ConnectionTestError>
    where
        F: Fn() -> bool,
    {
        let pool = Arc::clone(&pool_reference.inner.pool);
        let mut connection = pool
            .checkout(request, cancelled, &|| self.reclaim_one_idle())
            .map_err(|code| ConnectionTestError {
                phase: connection_test_phase(code),
                code,
            })?;
        match connection.date_cancellable(cancelled) {
            Ok(date) => {
                pool.finish(connection, true);
                Ok(date)
            }
            Err(code) => {
                pool.finish(connection, false);
                Err(ConnectionTestError {
                    phase: if authentication_failure(code) {
                        "authentication"
                    } else {
                        "date"
                    },
                    code,
                })
            }
        }
    }

    fn reclaim_one_idle(&self) -> bool {
        let pools = self.pools.lock().expect("NNTP pool registry lock");
        pools.values().any(|pool| pool.discard_one_idle())
    }

    pub fn stat_batch<F>(
        &self,
        pool_reference: &PoolReference,
        request: &BodyRequest,
        group: Option<&str>,
        message_ids: &[&str],
        cancelled: &F,
    ) -> Result<Vec<bool>, &'static str>
    where
        F: Fn() -> bool,
    {
        let pool = Arc::clone(&pool_reference.inner.pool);
        let mut connection =
            pool.checkout_preparation(request, cancelled, &|| self.reclaim_one_idle())?;
        let result = connection.stat_batch(message_ids, group, cancelled);
        pool.finish(connection, result.is_ok());
        result
    }

    #[cfg(test)]
    pub fn verified_part(
        self: &Arc<Self>,
        pool_reference: PoolReference,
        request: BodyRequest,
        group: Option<String>,
        maximum_bytes: usize,
        cancellation: FlightCancellation,
    ) -> Result<crate::yenc::DecodedPart, &'static str> {
        let mut request = request;
        let mut partition = [0_u8; 32];
        partition.copy_from_slice(&Sha256::digest(
            pool_reference.inner.lane.generation.as_bytes(),
        ));
        request.message_id = canonical_message_id(&request.message_id)?.to_owned();
        let session = request.message_id.clone();
        self.verified_part_scheduled(
            pool_reference,
            request,
            group,
            maximum_bytes,
            cancellation,
            SchedulingContext {
                work_class: WorkClass::Interactive,
                configuration_partition: partition,
                session,
                declared_encoded_bytes: u64::try_from(maximum_bytes)
                    .map_err(|_| "invalid_nntp_article_cost")?,
            },
        )
    }

    pub fn verified_part_scheduled(
        self: &Arc<Self>,
        pool_reference: PoolReference,
        request: BodyRequest,
        group: Option<String>,
        maximum_bytes: usize,
        cancellation: FlightCancellation,
        scheduling: SchedulingContext,
    ) -> Result<crate::yenc::DecodedPart, &'static str> {
        let started = Instant::now();
        let performance = Arc::clone(&pool_reference.inner.performance);
        let result = self.verified_part_scheduled_inner(
            pool_reference,
            request,
            group,
            maximum_bytes,
            cancellation,
            scheduling,
        );
        performance.observe(started.elapsed(), &result);
        result
    }

    fn verified_part_scheduled_inner(
        self: &Arc<Self>,
        pool_reference: PoolReference,
        request: BodyRequest,
        group: Option<String>,
        maximum_bytes: usize,
        cancellation: FlightCancellation,
        scheduling: SchedulingContext,
    ) -> Result<crate::yenc::DecodedPart, &'static str> {
        let telemetry = Arc::clone(&self.provider_telemetry);
        let pool = Arc::clone(&pool_reference.inner.pool);
        let article_limit = article_read_limit(maximum_bytes)?;
        let reservation =
            match pool.reserve_pipeline(scheduling.declared_encoded_bytes, scheduling.work_class) {
                Ok(reservation) => reservation,
                Err(code) => {
                    ProviderTelemetry::increment(&telemetry.scheduler_busy_rejections);
                    return Err(code);
                }
            };
        let decoded_limit = reservation.decoded_bytes;
        let (result, receive_result) = mpsc::sync_channel(1);
        let caller_cancellation = cancellation.clone();
        let start_dispatcher = pool.enqueue(PipelineTask {
            request,
            group,
            maximum_wire_bytes: article_limit,
            maximum_decoded_bytes: usize::try_from(decoded_limit)
                .map_err(|_| "invalid_nntp_article_cost")?,
            cancellation,
            scheduling,
            _reservation: Some(reservation),
            _pool_reference: pool_reference,
            result,
        })?;
        ProviderTelemetry::increment(&telemetry.provider_attempts);
        if start_dispatcher && !self.spawn_pipeline_dispatcher(Arc::clone(&pool)) {
            self.drive_pipeline(Arc::clone(&pool));
        }
        loop {
            if let Err(code) = caller_cancellation.checkpoint() {
                ProviderTelemetry::increment(&telemetry.provider_cancellations);
                return Err(code);
            }
            match receive_result.recv_timeout(Duration::from_millis(25)) {
                Ok(result) => {
                    match &result {
                        Ok(_) => {
                            ProviderTelemetry::increment(&telemetry.provider_suppliers);
                            ProviderTelemetry::increment(&telemetry.provider_hits);
                        }
                        Err("nntp_article_missing") => {
                            ProviderTelemetry::increment(&telemetry.provider_missing);
                        }
                        Err(code) if crate::yenc::integrity_failure(code) => {
                            ProviderTelemetry::increment(&telemetry.provider_corrupt);
                        }
                        Err("nntp_cancelled") => {
                            ProviderTelemetry::increment(&telemetry.provider_cancellations);
                        }
                        Err(_) => {
                            ProviderTelemetry::increment(&telemetry.provider_failures);
                        }
                    }
                    return result;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    ProviderTelemetry::increment(&telemetry.provider_failures);
                    return Err("nntp_pipeline_unavailable");
                }
            }
        }
    }

    fn spawn_pipeline_dispatcher(self: &Arc<Self>, pool: Arc<PhysicalPool>) -> bool {
        let registry = Arc::clone(self);
        thread::Builder::new()
            .name("usenet-nntp-pipeline".into())
            .spawn(move || registry.drive_pipeline(pool))
            .is_ok()
    }

    fn drive_pipeline(self: &Arc<Self>, pool: Arc<PhysicalPool>) {
        'dispatch: while let Some((batch, start_dispatcher)) = pool.take_pipeline_batch() {
            if start_dispatcher && !self.spawn_pipeline_dispatcher(Arc::clone(&pool)) {
                pool.cancel_dispatcher_reservation();
            }
            let mut active = batch
                .into_iter()
                .filter_map(|mut task| {
                    if task.cancellation.is_cancelled() {
                        task.complete(Err("nntp_cancelled"));
                        None
                    } else {
                        Some(task)
                    }
                })
                .collect::<Vec<_>>();
            if active.is_empty() {
                pool.complete_pipeline_batch();
                continue;
            }
            let mut group_dispatch = match active[0]
                ._pool_reference
                .inner
                .group_routing
                .dispatch(&active[0].cancellation)
            {
                Ok(dispatch) => dispatch,
                Err(code) => {
                    send_pipeline_failure(&mut active, 0, code);
                    pool.complete_pipeline_batch();
                    continue;
                }
            };
            let mut connection = match pool.checkout(
                &active[0].request,
                &|| active.iter().all(|task| task.cancellation.is_cancelled()),
                &|| self.reclaim_one_idle(),
            ) {
                Ok(connection) => connection,
                Err(code) => {
                    send_pipeline_failure(&mut active, 0, code);
                    pool.complete_pipeline_batch();
                    continue;
                }
            };
            while group_dispatch.requirement == GroupRequirement::Unknown
                && connection.selected_group.is_some()
            {
                pool.retire(connection);
                connection = match pool.checkout(
                    &active[0].request,
                    &|| active.iter().all(|task| task.cancellation.is_cancelled()),
                    &|| self.reclaim_one_idle(),
                ) {
                    Ok(connection) => connection,
                    Err(code) => {
                        send_pipeline_failure(&mut active, 0, code);
                        pool.complete_pipeline_batch();
                        continue 'dispatch;
                    }
                };
            }
            let mut start = 0usize;
            let mut reconnected = false;
            loop {
                let outcome =
                    connection.pipeline_attempt(&mut active[start..], &mut group_dispatch, None);
                match outcome {
                    PipelineOutcome::Healthy => {
                        pool.finish(connection, true);
                        break;
                    }
                    PipelineOutcome::Unhealthy => {
                        pool.finish(connection, false);
                        break;
                    }
                    PipelineOutcome::ReconnectFrom(offset, _) if !reconnected => {
                        pool.finish(connection, false);
                        start += offset;
                        reconnected = true;
                        connection = match pool.checkout(
                            &active[start].request,
                            &|| {
                                active[start..]
                                    .iter()
                                    .all(|task| task.cancellation.is_cancelled())
                            },
                            &|| self.reclaim_one_idle(),
                        ) {
                            Ok(connection) => connection,
                            Err(code) => {
                                send_pipeline_failure(&mut active, start, code);
                                break;
                            }
                        };
                    }
                    PipelineOutcome::ReconnectFrom(offset, code) => {
                        send_pipeline_failure(&mut active, start + offset, code);
                        pool.finish(connection, false);
                        break;
                    }
                }
            }
            pool.complete_pipeline_batch();
        }
    }
}

#[cfg(test)]
fn connection_test_phase(code: &'static str) -> &'static str {
    match code {
        "nntp_invalid_request"
        | "nntp_address_denied"
        | "nntp_dns_failed"
        | "nntp_dns_timeout"
        | "nntp_dns_busy"
        | "nntp_dns_unavailable" => "dns_address_policy",
        "nntp_connect_failed" | "nntp_timeout_configuration_failed" => "tcp",
        "nntp_tls_failed"
        | "nntp_tls_name_invalid"
        | "nntp_tls_roots_failed"
        | "nntp_starttls_failed"
        | "nntp_starttls_unavailable" => "tls_certificate",
        "nntp_auth_failed" | "nntp_auth_required" => "authentication",
        "nntp_reader_failed" => "reader_mode",
        _ => "greeting_capabilities",
    }
}

fn line_cancellable<F>(
    reader: &mut Reader,
    cancelled: &F,
    timeout: Duration,
) -> Result<Vec<u8>, &'static str>
where
    F: Fn() -> bool,
{
    let mut value = Vec::new();
    line_into_cancellable(reader, &mut value, cancelled, timeout)?;
    Ok(value)
}

fn line_into_cancellable<F>(
    reader: &mut Reader,
    scratch: &mut Vec<u8>,
    cancelled: &F,
    timeout: Duration,
) -> Result<(), &'static str>
where
    F: Fn() -> bool,
{
    scratch.clear();
    let deadline = Instant::now() + timeout;
    loop {
        cancellation_checkpoint(cancelled)?;
        if Instant::now() >= deadline {
            return Err("nntp_read_failed");
        }
        match reader.fill_buf() {
            Ok([]) => return Err("nntp_invalid_response"),
            Ok(available) => {
                let consumed = available
                    .iter()
                    .position(|byte| *byte == b'\n')
                    .map_or(available.len(), |index| index + 1);
                if scratch.len() + consumed > MAX_LINE_BYTES {
                    return Err("nntp_invalid_response");
                }
                scratch.extend_from_slice(&available[..consumed]);
                reader.consume(consumed);
                if scratch.ends_with(b"\n") {
                    break;
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                cancellation_checkpoint(cancelled)?;
                if Instant::now() >= deadline {
                    return Err("nntp_read_failed");
                }
            }
            Err(_) => return Err("nntp_read_failed"),
        }
    }
    if scratch.is_empty() || scratch.len() > MAX_LINE_BYTES || !scratch.ends_with(b"\r\n") {
        return Err("nntp_invalid_response");
    }
    Ok(())
}

fn code(value: &[u8]) -> Result<u16, &'static str> {
    crate::nntp_protocol::status_code(value)
}

fn authentication_status_failure(status: u16) -> Option<&'static str> {
    match status {
        480 => Some("nntp_auth_required"),
        481 => Some("nntp_auth_failed"),
        _ => None,
    }
}

fn command_cancellable<F>(
    reader: &mut Reader,
    value: &str,
    cancelled: &F,
    timeout: Duration,
) -> Result<u16, &'static str>
where
    F: Fn() -> bool,
{
    command_cancellable_until(reader, value, cancelled, Instant::now() + timeout)
}

fn command_cancellable_until<F>(
    reader: &mut Reader,
    value: &str,
    cancelled: &F,
    deadline: Instant,
) -> Result<u16, &'static str>
where
    F: Fn() -> bool,
{
    cancellation_checkpoint(cancelled)?;
    reader
        .get_mut()
        .write_all_cancellable_until(value.as_bytes(), cancelled, deadline)?;
    let timeout = deadline
        .checked_duration_since(Instant::now())
        .filter(|timeout| !timeout.is_zero())
        .ok_or("nntp_read_failed")?;
    code(&line_cancellable(reader, cancelled, timeout)?)
}

struct Capabilities {
    starttls: bool,
    deflate: bool,
}

fn capabilities_cancellable<F>(
    reader: &mut Reader,
    cancelled: &F,
) -> Result<Capabilities, &'static str>
where
    F: Fn() -> bool,
{
    capabilities_cancellable_until(reader, cancelled, Instant::now() + TIMEOUT)
}

fn capabilities_cancellable_until<F>(
    reader: &mut Reader,
    cancelled: &F,
    deadline: Instant,
) -> Result<Capabilities, &'static str>
where
    F: Fn() -> bool,
{
    reader
        .get_mut()
        .begin_compressed_read_budget(CAPABILITIES_COMPRESSED_READ_LIMIT);
    if command_cancellable_until(reader, "CAPABILITIES\r\n", cancelled, deadline)? != 101 {
        return Err("nntp_capabilities_failed");
    }
    let mut lines = 0;
    let mut starttls = false;
    let mut deflate = false;
    loop {
        let timeout = deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or("nntp_read_failed")?;
        let value = line_cancellable(reader, cancelled, timeout)?;
        if value == b".\r\n" {
            return Ok(Capabilities { starttls, deflate });
        }
        if lines == MAX_CAPABILITY_LINES {
            return Err("nntp_capabilities_failed");
        }
        lines += 1;
        let value = value
            .strip_suffix(b"\r\n")
            .expect("validated NNTP response line");
        let mut fields = value
            .split(|byte| byte.is_ascii_whitespace())
            .filter(|field| !field.is_empty());
        let Some(name) = fields.next() else {
            continue;
        };
        starttls |= name.eq_ignore_ascii_case(b"STARTTLS");
        deflate |= name.eq_ignore_ascii_case(b"COMPRESS")
            && fields.any(|field| field.eq_ignore_ascii_case(b"DEFLATE"));
    }
}

fn validate_request(
    host: &str,
    tls_mode: &str,
    username: Option<&str>,
    password: Option<&str>,
    message_id: &str,
) -> Result<(), &'static str> {
    if !valid_text(host, 253) || canonical_message_id(message_id).is_err() {
        return Err("nntp_invalid_request");
    }
    if !matches!(tls_mode, "implicit" | "starttls" | "plaintext") {
        return Err("nntp_invalid_request");
    }
    if username.is_some() != password.is_some()
        || username.is_some_and(|value| !valid_text(value, 512))
        || password.is_some_and(|value| !valid_text(value, 512))
    {
        return Err("nntp_invalid_request");
    }
    Ok(())
}

pub(crate) fn canonical_message_id(value: &str) -> Result<&str, &'static str> {
    if value.is_empty() || value.len() > 998 {
        return Err("nntp_invalid_request");
    }
    let bracketed = value.starts_with('<');
    if bracketed != value.ends_with('>') {
        return Err("nntp_invalid_request");
    }
    let value = if bracketed {
        &value[1..value.len() - 1]
    } else {
        value
    };
    if !valid_text(value, 996) || value.bytes().any(|byte| matches!(byte, b'<' | b'>')) {
        return Err("nntp_invalid_request");
    }
    Ok(value)
}

fn resolve_blocking(host: &str, port: u16) -> Resolution {
    let mut addresses = (host, port)
        .to_socket_addrs()
        .map_err(|_| "nntp_dns_failed")?;
    let mut result = Vec::new();
    for address in addresses.by_ref().take(MAX_RESOLVED_ADDRESSES) {
        if !result.contains(&address) {
            result.push(address);
        }
    }
    if result.is_empty() {
        return Err("nntp_dns_failed");
    }
    Ok(result)
}

fn wait_for_resolution<F>(
    receiver: &mpsc::Receiver<Resolution>,
    cancelled: &F,
    deadline: Instant,
) -> Resolution
where
    F: Fn() -> bool,
{
    loop {
        cancellation_checkpoint(cancelled)?;
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .filter(|remaining| !remaining.is_zero())
            .ok_or("nntp_dns_timeout")?;
        match receiver.recv_timeout(remaining.min(CANCELLATION_POLL)) {
            Ok(result) => {
                cancellation_checkpoint(cancelled)?;
                if Instant::now() >= deadline {
                    return Err("nntp_dns_timeout");
                }
                return result;
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("nntp_dns_unavailable");
            }
        }
    }
}

fn resolve_cancellable<F>(host: &str, port: u16, cancelled: &F) -> Resolution
where
    F: Fn() -> bool,
{
    if let Ok(address) = host.parse::<IpAddr>() {
        cancellation_checkpoint(cancelled)?;
        return Ok(vec![SocketAddr::new(address, port)]);
    }
    let deadline = Instant::now() + TIMEOUT;
    let resolver = DNS_RESOLVER
        .get_or_init(Resolver::start)
        .as_ref()
        .map_err(|code| *code)?;
    let (result, receiver) = mpsc::sync_channel(1);
    let abandoned = Arc::new(AtomicBool::new(false));
    resolver.submit(ResolverJob {
        host: host.to_owned(),
        port,
        deadline,
        abandoned: Arc::clone(&abandoned),
        result,
    })?;
    let resolution = wait_for_resolution(&receiver, cancelled, deadline);
    abandoned.store(true, Ordering::Release);
    resolution
}

fn connect<F>(
    host: &str,
    port: u16,
    tls_mode: &str,
    allow_private: bool,
    username: Option<&str>,
    password: Option<&str>,
    cancelled: &F,
) -> Result<Reader, &'static str>
where
    F: Fn() -> bool,
{
    cancellation_checkpoint(cancelled)?;
    let addresses = resolve_cancellable(host, port, cancelled)?;
    if !allow_private
        && addresses
            .iter()
            .any(|address| !is_public_address(address.ip()))
    {
        return Err("nntp_address_denied");
    }
    let stream = connect_addresses_cancellable(&addresses, cancelled)?;
    let mut reader = if tls_mode == "implicit" {
        BufReader::new(NntpStream::Tls(Box::new(tls_stream_cancellable(
            stream, host, cancelled,
        )?)))
    } else {
        BufReader::new(NntpStream::Plain(stream))
    };
    reader.get_ref().set_read_timeout(CANCELLATION_POLL)?;
    reader.get_ref().set_write_timeout(CANCELLATION_POLL)?;
    cancellation_checkpoint(cancelled)?;
    let greeting = line_cancellable(&mut reader, cancelled, TIMEOUT)?;
    match code(&greeting)? {
        200 | 201 => {}
        _ => return Err("nntp_greeting_rejected"),
    }
    cancellation_checkpoint(cancelled)?;
    let initial_capabilities = capabilities_cancellable(&mut reader, cancelled)?;
    if tls_mode == "starttls" {
        if !initial_capabilities.starttls {
            return Err("nntp_starttls_unavailable");
        }
        if command_cancellable(&mut reader, "STARTTLS\r\n", cancelled, TIMEOUT)? != 382 {
            return Err("nntp_starttls_failed");
        }
        cancellation_checkpoint(cancelled)?;
        reader.get_ref().set_read_timeout(TIMEOUT)?;
        reader.get_ref().set_write_timeout(TIMEOUT)?;
        let stream = match reader.into_inner() {
            NntpStream::Plain(stream) => stream,
            NntpStream::Tls(_) | NntpStream::Deflate(_) => {
                return Err("nntp_starttls_failed");
            }
        };
        reader = BufReader::new(NntpStream::Tls(Box::new(tls_stream_cancellable(
            stream, host, cancelled,
        )?)));
        reader.get_ref().set_read_timeout(CANCELLATION_POLL)?;
        reader.get_ref().set_write_timeout(CANCELLATION_POLL)?;
        capabilities_cancellable(&mut reader, cancelled)?;
    }
    let authenticate = |reader: &mut Reader| -> Result<(), &'static str> {
        let (Some(username), Some(password)) = (username, password) else {
            return Err("nntp_auth_required");
        };
        let username_command = Zeroizing::new(format!("AUTHINFO USER {username}\r\n"));
        match command_cancellable(reader, &username_command, cancelled, TIMEOUT)? {
            281 => return Ok(()),
            381 => {}
            _ => return Err("nntp_auth_failed"),
        }
        let password_command = Zeroizing::new(format!("AUTHINFO PASS {password}\r\n"));
        if command_cancellable(reader, &password_command, cancelled, TIMEOUT)? != 281 {
            return Err("nntp_auth_failed");
        }
        Ok(())
    };
    cancellation_checkpoint(cancelled)?;
    match command_cancellable(&mut reader, "MODE READER\r\n", cancelled, TIMEOUT)? {
        200 | 201 => {
            if username.is_some() {
                authenticate(&mut reader)?;
            }
        }
        480 => {
            authenticate(&mut reader)?;
            if !matches!(
                command_cancellable(&mut reader, "MODE READER\r\n", cancelled, TIMEOUT)?,
                200 | 201
            ) {
                return Err("nntp_reader_failed");
            }
        }
        _ => return Err("nntp_reader_failed"),
    }
    cancellation_checkpoint(cancelled)?;
    let final_capabilities = capabilities_cancellable(&mut reader, cancelled)?;
    if final_capabilities.deflate {
        if command_cancellable(&mut reader, "COMPRESS DEFLATE\r\n", cancelled, TIMEOUT)? != 206 {
            return Err("nntp_compression_failed");
        }
        if !reader.buffer().is_empty() {
            return Err("nntp_compression_desynchronized");
        }
        reader = BufReader::new(reader.into_inner().enable_deflate());
        capabilities_cancellable(&mut reader, cancelled)?;
    }
    Ok(reader)
}

impl NntpConnection {
    fn open<F>(request: &BodyRequest, cancelled: &F) -> Result<Self, &'static str>
    where
        F: Fn() -> bool,
    {
        let reader = connect(
            &request.host,
            request.port,
            &request.tls_mode,
            request.allow_private,
            request.username.as_deref(),
            request.password.as_deref(),
            cancelled,
        )?;
        reader.get_ref().set_read_timeout(CANCELLATION_POLL)?;
        reader.get_ref().set_write_timeout(CANCELLATION_POLL)?;
        Ok(Self {
            reader,
            selected_group: None,
            pool_epoch: 0,
        })
    }

    #[cfg(test)]
    fn body<F>(
        &mut self,
        message_id: &str,
        maximum_bytes: usize,
        cancelled: &F,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        self.body_until(
            message_id,
            maximum_bytes,
            cancelled,
            Instant::now() + ARTICLE_TOTAL_TIMEOUT,
        )
    }

    #[cfg(test)]
    fn body_until<F>(
        &mut self,
        message_id: &str,
        maximum_bytes: usize,
        cancelled: &F,
        deadline: Instant,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        cancellation_checkpoint(cancelled)?;
        let compressed_budget = u64::try_from(maximum_bytes)
            .map_err(|_| "article_too_large")?
            .checked_add(COMPRESSED_FRAMING_ALLOWANCE)
            .ok_or("article_too_large")?;
        self.reader
            .get_mut()
            .begin_compressed_read_budget(compressed_budget);
        let response = article_result(
            command_cancellable_until(
                &mut self.reader,
                &format!("BODY <{}>\r\n", canonical_message_id(message_id)?),
                cancelled,
                deadline,
            ),
            deadline,
        );
        let response = response?;
        match response {
            222 => {}
            430 => return Err("nntp_article_missing"),
            status => {
                return Err(authentication_status_failure(status).unwrap_or("nntp_body_failed"));
            }
        }
        self.read_multiline_body_until(maximum_bytes, cancelled, deadline)
    }

    #[cfg(test)]
    fn date_cancellable<F>(&mut self, cancelled: &F) -> Result<String, &'static str>
    where
        F: Fn() -> bool,
    {
        cancellation_checkpoint(cancelled)?;
        self.reader
            .get_mut()
            .write_all_cancellable(b"DATE\r\n", cancelled, TIMEOUT)?;
        let line = line_cancellable(&mut self.reader, cancelled, TIMEOUT)?;
        let status = code(&line)?;
        if status != 111 {
            return Err(authentication_status_failure(status).unwrap_or("nntp_date_failed"));
        }
        let line = std::str::from_utf8(&line).map_err(|_| "nntp_date_failed")?;
        let mut fields = line.trim_end_matches("\r\n").split_ascii_whitespace();
        let _response_code = fields.next();
        let date = fields.next().ok_or("nntp_date_failed")?;
        if fields.next().is_some()
            || date.len() != 14
            || !date.bytes().all(|byte| byte.is_ascii_digit())
        {
            return Err("nntp_date_failed");
        }
        Ok(date.to_owned())
    }

    #[cfg(test)]
    fn read_multiline_body_until<F>(
        &mut self,
        maximum_bytes: usize,
        cancelled: &F,
        deadline: Instant,
    ) -> Result<Vec<u8>, &'static str>
    where
        F: Fn() -> bool,
    {
        let mut decoder = crate::nntp_protocol::MultilineBodyDecoder::new(maximum_bytes);
        let mut line_scratch = Vec::with_capacity(MAX_LINE_BYTES);
        loop {
            cancellation_checkpoint(cancelled)?;
            let timeout = article_remaining(deadline)?;
            article_result(
                line_into_cancellable(&mut self.reader, &mut line_scratch, cancelled, timeout),
                deadline,
            )?;
            if decoder.push_line(&line_scratch)? {
                return decoder.into_body();
            }
        }
    }

    fn read_yenc_body<F>(
        &mut self,
        maximum_wire_bytes: usize,
        maximum_decoded_bytes: usize,
        cancelled: &F,
        total_deadline: Option<Instant>,
    ) -> PartResult
    where
        F: Fn() -> bool,
    {
        let mut decoder = crate::yenc::Decoder::new(maximum_decoded_bytes);
        let mut decode_error = None;
        let mut wire_bytes = 0usize;
        let mut line_scratch = Vec::with_capacity(MAX_LINE_BYTES);
        loop {
            cancellation_checkpoint(cancelled)?;
            let timeout = pipeline_remaining(total_deadline)?;
            pipeline_result(
                line_into_cancellable(&mut self.reader, &mut line_scratch, cancelled, timeout),
                total_deadline,
            )?;
            let Some(value) = crate::nntp_protocol::multiline_value(&line_scratch)? else {
                return match decode_error {
                    Some(code) => Err(code),
                    None => decoder.finish(),
                };
            };
            wire_bytes = wire_bytes
                .checked_add(value.len())
                .filter(|total| *total <= maximum_wire_bytes)
                .ok_or("article_too_large")?;
            if decode_error.is_none()
                && let Err(code) = decoder.push_line(
                    value
                        .strip_suffix(b"\r\n")
                        .expect("validated NNTP response line"),
                )
            {
                decode_error = Some(code);
            }
        }
    }

    fn select_group<F>(
        &mut self,
        group: &str,
        cancelled: &F,
        deadline: Instant,
    ) -> Result<(), &'static str>
    where
        F: Fn() -> bool,
    {
        article_remaining(deadline)?;
        if self.selected_group.as_deref() == Some(group) {
            return Ok(());
        }
        let command = format!("GROUP {group}\r\n");
        match article_result(
            command_cancellable_until(&mut self.reader, &command, cancelled, deadline),
            deadline,
        )? {
            211 => {}
            411 => return Err("nntp_group_failed"),
            status => {
                return Err(authentication_status_failure(status).unwrap_or("nntp_body_failed"));
            }
        }
        self.selected_group = Some(group.to_owned());
        Ok(())
    }

    fn retry_body_in_group(
        &mut self,
        task: &PipelineTask,
        total_deadline: Option<Instant>,
    ) -> PartResult {
        let group = task.group.as_deref().ok_or("nntp_group_required")?;
        self.select_group(
            group,
            &|| task.cancellation.is_cancelled(),
            pipeline_deadline(total_deadline, TIMEOUT),
        )?;
        let command = format!("BODY <{}>\r\n", task.request.message_id);
        let deadline = pipeline_deadline(total_deadline, TIMEOUT);
        let response = pipeline_result(
            command_cancellable_until(
                &mut self.reader,
                &command,
                &|| task.cancellation.is_cancelled(),
                deadline,
            ),
            total_deadline,
        );
        let response = response?;
        match response {
            222 => self.read_yenc_body(
                task.maximum_wire_bytes,
                task.maximum_decoded_bytes,
                &|| task.cancellation.is_cancelled(),
                total_deadline,
            ),
            430 => Err("nntp_article_missing"),
            status => Err(authentication_status_failure(status).unwrap_or("nntp_body_failed")),
        }
    }

    fn stat_batch<F>(
        &mut self,
        message_ids: &[&str],
        group: Option<&str>,
        cancelled: &F,
    ) -> Result<Vec<bool>, &'static str>
    where
        F: Fn() -> bool,
    {
        if message_ids.is_empty() || message_ids.len() > MAX_PIPELINE_DEPTH {
            return Err("nntp_invalid_request");
        }
        let deadline = Instant::now() + ARTICLE_TOTAL_TIMEOUT;
        self.reader.get_mut().begin_compressed_read_budget(
            u64::try_from(MAX_LINE_BYTES * message_ids.len())
                .map_err(|_| "nntp_invalid_request")?
                .checked_add(COMPRESSED_FRAMING_ALLOWANCE)
                .ok_or("nntp_invalid_request")?,
        );
        if let Some(group) = group {
            self.select_group(group, cancelled, deadline)?;
        }
        let write_deadline = std::cmp::min(Instant::now() + TIMEOUT, deadline);
        let mut command = String::with_capacity(message_ids.len() * 64);
        use std::fmt::Write as _;
        for message_id in message_ids {
            let _ = write!(command, "STAT <{}>\r\n", canonical_message_id(message_id)?);
        }
        article_result(
            self.reader.get_mut().write_all_cancellable_until(
                command.as_bytes(),
                cancelled,
                write_deadline,
            ),
            deadline,
        )?;
        let mut results = Vec::with_capacity(message_ids.len());
        let mut response = Vec::with_capacity(MAX_LINE_BYTES);
        for _ in message_ids {
            let timeout = article_remaining(deadline)?;
            article_result(
                line_into_cancellable(&mut self.reader, &mut response, cancelled, timeout),
                deadline,
            )?;
            match code(&response)? {
                223 => results.push(true),
                430 => results.push(false),
                480 => return Err("nntp_auth_required"),
                500 | 501 => return Err("nntp_stat_unsupported"),
                412 => return Err("nntp_group_required"),
                status => {
                    return Err(authentication_status_failure(status).unwrap_or("nntp_stat_failed"));
                }
            }
        }
        Ok(results)
    }

    #[cfg(test)]
    fn pipeline(&mut self, tasks: &mut [PipelineTask], group_dispatch: &mut GroupDispatch) -> bool {
        matches!(
            self.pipeline_attempt(tasks, group_dispatch, None),
            PipelineOutcome::Healthy
        )
    }

    #[cfg(test)]
    fn pipeline_until(
        &mut self,
        tasks: &mut [PipelineTask],
        group_dispatch: &mut GroupDispatch,
        article_deadline: Instant,
    ) -> bool {
        matches!(
            self.pipeline_attempt(tasks, group_dispatch, Some(article_deadline)),
            PipelineOutcome::Healthy
        )
    }

    fn pipeline_attempt(
        &mut self,
        tasks: &mut [PipelineTask],
        group_dispatch: &mut GroupDispatch,
        total_deadline: Option<Instant>,
    ) -> PipelineOutcome {
        debug_assert!(tasks.iter().all(|task| {
            task._pool_reference.inner.lane == tasks[0]._pool_reference.inner.lane
                && Arc::ptr_eq(
                    &task._pool_reference.inner.group_routing,
                    &tasks[0]._pool_reference.inner.group_routing,
                )
        }));
        debug_assert!(
            group_dispatch.requirement != GroupRequirement::Unknown
                || tasks.len() == 1
                || tasks.iter().all(|task| task.group.is_none())
        );
        let compressed_budget = tasks
            .iter()
            .map(|task| {
                u64::try_from(task.maximum_wire_bytes).expect("bounded NNTP article")
                    + COMPRESSED_FRAMING_ALLOWANCE
            })
            .sum();
        self.reader
            .get_mut()
            .begin_compressed_read_budget(compressed_budget);
        if group_dispatch.requirement == GroupRequirement::Required {
            let Some(group) = tasks[0].group.as_deref() else {
                send_pipeline_failure(tasks, 0, "nntp_group_required");
                return PipelineOutcome::Healthy;
            };
            if let Err(code) = self.select_group(
                group,
                &|| tasks[0].cancellation.is_cancelled(),
                pipeline_deadline(total_deadline, TIMEOUT),
            ) {
                if reconnectable_connection_failure(code) {
                    return PipelineOutcome::ReconnectFrom(0, code);
                }
                send_pipeline_failure(tasks, 0, code);
                return if code == "nntp_group_failed" {
                    PipelineOutcome::Healthy
                } else {
                    PipelineOutcome::Unhealthy
                };
            }
        }
        let write_deadline = pipeline_deadline(total_deadline, TIMEOUT);
        let mut command_scratch = String::with_capacity(64);
        for index in 0..tasks.len() {
            if tasks.iter().any(|task| task.cancellation.is_cancelled()) {
                send_pipeline_failure(tasks, 0, "nntp_cancelled");
                return PipelineOutcome::Unhealthy;
            }
            command_scratch.clear();
            let message_id = &tasks[index].request.message_id;
            use std::fmt::Write;
            let _ = write!(command_scratch, "BODY <{message_id}>\r\n");
            let write = {
                let pipeline_cancelled =
                    || tasks.iter().any(|task| task.cancellation.is_cancelled());
                self.reader.get_mut().write_all_cancellable_until(
                    command_scratch.as_bytes(),
                    &pipeline_cancelled,
                    write_deadline,
                )
            };
            if let Err(code) = pipeline_result(write, total_deadline) {
                if reconnectable_connection_failure(code) {
                    return PipelineOutcome::ReconnectFrom(0, code);
                }
                send_pipeline_failure(tasks, 0, code);
                return PipelineOutcome::Unhealthy;
            }
        }
        let mut healthy = true;
        let mut response_scratch = Vec::with_capacity(MAX_LINE_BYTES);
        for index in 0..tasks.len() {
            let (result, learned) = {
                let task = &tasks[index];
                let task_cancelled = || task.cancellation.is_cancelled();
                let response = pipeline_remaining(total_deadline).and_then(|timeout| {
                    line_into_cancellable(
                        &mut self.reader,
                        &mut response_scratch,
                        &task_cancelled,
                        timeout,
                    )
                    .and_then(|()| code(&response_scratch))
                });
                let code = match pipeline_result(response, total_deadline) {
                    Ok(code) => code,
                    Err(code) => {
                        if reconnectable_connection_failure(code) {
                            return PipelineOutcome::ReconnectFrom(index, code);
                        }
                        send_pipeline_failure(tasks, index, code);
                        return PipelineOutcome::Unhealthy;
                    }
                };
                match code {
                    222 => {
                        let result = self.read_yenc_body(
                            task.maximum_wire_bytes,
                            task.maximum_decoded_bytes,
                            &|| task.cancellation.is_cancelled(),
                            total_deadline,
                        );
                        let learned = result.is_ok().then_some(GroupRequirement::NotRequired);
                        (result, learned)
                    }
                    412 if group_dispatch.requirement == GroupRequirement::Unknown => {
                        match self.retry_body_in_group(task, total_deadline) {
                            Ok(part) => (Ok(part), Some(GroupRequirement::Required)),
                            Err(code) if reconnectable_connection_failure(code) => {
                                return PipelineOutcome::ReconnectFrom(index, code);
                            }
                            Err(code) => (Err(code), None),
                        }
                    }
                    412 => (Err("nntp_group_required"), None),
                    430 => (Err("nntp_article_missing"), None),
                    480 => {
                        return PipelineOutcome::ReconnectFrom(index, "nntp_auth_required");
                    }
                    status => (
                        Err(authentication_status_failure(status).unwrap_or("nntp_body_failed")),
                        None,
                    ),
                }
            };
            if group_dispatch.probing {
                group_dispatch.finish(learned);
            }
            if let Some(code) = result.as_ref().err().copied()
                && reconnectable_connection_failure(code)
            {
                return PipelineOutcome::ReconnectFrom(index, code);
            }
            let synchronized = result
                .as_ref()
                .err()
                .is_none_or(|code| synchronized_connection_response(code));
            if result
                .as_ref()
                .err()
                .is_some_and(|code| !reusable_connection_response(code))
            {
                healthy = false;
            }
            tasks[index].complete(result);
            if !synchronized {
                send_pipeline_failure(tasks, index + 1, "nntp_pipeline_desynchronized");
                return PipelineOutcome::Unhealthy;
            }
        }
        if healthy {
            PipelineOutcome::Healthy
        } else {
            PipelineOutcome::Unhealthy
        }
    }
}

fn send_pipeline_failure(tasks: &mut [PipelineTask], start: usize, code: &'static str) {
    for task in &mut tasks[start..] {
        task.complete(Err(code));
    }
}

#[cfg(test)]
fn connect_addresses(addresses: &[SocketAddr]) -> Result<TcpStream, &'static str> {
    connect_addresses_cancellable(addresses, &|| false)
}

fn connect_addresses_cancellable<F>(
    addresses: &[SocketAddr],
    cancelled: &F,
) -> Result<TcpStream, &'static str>
where
    F: Fn() -> bool,
{
    let deadline = Instant::now() + TIMEOUT;
    for address in addresses {
        cancellation_checkpoint(cancelled)?;
        if Instant::now() >= deadline {
            break;
        }
        let domain = if address.is_ipv4() {
            libc::AF_INET
        } else {
            libc::AF_INET6
        };
        // SAFETY: socket returns a new descriptor or -1. Successful descriptors
        // are immediately placed under OwnedFd so every error path closes them.
        let descriptor = unsafe {
            libc::socket(
                domain,
                libc::SOCK_STREAM | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK,
                libc::IPPROTO_TCP,
            )
        };
        if descriptor < 0 {
            continue;
        }
        // SAFETY: descriptor is a unique successful socket result owned here.
        let descriptor = unsafe { OwnedFd::from_raw_fd(descriptor) };
        let connected = match address {
            SocketAddr::V4(address) => {
                let raw = libc::sockaddr_in {
                    sin_family: libc::AF_INET as libc::sa_family_t,
                    sin_port: address.port().to_be(),
                    sin_addr: libc::in_addr {
                        s_addr: u32::from_ne_bytes(address.ip().octets()),
                    },
                    sin_zero: [0; 8],
                };
                // SAFETY: raw is a fully initialized sockaddr_in of the supplied length.
                unsafe {
                    libc::connect(
                        descriptor.as_raw_fd(),
                        std::ptr::from_ref(&raw).cast(),
                        std::mem::size_of_val(&raw) as libc::socklen_t,
                    )
                }
            }
            SocketAddr::V6(address) => {
                let raw = libc::sockaddr_in6 {
                    sin6_family: libc::AF_INET6 as libc::sa_family_t,
                    sin6_port: address.port().to_be(),
                    sin6_flowinfo: address.flowinfo().to_be(),
                    sin6_addr: libc::in6_addr {
                        s6_addr: address.ip().octets(),
                    },
                    sin6_scope_id: address.scope_id(),
                };
                // SAFETY: raw is a fully initialized sockaddr_in6 of the supplied length.
                unsafe {
                    libc::connect(
                        descriptor.as_raw_fd(),
                        std::ptr::from_ref(&raw).cast(),
                        std::mem::size_of_val(&raw) as libc::socklen_t,
                    )
                }
            }
        };
        let connected = if connected == 0 {
            Ok(())
        } else {
            let error = std::io::Error::last_os_error();
            if matches!(
                error.raw_os_error(),
                Some(libc::EINPROGRESS | libc::EALREADY)
            ) {
                wait_for_connect_cancellable(descriptor.as_raw_fd(), cancelled, deadline)
            } else {
                Err("nntp_connect_failed")
            }
        };
        if let Err(code) = connected {
            if code == "nntp_cancelled" {
                return Err(code);
            }
            continue;
        }
        cancellation_checkpoint(cancelled)?;
        let stream = TcpStream::from(descriptor);
        if stream.set_nonblocking(false).is_err() {
            continue;
        }
        if stream.set_read_timeout(Some(TIMEOUT)).is_err()
            || stream.set_write_timeout(Some(TIMEOUT)).is_err()
        {
            continue;
        }
        return Ok(stream);
    }
    Err("nntp_connect_failed")
}

fn wait_for_connect_cancellable<F>(
    descriptor: libc::c_int,
    cancelled: &F,
    deadline: Instant,
) -> Result<(), &'static str>
where
    F: Fn() -> bool,
{
    wait_for_connect_with(cancelled, deadline, |timeout| {
        let mut event = libc::pollfd {
            fd: descriptor,
            events: libc::POLLOUT,
            revents: 0,
        };
        let milliseconds = timeout.as_millis().clamp(1, i32::MAX as u128) as i32;
        // SAFETY: event points to one initialized pollfd for the duration of poll.
        let ready = unsafe { libc::poll(std::ptr::addr_of_mut!(event), 1, milliseconds) };
        if ready == 0 {
            return Ok(None);
        }
        if ready < 0 {
            return if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                Ok(None)
            } else {
                Err(())
            };
        }
        let mut socket_error: libc::c_int = 0;
        let mut length = std::mem::size_of_val(&socket_error) as libc::socklen_t;
        // SAFETY: socket_error and length are valid writable getsockopt outputs.
        let result = unsafe {
            libc::getsockopt(
                descriptor,
                libc::SOL_SOCKET,
                libc::SO_ERROR,
                std::ptr::addr_of_mut!(socket_error).cast(),
                std::ptr::addr_of_mut!(length),
            )
        };
        if result == 0 {
            Ok(Some(socket_error))
        } else {
            Err(())
        }
    })
}

fn wait_for_connect_with<F, P>(
    cancelled: &F,
    deadline: Instant,
    mut poll: P,
) -> Result<(), &'static str>
where
    F: Fn() -> bool,
    P: FnMut(Duration) -> Result<Option<libc::c_int>, ()>,
{
    loop {
        cancellation_checkpoint(cancelled)?;
        let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
            return Err("nntp_connect_failed");
        };
        let timeout = remaining.min(CANCELLATION_POLL);
        let socket_error = poll(timeout).map_err(|_| "nntp_connect_failed")?;
        cancellation_checkpoint(cancelled)?;
        match socket_error {
            Some(0) => return Ok(()),
            Some(_) => return Err("nntp_connect_failed"),
            None => {}
        }
    }
}

fn cancellation_checkpoint<F>(cancelled: &F) -> Result<(), &'static str>
where
    F: Fn() -> bool,
{
    if cancelled() {
        Err("nntp_cancelled")
    } else {
        Ok(())
    }
}

fn is_public_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => {
            !address.is_private()
                && !address.is_loopback()
                && !address.is_link_local()
                && !address.is_broadcast()
                && !address.is_unspecified()
                && !address.is_multicast()
                && !matches!(
                    address.octets(),
                    [0, ..]
                        | [100, 64..=127, ..]
                        | [169, 254, ..]
                        | [192, 0, 0, ..]
                        | [192, 0, 2, ..]
                        | [198, 18..=19, ..]
                        | [198, 51, 100, ..]
                        | [203, 0, 113, ..]
                        | [240..=255, ..]
                )
        }
        IpAddr::V6(address) => {
            !address.is_loopback()
                && !address.is_unspecified()
                && !address.is_multicast()
                && !address.is_unicast_link_local()
                && (address.segments()[0] & 0xfe00 != 0xfc00)
        }
    }
}

fn native_root_store() -> Result<RootCertStore, &'static str> {
    let mut roots = RootCertStore::empty();
    let certificates = rustls_native_certs::load_native_certs();
    roots.add_parsable_certificates(certificates.certs);
    if roots.is_empty() {
        return Err("nntp_tls_roots_failed");
    }
    Ok(roots)
}

fn tls_client_config(roots: RootCertStore) -> ClientConfig {
    ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth()
}

fn tls_stream_cancellable<F>(
    stream: TcpStream,
    host: &str,
    cancelled: &F,
) -> Result<StreamOwned<ClientConnection, TcpStream>, &'static str>
where
    F: Fn() -> bool,
{
    let roots = native_root_store()?;
    let server_name = ServerName::try_from(host.to_owned()).map_err(|_| "nntp_tls_name_invalid")?;
    let configuration = tls_client_config(roots);
    let mut stream = StreamOwned::new(
        ClientConnection::new(Arc::new(configuration), server_name)
            .map_err(|_| "nntp_tls_failed")?,
        stream,
    );
    stream
        .sock
        .set_read_timeout(Some(CANCELLATION_POLL))
        .map_err(|_| "nntp_timeout_configuration_failed")?;
    stream
        .sock
        .set_write_timeout(Some(CANCELLATION_POLL))
        .map_err(|_| "nntp_timeout_configuration_failed")?;
    let deadline = Instant::now() + TIMEOUT;
    complete_tls_with(cancelled, deadline, || {
        match stream.conn.complete_io(&mut stream.sock) {
            Ok(_) => Ok(!stream.conn.is_handshaking()),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::Interrupted
                ) =>
            {
                Ok(false)
            }
            Err(error) => Err(error),
        }
    })?;
    stream
        .sock
        .set_read_timeout(Some(TIMEOUT))
        .map_err(|_| "nntp_timeout_configuration_failed")?;
    stream
        .sock
        .set_write_timeout(Some(TIMEOUT))
        .map_err(|_| "nntp_timeout_configuration_failed")?;
    Ok(stream)
}

fn complete_tls_with<F, H>(
    cancelled: &F,
    deadline: Instant,
    mut handshake: H,
) -> Result<(), &'static str>
where
    F: Fn() -> bool,
    H: FnMut() -> std::io::Result<bool>,
{
    loop {
        cancellation_checkpoint(cancelled)?;
        if Instant::now() >= deadline {
            return Err("nntp_tls_failed");
        }
        if handshake().map_err(|_| "nntp_tls_failed")? {
            cancellation_checkpoint(cancelled)?;
            return Ok(());
        }
    }
}

#[cfg(test)]
fn body(request: BodyRequest, maximum_bytes: usize) -> Result<Vec<u8>, &'static str> {
    body_with_cancellation(request, maximum_bytes, &|| false)
}

#[cfg(test)]
fn body_with_cancellation<F>(
    request: BodyRequest,
    maximum_bytes: usize,
    cancelled: &F,
) -> Result<Vec<u8>, &'static str>
where
    F: Fn() -> bool,
{
    validate_request(
        &request.host,
        &request.tls_mode,
        request.username.as_deref(),
        request.password.as_deref(),
        &request.message_id,
    )?;
    NntpConnection::open(&request, cancelled)?.body(&request.message_id, maximum_bytes, cancelled)
}

#[cfg(test)]
fn verified_part(
    request: BodyRequest,
    maximum_bytes: usize,
) -> Result<crate::yenc::DecodedPart, &'static str> {
    crate::yenc::decode(&body(request, maximum_bytes)?)
}

#[cfg(test)]
mod tests {
    #[test]
    fn tls_client_config_completes_a_real_handshake_and_round_trips_a_command() {
        use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName};
        use rustls::{RootCertStore, ServerConfig, ServerConnection, StreamOwned};
        use std::io::{Read as _, Write as _};
        use std::net::{TcpListener, TcpStream};

        let issued = rcgen::generate_simple_self_signed(vec!["localhost".to_owned()])
            .expect("generate test certificate");
        let certificate = CertificateDer::from(issued.cert.der().to_vec());
        let key =
            PrivateKeyDer::try_from(issued.signing_key.serialize_der()).expect("test private key");

        let mut roots = RootCertStore::empty();
        roots
            .add(certificate.clone())
            .expect("trust the generated certificate");

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind TLS test listener");
        let address = listener.local_addr().expect("TLS test address");
        let server = thread::spawn(move || {
            let configuration = ServerConfig::builder()
                .with_no_client_auth()
                .with_single_cert(vec![certificate], key)
                .expect("server TLS configuration");
            let (socket, _) = listener.accept().expect("accept TLS test client");
            let connection =
                ServerConnection::new(Arc::new(configuration)).expect("server connection");
            let mut stream = StreamOwned::new(connection, socket);
            stream
                .write_all(b"200 test server ready\r\n")
                .expect("write greeting");
            let mut request = [0u8; 32];
            let read = stream.read(&mut request).expect("read command");
            assert_eq!(&request[..read], b"DATE\r\n");
            stream
                .write_all(b"111 20260727123456\r\n")
                .expect("write reply");
        });

        let socket = TcpStream::connect(address).expect("connect TLS test server");
        let configuration = super::tls_client_config(roots);
        let name = ServerName::try_from("localhost").expect("server name");
        let connection =
            super::ClientConnection::new(Arc::new(configuration), name).expect("client connection");
        let mut stream = StreamOwned::new(connection, socket);

        let mut greeting = [0u8; 23];
        stream.read_exact(&mut greeting).expect("read greeting");
        assert_eq!(&greeting, b"200 test server ready\r\n");
        assert!(!stream.conn.is_handshaking(), "handshake did not complete");
        assert!(
            stream.conn.negotiated_cipher_suite().is_some(),
            "no cipher suite negotiated"
        );
        assert!(
            matches!(
                stream.conn.protocol_version(),
                Some(rustls::ProtocolVersion::TLSv1_3 | rustls::ProtocolVersion::TLSv1_2)
            ),
            "negotiated version is outside the TLS 1.2/1.3 contract"
        );

        stream.write_all(b"DATE\r\n").expect("write command");
        let mut reply = [0u8; 20];
        stream.read_exact(&mut reply).expect("read reply");
        assert_eq!(&reply, b"111 20260727123456\r\n");

        server.join().expect("join TLS test server");
    }

    use super::{
        ARTICLE_RESERVATION_QUANTUM, BodyRequest, CANCELLATION_POLL, FlightCancellation,
        GroupDispatch, GroupRequirement, MAX_DECLARED_ARTICLE_BYTES, MAX_PIPELINE_DEPTH,
        MAX_PIPELINE_QUEUE, NntpConnection, NntpStream, PipelineTask, PoolReference, PoolRegistry,
        PoolStats, ProviderPerformance, SchedulingContext, SecretText, TIMEOUT, WorkClass,
        article_read_limit, authentication_status_failure, body, body_with_cancellation,
        capabilities_cancellable_until, code, complete_tls_with, connect_addresses,
        line_cancellable, resolve_cancellable, rounded_article_cost, valid_group,
        validate_body_template, verified_part, wait_for_connect_with, wait_for_resolution,
    };
    use std::cell::Cell;
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpListener;
    use std::sync::atomic::Ordering;
    use std::sync::{Arc, Barrier, mpsc};
    use std::thread;
    use std::time::{Duration, Instant};

    #[test]
    fn code_parses_three_ascii_digits_and_rejects_other_shapes() {
        assert_eq!(code(b"200 ok\r\n"), Ok(200));
        assert_eq!(code(b"111 status\r\n"), Ok(111));
        assert_eq!(code(b"111"), Err("nntp_invalid_response"));
        assert_eq!(code(b"999 extra\r\n"), Err("nntp_invalid_response"));
        assert_eq!(code(b"4"), Err("nntp_invalid_response"));
        assert_eq!(code(b"12\r\n"), Err("nntp_invalid_response"));
        assert_eq!(code(b"abc\r\n"), Err("nntp_invalid_response"));
        assert_eq!(code(b"2x9\r\n"), Err("nntp_invalid_response"));
        assert_eq!(code(b"430\r\n"), Ok(430));
    }

    #[test]
    fn response_lines_stop_at_the_bound_while_the_peer_keeps_trickling() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind trickling line server");
        let address = listener
            .local_addr()
            .expect("get trickling line server address");
        let (stop_sender, stop_receiver) = mpsc::channel();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept trickling line client");
            loop {
                if stop_receiver.try_recv().is_ok() {
                    return;
                }
                if stream.write_all(&[b'x'; 1024]).is_err() {
                    return;
                }
                thread::sleep(Duration::from_millis(10));
            }
        });
        let stream = std::net::TcpStream::connect(address).expect("connect trickling line client");
        let (result_sender, result_receiver) = mpsc::channel();
        let client = thread::spawn(move || {
            let mut reader = BufReader::new(NntpStream::Plain(stream));
            reader
                .get_ref()
                .set_read_timeout(Duration::from_secs(1))
                .expect("bound trickling line reads");
            let result = line_cancellable(&mut reader, &|| false, Duration::from_secs(10));
            result_sender.send(result).expect("report line result");
        });
        let result = result_receiver.recv_timeout(Duration::from_secs(2));
        let _ = stop_sender.send(());
        server.join().expect("join trickling line server");
        client.join().expect("join trickling line client");
        assert_eq!(result, Ok(Err::<Vec<u8>, _>("nntp_invalid_response")));
    }

    #[test]
    fn capabilities_share_one_deadline_across_the_status_and_every_line() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind slow capabilities server");
        let address = listener
            .local_addr()
            .expect("get slow capabilities server address");
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept capabilities client");
            let mut reader = BufReader::new(stream);
            assert_eq!(next_command(&mut reader), "CAPABILITIES\r\n");
            if reader
                .get_mut()
                .write_all(b"101 capabilities follow\r\n")
                .is_err()
            {
                return;
            }
            for _ in 0..8 {
                thread::sleep(Duration::from_millis(60));
                if reader.get_mut().write_all(b"VERSION 2\r\n").is_err() {
                    return;
                }
            }
            let _ = reader.get_mut().write_all(b".\r\n");
        });
        let stream = std::net::TcpStream::connect(address).expect("connect capabilities client");
        let mut reader = BufReader::new(NntpStream::Plain(stream));
        reader
            .get_ref()
            .set_read_timeout(CANCELLATION_POLL)
            .expect("bound capabilities reads");

        assert_eq!(
            capabilities_cancellable_until(
                &mut reader,
                &|| false,
                Instant::now() + Duration::from_millis(175),
            )
            .err(),
            Some("nntp_read_failed")
        );
        drop(reader);
        server.join().expect("join slow capabilities server");
    }

    #[test]
    fn authentication_statuses_use_only_protocol_codes() {
        assert_eq!(
            authentication_status_failure(480),
            Some("nntp_auth_required")
        );
        assert_eq!(authentication_status_failure(481), Some("nntp_auth_failed"));
        assert_eq!(authentication_status_failure(482), None);
    }

    #[test]
    fn adaptive_hedge_delay_uses_bounded_latency_throughput_and_miss_ewmas() {
        let performance = ProviderPerformance::default();
        assert_eq!(
            performance.hedge_delay(1024 * 1024),
            Duration::from_millis(500)
        );
        performance.observe(
            Duration::from_millis(50),
            &Ok(crate::yenc::DecodedPart {
                bytes: vec![0; 1024 * 1024],
                begin: 1,
                end: 1024 * 1024,
                total_size: 1024 * 1024,
                expected_crc32: Some(0),
                expected_whole_crc32: None,
            }),
        );
        let learned = performance.hedge_delay(1024 * 1024);
        assert!(learned >= Duration::from_millis(190));
        assert!(learned <= Duration::from_millis(210));

        performance.observe(Duration::from_millis(50), &Err("nntp_article_missing"));
        assert!(performance.hedge_delay(1024 * 1024) < learned);
        performance.observe(Duration::from_millis(50), &Err("nntp_auth_failed"));
        let diagnostics = performance.diagnostics();
        assert_eq!(diagnostics.suppliers, 1);
        assert_eq!(diagnostics.missing, 1);
        assert_eq!(diagnostics.failures, 1);
        assert_eq!(diagnostics.authentication_failures, 1);
    }

    fn scripted_server(
        script: impl FnOnce(&mut BufReader<std::net::TcpStream>) + Send + 'static,
    ) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind scripted NNTP server");
        let port = listener
            .local_addr()
            .expect("get scripted NNTP address")
            .port();
        thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept NNTP client");
            let mut reader = BufReader::new(stream.try_clone().expect("clone NNTP client"));
            stream
                .try_clone()
                .expect("clone NNTP writer")
                .write_all(b"200 scripted server ready\r\n")
                .expect("send NNTP greeting");
            script(&mut reader);
        });
        port
    }

    fn next_command(reader: &mut BufReader<std::net::TcpStream>) -> String {
        let mut line = String::new();
        reader.read_line(&mut line).expect("read NNTP command");
        line
    }

    fn next_compressed_command(reader: &mut BufReader<NntpStream>) -> String {
        let mut line = String::new();
        reader
            .read_line(&mut line)
            .expect("read compressed NNTP command");
        line
    }

    fn respond(reader: &mut BufReader<std::net::TcpStream>, response: &[u8]) {
        reader
            .get_mut()
            .write_all(response)
            .expect("send scripted NNTP response");
    }

    fn capabilities(reader: &mut BufReader<std::net::TcpStream>) {
        assert_eq!(next_command(reader), "CAPABILITIES\r\n");
        respond(reader, b"101 capabilities follow\r\nVERSION 2\r\n.\r\n");
    }

    fn enter_reader_mode(reader: &mut BufReader<std::net::TcpStream>) {
        capabilities(reader);
        assert_eq!(next_command(reader), "MODE READER\r\n");
        respond(reader, b"200 reader enabled\r\n");
        capabilities(reader);
    }

    fn enter_authenticated_reader_mode(reader: &mut BufReader<std::net::TcpStream>) {
        capabilities(reader);
        assert_eq!(next_command(reader), "MODE READER\r\n");
        respond(reader, b"200 reader enabled\r\n");
        assert_eq!(next_command(reader), "AUTHINFO USER user\r\n");
        respond(reader, b"381 password required\r\n");
        assert_eq!(next_command(reader), "AUTHINFO PASS secret\r\n");
        respond(reader, b"281 authentication accepted\r\n");
        capabilities(reader);
    }

    #[test]
    fn ignores_unconsumed_capability_metadata_after_authentication() {
        let port = scripted_server(|reader| {
            assert_eq!(next_command(reader), "CAPABILITIES\r\n");
            respond(
                reader,
                b"101 Capabilities list:\r\nVERSION 1\r\nAUTHINFO USER PASS\r\n.\r\n",
            );
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"201 reader enabled\r\n");
            assert_eq!(next_command(reader), "AUTHINFO USER user\r\n");
            respond(reader, b"381 password required\r\n");
            assert_eq!(next_command(reader), "AUTHINFO PASS secret\r\n");
            respond(reader, b"281 authentication accepted\r\n");
            assert_eq!(next_command(reader), "CAPABILITIES\r\n");
            respond(
                reader,
                b"101 Capabilities list:\r\nVERSION 1\r\nMODE-READER\r\nREADER\r\n\
                  PIPELINING\r\nLIST COUNTS OVERVIEW.FMT ACTIVE ACTIVE.TIMES NEWSGROUPS\r\n\
                  XHDR\r\nXOVER\r\nXZVER\r\nXZHDR\r\n\
                  XFEATURE-COMPRESS GZIP TERMINATOR\r\n\
                  X-FUTURE opaque/value=42 \xff\r\n.\r\n",
            );
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: Some("user".into()),
            password: Some("secret".into()),
            message_id: "article@example.test".into(),
        };

        NntpConnection::open(&request, &|| false).expect("ignore future capability metadata");
    }

    fn pool_request(port: u16, message_id: &str) -> BodyRequest {
        BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: message_id.into(),
        }
    }

    #[test]
    fn request_clones_share_zeroizing_credential_allocations() {
        let mut request = pool_request(563, "credentials@example.test");
        request.username = Some(SecretText::from("user"));
        request.password = Some(SecretText::from("secret"));
        let cloned = request.clone();

        assert!(Arc::ptr_eq(
            &request.username.as_ref().expect("request username").0,
            &cloned.username.as_ref().expect("cloned username").0,
        ));
        assert!(Arc::ptr_eq(
            &request.password.as_ref().expect("request password").0,
            &cloned.password.as_ref().expect("cloned password").0,
        ));
    }

    fn pipeline_task(
        request: BodyRequest,
        cancellation: FlightCancellation,
        pool_reference: PoolReference,
    ) -> (
        PipelineTask,
        mpsc::Receiver<Result<crate::yenc::DecodedPart, &'static str>>,
    ) {
        pool_reference.inner.group_routing.assume_not_required();
        let (result, receiver) = mpsc::sync_channel(1);
        (
            PipelineTask {
                scheduling: SchedulingContext {
                    work_class: WorkClass::Interactive,
                    configuration_partition: [0; 32],
                    session: "test-session".into(),
                    declared_encoded_bytes: 1024,
                },
                request,
                group: None,
                maximum_wire_bytes: 1024,
                maximum_decoded_bytes: 1024,
                cancellation,
                _reservation: None,
                _pool_reference: pool_reference,
                result,
            },
            receiver,
        )
    }

    fn wait_for_pool_stats(
        pools: &PoolRegistry,
        description: &str,
        predicate: impl Fn(&PoolStats) -> bool,
    ) -> PoolStats {
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            let stats = pools.stats();
            if predicate(&stats) {
                return stats;
            }
            assert!(
                Instant::now() < deadline,
                "timed out waiting for {description}"
            );
            thread::yield_now();
        }
    }

    fn one_article_pool_server() -> (u16, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind one-article NNTP server");
        let port = listener
            .local_addr()
            .expect("get one-article NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept one-article NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 one-article server ready\r\n");
            enter_reader_mode(&mut reader);
            let _ = next_command(&mut reader);
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
        });
        (port, server)
    }

    #[test]
    fn physical_pool_reuses_one_authenticated_connection_for_matching_accounts() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind pooled NNTP server");
        let port = listener
            .local_addr()
            .expect("get pooled NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept one pooled NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 pooled server ready\r\n");
            enter_reader_mode(&mut reader);
            for message_id in ["one@example.test", "two@example.test"] {
                assert_eq!(
                    next_command(&mut reader),
                    format!("BODY <{message_id}>\r\n")
                );
                respond(
                    &mut reader,
                    b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
                );
            }
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create physical pool registry"));
        let first_request = pool_request(port, "one@example.test");
        let broad_reference = pools
            .reference(&first_request, 4, 2)
            .expect("reference broad physical pool");

        assert_eq!(
            pools
                .verified_part(
                    broad_reference.clone(),
                    first_request,
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("fetch first pooled part")
                .bytes,
            b"A"
        );
        wait_for_pool_stats(&pools, "first pooled connection to become idle", |stats| {
            stats.idle == 1
        });
        let second_request = pool_request(port, "two@example.test");
        let matching_reference = pools
            .reference(&second_request, 4, 2)
            .expect("reference matching physical pool");
        assert_eq!(
            pools
                .verified_part(
                    matching_reference.clone(),
                    second_request,
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("fetch second pooled part")
                .bytes,
            b"A"
        );
        server.join().expect("join pooled NNTP server");
    }

    #[test]
    fn stale_idle_connection_retries_once_on_a_fresh_socket() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind stale-idle NNTP server");
        let port = listener
            .local_addr()
            .expect("get stale-idle address")
            .port();
        let server = thread::spawn(move || {
            {
                let (stream, _) = listener.accept().expect("accept stale-idle NNTP client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 stale-idle server ready\r\n");
                enter_reader_mode(&mut reader);
                assert_eq!(
                    next_command(&mut reader),
                    "BODY <first-stale@example.test>\r\n"
                );
                respond(
                    &mut reader,
                    b"222 first body follows\r\n=ybegin line=128 size=1 name=first\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
                );
            }
            {
                let (stream, _) = listener
                    .accept()
                    .expect("accept stale-idle replacement client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 stale-idle replacement ready\r\n");
                enter_reader_mode(&mut reader);
                assert_eq!(
                    next_command(&mut reader),
                    "BODY <second-stale@example.test>\r\n"
                );
                respond(
                    &mut reader,
                    b"222 second body follows\r\n=ybegin line=128 size=1 name=second\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n",
                );
            }
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create stale-idle pool registry"));
        let first_request = pool_request(port, "first-stale@example.test");
        let reference = pools
            .reference(&first_request, 1, 1)
            .expect("reference stale-idle pool");

        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    first_request,
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("fetch before stale idle close")
                .bytes,
            b"A"
        );
        wait_for_pool_stats(&pools, "stale connection to become idle", |stats| {
            stats.idle == 1
        });
        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    pool_request(port, "second-stale@example.test"),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("retry stale idle connection")
                .bytes,
            b"B"
        );
        server.join().expect("join stale-idle NNTP server");
        let stats = wait_for_pool_stats(&pools, "replacement connection to become idle", |stats| {
            stats.open == 1 && stats.idle == 1
        });
        assert_eq!(stats.poisoned, 1);
        assert_eq!(stats.provider_attempts, 2);
        assert_eq!(stats.provider_hits, 2);
    }

    #[test]
    fn expired_pooled_authentication_reconnects_once_within_the_article_deadline() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind reauthentication NNTP server");
        let port = listener
            .local_addr()
            .expect("get reauthentication NNTP address")
            .port();
        let server = thread::spawn(move || {
            for expired in [true, false] {
                let (stream, _) = listener
                    .accept()
                    .expect("accept reauthentication NNTP client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 reauthentication server ready\r\n");
                enter_authenticated_reader_mode(&mut reader);
                assert_eq!(
                    next_command(&mut reader),
                    "BODY <reauthenticate@example.test>\r\n"
                );
                if expired {
                    respond(&mut reader, b"480 authentication required again\r\n");
                } else {
                    respond(
                        &mut reader,
                        b"222 body follows\r\n=ybegin line=128 size=1 name=valid\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
                    );
                }
            }
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create reauthentication pool registry"));
        let mut request = pool_request(port, "reauthenticate@example.test");
        request.username = Some(SecretText::from("user"));
        request.password = Some(SecretText::from("secret"));
        let reference = pools
            .reference(&request, 1, 1)
            .expect("reference reauthentication pool");

        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    request,
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("retry article after session authentication expires")
                .bytes,
            b"A"
        );
        server.join().expect("join reauthentication NNTP server");
        let stats = pools.stats();
        assert_eq!(stats.provider_attempts, 1);
        assert_eq!(stats.provider_hits, 1);
        assert_eq!(stats.poisoned, 1);
    }

    #[test]
    fn repeated_authentication_expiry_stops_after_one_reconnect() {
        let listener =
            TcpListener::bind("127.0.0.1:0").expect("bind bounded-reauthentication NNTP server");
        let port = listener
            .local_addr()
            .expect("get bounded-reauthentication NNTP address")
            .port();
        let server = thread::spawn(move || {
            for _ in 0..2 {
                let (stream, _) = listener
                    .accept()
                    .expect("accept bounded-reauthentication NNTP client");
                let mut reader = BufReader::new(stream);
                respond(
                    &mut reader,
                    b"200 bounded-reauthentication server ready\r\n",
                );
                enter_authenticated_reader_mode(&mut reader);
                assert_eq!(
                    next_command(&mut reader),
                    "BODY <bounded-reauthenticate@example.test>\r\n"
                );
                respond(&mut reader, b"480 authentication required again\r\n");
            }
        });
        let pools =
            Arc::new(PoolRegistry::new(1).expect("create bounded-reauthentication pool registry"));
        let mut request = pool_request(port, "bounded-reauthenticate@example.test");
        request.username = Some(SecretText::from("user"));
        request.password = Some(SecretText::from("secret"));
        let reference = pools
            .reference(&request, 1, 1)
            .expect("reference bounded-reauthentication pool");

        assert_eq!(
            pools.verified_part(
                reference.clone(),
                request,
                None,
                1024,
                FlightCancellation::new(),
            ),
            Err("nntp_auth_required")
        );
        server
            .join()
            .expect("join bounded-reauthentication NNTP server");
        let stats = pools.stats();
        assert_eq!(stats.provider_attempts, 1);
        assert_eq!(stats.poisoned, 2);
    }

    #[test]
    fn early_yenc_failure_drains_and_reuses_the_synchronized_connection() {
        let listener =
            TcpListener::bind("127.0.0.1:0").expect("bind reusable-integrity NNTP server");
        let port = listener
            .local_addr()
            .expect("get reusable-integrity NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept one reusable-integrity NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 reusable-integrity server ready\r\n");
            enter_reader_mode(&mut reader);

            assert_eq!(next_command(&mut reader), "BODY <corrupt@example.test>\r\n");
            respond(
                &mut reader,
                b"222 corrupt body follows\r\nnot a yEnc header\r\nignored payload\r\n=yend size=1 crc32=00000000\r\n.\r\n",
            );

            assert_eq!(next_command(&mut reader), "BODY <valid@example.test>\r\n");
            respond(
                &mut reader,
                b"222 valid body follows\r\n=ybegin line=128 size=1 name=valid\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
        });
        let pools =
            Arc::new(PoolRegistry::new(1).expect("create reusable-integrity pool registry"));
        let corrupt_request = pool_request(port, "corrupt@example.test");
        let reference = pools
            .reference(&corrupt_request, 1, 1)
            .expect("reference reusable-integrity pool");

        assert_eq!(
            pools.verified_part(
                reference.clone(),
                corrupt_request,
                None,
                1024,
                FlightCancellation::new(),
            ),
            Err("missing_ybegin")
        );
        wait_for_pool_stats(
            &pools,
            "integrity response connection to become idle",
            |stats| stats.idle == 1,
        );
        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    pool_request(port, "valid@example.test"),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("reuse connection after integrity response")
                .bytes,
            b"A"
        );

        server.join().expect("join reusable-integrity NNTP server");
        wait_for_pool_stats(
            &pools,
            "valid response connection to become idle",
            |stats| stats.idle == 1,
        );
        assert_eq!(pools.stats().poisoned, 0);
    }

    #[test]
    fn learns_group_requirement_only_after_verified_retry_and_reuses_selection() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind group-learning NNTP server");
        let port = listener
            .local_addr()
            .expect("get group-learning NNTP address")
            .port();
        let server = thread::spawn(move || {
            {
                let (stream, _) = listener
                    .accept()
                    .expect("accept first-generation group-learning client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 group-learning server ready\r\n");
                enter_reader_mode(&mut reader);

                assert_eq!(
                    next_command(&mut reader),
                    "BODY <first-group@example.test>\r\n"
                );
                respond(&mut reader, b"412 no newsgroup selected\r\n");
                assert_eq!(next_command(&mut reader), "GROUP alt.video\r\n");
                respond(&mut reader, b"211 2 1 2 alt.video\r\n");
                assert_eq!(
                    next_command(&mut reader),
                    "BODY <first-group@example.test>\r\n"
                );
                respond(
                    &mut reader,
                    b"222 first body follows\r\n=ybegin line=128 size=1 name=first\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
                );

                assert_eq!(
                    next_command(&mut reader),
                    "BODY <second-group@example.test>\r\n"
                );
                respond(
                    &mut reader,
                    b"222 second body follows\r\n=ybegin line=128 size=1 name=second\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n",
                );
            }
            {
                let (stream, _) = listener
                    .accept()
                    .expect("accept second-generation group-learning client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 independently scoped server ready\r\n");
                enter_reader_mode(&mut reader);
                assert_eq!(next_command(&mut reader), "BODY <scope@example.test>\r\n");
                respond(&mut reader, b"412 no newsgroup selected\r\n");
                assert_eq!(next_command(&mut reader), "GROUP alt.other\r\n");
                respond(&mut reader, b"211 1 1 1 alt.other\r\n");
                assert_eq!(next_command(&mut reader), "BODY <scope@example.test>\r\n");
                respond(
                    &mut reader,
                    b"222 scoped body follows\r\n=ybegin line=128 size=1 name=scoped\r\nm\r\n=yend size=1 crc32=3dd7ffa7\r\n.\r\n",
                );
            }
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create group-learning pool registry"));
        let first_request = pool_request(port, "first-group@example.test");
        let reference = pools
            .reference_for_generation(&first_request, 1, 2, &"a".repeat(64), 0, false)
            .expect("reference group-learning pool");

        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    first_request,
                    Some("alt.video".into()),
                    1024,
                    FlightCancellation::new(),
                )
                .expect("verify group-learning retry")
                .bytes,
            b"A"
        );
        assert_eq!(
            reference.inner.group_routing.snapshot(),
            GroupRequirement::Required
        );
        let other_generation = pools
            .reference_for_generation(
                &pool_request(port, "scope@example.test"),
                1,
                2,
                &"b".repeat(64),
                0,
                false,
            )
            .expect("reference independently scoped group learning");
        assert!(Arc::ptr_eq(
            &reference.inner.pool,
            &other_generation.inner.pool
        ));
        assert_eq!(
            other_generation.inner.group_routing.snapshot(),
            GroupRequirement::Unknown
        );
        assert_eq!(
            pools
                .verified_part(
                    reference,
                    pool_request(port, "second-group@example.test"),
                    Some("alt.video".into()),
                    1024,
                    FlightCancellation::new(),
                )
                .expect("reuse learned group selection")
                .bytes,
            b"B"
        );
        assert_eq!(
            pools
                .verified_part(
                    other_generation,
                    pool_request(port, "scope@example.test"),
                    Some("alt.other".into()),
                    1024,
                    FlightCancellation::new(),
                )
                .expect("learn independently scoped group requirement")
                .bytes,
            b"C"
        );
        server.join().expect("join group-learning NNTP server");
    }

    #[test]
    fn failed_group_retry_does_not_learn_a_requirement() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind failed-group NNTP server");
        let port = listener
            .local_addr()
            .expect("get failed-group NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept failed-group NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 failed-group server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(
                next_command(&mut reader),
                "BODY <missing-group@example.test>\r\n"
            );
            respond(&mut reader, b"412 no newsgroup selected\r\n");
            assert_eq!(next_command(&mut reader), "GROUP alt.video\r\n");
            respond(&mut reader, b"211 0 0 0 alt.video\r\n");
            assert_eq!(
                next_command(&mut reader),
                "BODY <missing-group@example.test>\r\n"
            );
            respond(&mut reader, b"430 no article with that message-id\r\n");
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create failed-group pool registry"));
        let request = pool_request(port, "missing-group@example.test");
        let reference = pools
            .reference_for_generation(&request, 1, 2, &"a".repeat(64), 0, false)
            .expect("reference failed-group pool");

        assert_eq!(
            pools.verified_part(
                reference.clone(),
                request,
                Some("alt.video".into()),
                1024,
                FlightCancellation::new(),
            ),
            Err("nntp_article_missing")
        );
        assert_eq!(
            reference.inner.group_routing.snapshot(),
            GroupRequirement::Unknown
        );
        server.join().expect("join failed-group NNTP server");
    }

    #[test]
    fn no_such_group_keeps_the_synchronized_connection_reusable() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind reusable-group NNTP server");
        let port = listener
            .local_addr()
            .expect("get reusable-group NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept reusable-group client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 reusable-group server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "GROUP alt.missing\r\n");
            respond(&mut reader, b"411 no such newsgroup\r\n");
            assert_eq!(next_command(&mut reader), "GROUP alt.present\r\n");
            respond(&mut reader, b"211 1 1 1 alt.present\r\n");
            assert_eq!(
                next_command(&mut reader),
                "BODY <present-group@example.test>\r\n"
            );
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create reusable-group pool"));
        let missing_request = pool_request(port, "missing-group@example.test");
        let reference = pools
            .reference(&missing_request, 1, 1)
            .expect("reference reusable-group pool");
        reference.inner.group_routing.assume_required();

        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    missing_request,
                    Some("alt.missing".into()),
                    1024,
                    FlightCancellation::new(),
                )
                .expect_err("report missing group"),
            "nntp_group_failed"
        );
        wait_for_pool_stats(&pools, "missing-group connection to become idle", |stats| {
            stats.open == 1 && stats.idle == 1 && stats.poisoned == 0
        });
        assert_eq!(
            pools
                .verified_part(
                    reference,
                    pool_request(port, "present-group@example.test"),
                    Some("alt.present".into()),
                    1024,
                    FlightCancellation::new(),
                )
                .expect("reuse connection after missing group")
                .bytes,
            b"A"
        );
        server.join().expect("join reusable-group NNTP server");
    }

    #[test]
    fn provider_test_rejects_a_non_date_response_and_poisons_the_lease() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind rejected-DATE NNTP server");
        let port = listener
            .local_addr()
            .expect("get rejected-DATE NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept rejected-DATE NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 rejected-DATE server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "DATE\r\n");
            respond(&mut reader, b"500 DATE unavailable\r\n");
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create rejected-DATE pool registry"));
        let request = pool_request(port, "test-date@example.test");
        let reference = pools
            .reference_for_generation(&request, 1, 1, &"a".repeat(64), 0, false)
            .expect("reference rejected-DATE pool");
        let error = pools
            .test_reference(&reference, &request)
            .expect_err("reject non-DATE response");
        assert_eq!(error.phase, "date");
        assert_eq!(error.code, "nntp_date_failed");
        let stats = pools.stats();
        assert_eq!(stats.open, 0);
        assert_eq!(stats.poisoned, 1);
        server.join().expect("join rejected-DATE NNTP server");
    }

    #[test]
    fn timeout_defaults_match_the_native_protocol_contract() {
        assert_eq!(TIMEOUT, Duration::from_secs(15));
        assert_eq!(super::ARTICLE_TOTAL_TIMEOUT, Duration::from_secs(30));
        assert_eq!(super::BODY_STALL_TIMEOUT, Duration::from_secs(30));
    }

    #[test]
    fn provider_requests_retry_immediately_after_authentication_recovers() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind recovering NNTP server");
        let port = listener
            .local_addr()
            .expect("get recovering NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (first, _) = listener.accept().expect("accept rejected NNTP client");
            let mut first = BufReader::new(first);
            respond(&mut first, b"200 recovering server ready\r\n");
            capabilities(&mut first);
            assert_eq!(next_command(&mut first), "MODE READER\r\n");
            respond(&mut first, b"480 authentication required\r\n");
            assert_eq!(next_command(&mut first), "AUTHINFO USER user\r\n");
            respond(&mut first, b"502 authentication rejected\r\n");

            let (second, _) = listener.accept().expect("accept recovered NNTP client");
            let mut second = BufReader::new(second);
            respond(&mut second, b"200 recovered server ready\r\n");
            capabilities(&mut second);
            assert_eq!(next_command(&mut second), "MODE READER\r\n");
            respond(&mut second, b"200 reader enabled\r\n");
            assert_eq!(next_command(&mut second), "AUTHINFO USER user\r\n");
            respond(&mut second, b"381 password required\r\n");
            assert_eq!(next_command(&mut second), "AUTHINFO PASS secret\r\n");
            respond(&mut second, b"281 authentication accepted\r\n");
            capabilities(&mut second);
            assert_eq!(
                next_command(&mut second),
                "BODY <recovered@example.test>\r\n"
            );
            respond(
                &mut second,
                b"222 body follows\r\n=ybegin line=128 size=1 name=auth\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create recovering pool registry"));
        let mut request = pool_request(port, "recovered@example.test");
        request.username = Some("user".into());
        request.password = Some("secret".into());
        let reference = pools
            .reference(&request, 1, 1)
            .expect("reference recovering pool");

        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    request.clone(),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect_err("reject authentication"),
            "nntp_auth_failed"
        );
        let failed_stats = pools.stats();
        assert_eq!(failed_stats.provider_attempts, 1);
        assert_eq!(failed_stats.provider_suppliers, 0);
        assert_eq!(failed_stats.provider_failures, 1);
        assert_eq!(
            pools
                .verified_part(
                    reference.clone(),
                    request.clone(),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("retry after authentication recovery")
                .bytes,
            b"A"
        );
        let stats = pools.stats();
        assert_eq!(stats.provider_attempts, 2);
        assert_eq!(stats.provider_suppliers, 1);
        assert_eq!(stats.provider_hits, 1);
        assert_eq!(stats.provider_failures, 1);
        server.join().expect("join recovering NNTP server");
    }

    #[test]
    fn physical_pool_recomputes_limits_from_live_generation_references() {
        let pools = PoolRegistry::new(16).expect("create referenced pool registry");
        let request = pool_request(119, "references@example.test");
        let broad = pools
            .reference(&request, 8, 4)
            .expect("create broad pool reference");
        let pool = Arc::clone(&broad.inner.pool);
        {
            let state = pool.state.lock().expect("inspect broad pool limits");
            assert_eq!(state.references.len(), 1);
            assert_eq!(state.connection_limit, 8);
            assert_eq!(state.pipeline_depth, 4);
        }
        let strict = pools
            .reference(&request, 2, 1)
            .expect("create strict pool reference");
        {
            let state = pool.state.lock().expect("inspect strict pool limits");
            assert_eq!(state.references.len(), 2);
            assert_eq!(state.connection_limit, 2);
            assert_eq!(state.pipeline_depth, 1);
        }
        drop(strict);
        {
            let state = pool.state.lock().expect("inspect restored pool limits");
            assert_eq!(state.references.len(), 1);
            assert_eq!(state.connection_limit, 8);
            assert_eq!(state.pipeline_depth, 4);
        }
        drop(broad);
        let state = pool.state.lock().expect("inspect unreferenced pool");
        assert!(state.references.is_empty());
        assert_eq!(state.connection_limit, 0);
        assert_eq!(state.pipeline_depth, 1);
    }

    #[test]
    fn pipeline_depth_changes_do_not_retire_active_connections() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind pipeline-depth NNTP server");
        let port = listener
            .local_addr()
            .expect("get pipeline-depth NNTP address")
            .port();
        let (body_seen, receive_body_seen) = mpsc::sync_channel(0);
        let (release_body, receive_release_body) = mpsc::sync_channel(0);
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept pipeline-depth NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 pipeline-depth server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "BODY <epoch@example.test>\r\n");
            body_seen.send(()).expect("report pipeline-depth BODY");
            receive_release_body
                .recv()
                .expect("release pipeline-depth body");
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=epoch\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create epoch-drain pool registry"));
        let request = pool_request(port, "epoch@example.test");
        let broad = pools
            .reference(&request, 1, 4)
            .expect("create broad epoch reference");
        let caller_pools = Arc::clone(&pools);
        let caller_reference = broad.clone();
        let caller_request = request.clone();
        let caller = thread::spawn(move || {
            caller_pools.verified_part(
                caller_reference,
                caller_request,
                None,
                1024,
                FlightCancellation::new(),
            )
        });
        receive_body_seen
            .recv()
            .expect("observe active pipeline connection");
        let strict = pools
            .reference(&request, 1, 1)
            .expect("change active pool pipeline limit");
        release_body.send(()).expect("release pipeline body");

        assert_eq!(
            caller
                .join()
                .expect("join pipeline-depth caller")
                .unwrap()
                .bytes,
            b"A"
        );
        let stats = wait_for_pool_stats(&pools, "active connection to become idle", |stats| {
            stats.open == 1 && stats.active == 0 && stats.idle == 1
        });
        assert_eq!(stats.open, 1);
        assert_eq!(stats.active, 0);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.poisoned, 0);
        drop(strict);
        drop(broad);
        server.join().expect("join pipeline-depth NNTP server");
    }

    #[test]
    fn configured_limit_increases_reuse_idle_connections() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind pipeline-depth NNTP server");
        let port = listener
            .local_addr()
            .expect("get pipeline-depth NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept pipeline-depth client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 pipeline-depth server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(
                next_command(&mut reader),
                "BODY <idle-epoch@example.test>\r\n"
            );
            respond(
                &mut reader,
                b"222 first body follows\r\n=ybegin line=128 size=1 name=first\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
            assert_eq!(
                next_command(&mut reader),
                "BODY <retained-epoch@example.test>\r\n"
            );
            respond(
                &mut reader,
                b"222 second body follows\r\n=ybegin line=128 size=1 name=second\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create idle epoch pool registry"));
        let request = pool_request(port, "idle-epoch@example.test");
        let strict = pools
            .reference(&request, 1, 1)
            .expect("create strict idle epoch reference");
        pools
            .verified_part(
                strict.clone(),
                request.clone(),
                None,
                1024,
                FlightCancellation::new(),
            )
            .expect("populate retained idle connection");
        wait_for_pool_stats(&pools, "initial connection to become idle", |stats| {
            stats.idle == 1
        });

        let broad = pools
            .reference(&request, 4, 4)
            .expect("create broad idle epoch reference");
        drop(strict);

        assert_eq!(
            pools
                .verified_part(
                    broad.clone(),
                    pool_request(port, "retained-epoch@example.test"),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("reuse idle connection after configured limit increase")
                .bytes,
            b"B"
        );
        let stats = wait_for_pool_stats(&pools, "retained connection to become idle", |stats| {
            stats.open == 1 && stats.active == 0 && stats.idle == 1
        });
        assert_eq!(stats.poisoned, 0);
        drop(broad);
        server.join().expect("join retained-idle NNTP server");
    }

    #[test]
    fn configured_limit_reductions_drain_active_connections_after_completion() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind reduced-limit NNTP server");
        let port = listener
            .local_addr()
            .expect("get reduced-limit NNTP address")
            .port();
        let server = thread::spawn(move || {
            let mut readers = Vec::new();
            for _ in 0..2 {
                let (stream, _) = listener.accept().expect("accept reduced-limit client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 reduced-limit server ready\r\n");
                enter_reader_mode(&mut reader);
                readers.push(reader);
            }
            for mut reader in readers {
                let mut after_retirement = String::new();
                assert_eq!(
                    reader
                        .read_line(&mut after_retirement)
                        .expect("observe reduced-limit retirement"),
                    0
                );
            }
        });
        let pools = Arc::new(PoolRegistry::new(2).expect("create reduced-limit pool registry"));
        let request = pool_request(port, "active-limit@example.test");
        let broad = pools
            .reference(&request, 2, 1)
            .expect("create broad active reference");
        let pool = Arc::clone(&broad.inner.pool);
        let first = pool
            .checkout(&request, &|| false, &|| false)
            .expect("checkout first active connection");
        let second = pool
            .checkout(&request, &|| false, &|| false)
            .expect("checkout second active connection");

        let strict = pools
            .reference(&request, 1, 1)
            .expect("reduce active connection limit");
        pool.finish(first, true);
        pool.finish(second, true);
        let stats = wait_for_pool_stats(&pools, "reduced active connections to drain", |stats| {
            stats.open == 0 && stats.active == 0 && stats.idle == 0
        });
        assert_eq!(stats.poisoned, 0);
        drop(strict);
        drop(broad);
        server.join().expect("join reduced-limit NNTP server");
    }

    #[test]
    fn final_generation_reference_drains_idle_authenticated_sockets() {
        let (port, server) = one_article_pool_server();
        let pools = Arc::new(PoolRegistry::new(1).expect("create draining pool registry"));
        let request = pool_request(port, "drain@example.test");
        let reference = pools
            .reference(&request, 1, 1)
            .expect("create draining pool reference");

        pools
            .verified_part(
                reference.clone(),
                request,
                None,
                1024,
                FlightCancellation::new(),
            )
            .expect("fetch before final reference drain");
        let active =
            wait_for_pool_stats(&pools, "authenticated connection to become idle", |stats| {
                stats.open == 1 && stats.idle == 1
            });
        assert_eq!(active.open, 1);
        assert_eq!(active.idle, 1);

        drop(reference);

        let drained =
            wait_for_pool_stats(&pools, "final generation connection to drain", |stats| {
                stats.open == 0 && stats.active == 0 && stats.idle == 0
            });
        assert_eq!(drained.open, 0);
        assert_eq!(drained.active, 0);
        assert_eq!(drained.idle, 0);
        server.join().expect("join final-reference NNTP server");
    }

    #[test]
    fn pipeline_writes_multiple_commands_and_associates_responses_fifo() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind pipeline NNTP server");
        let port = listener
            .local_addr()
            .expect("get pipeline NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept pipeline NNTP client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound pipeline command reads");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 pipeline server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "BODY <first@example.test>\r\n");
            assert_eq!(next_command(&mut reader), "BODY <second@example.test>\r\n");
            respond(
                &mut reader,
                b"222 first body follows\r\n=ybegin line=128 size=1 name=first\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
            respond(
                &mut reader,
                b"222 second body follows\r\n=ybegin line=128 size=1 name=second\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create pipeline pool registry"));
        let first_request = pool_request(port, "first@example.test");
        let pool_reference = pools
            .reference(&first_request, 1, 2)
            .expect("reference physical pipeline pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let (first, first_result) = pipeline_task(
            first_request,
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        let (second, second_result) = pipeline_task(
            pool_request(port, "second@example.test"),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        assert!(pool.enqueue(first).expect("enqueue first pipeline task"));
        assert!(!pool.enqueue(second).expect("enqueue second pipeline task"));

        pools.drive_pipeline(Arc::clone(&pool));

        assert_eq!(
            first_result
                .recv()
                .expect("receive first pipeline result")
                .expect("decode first pipeline result")
                .bytes,
            b"A"
        );
        assert_eq!(
            second_result
                .recv()
                .expect("receive second pipeline result")
                .expect("decode second pipeline result")
                .bytes,
            b"B"
        );
        let stats = pools.stats();
        assert_eq!(stats.open, 1);
        assert_eq!(stats.active, 0);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.poisoned, 0);
        server.join().expect("join pipeline NNTP server");
    }

    #[test]
    fn pipeline_queue_is_bounded_before_network_dispatch() {
        let pools = PoolRegistry::new(1).expect("create bounded pipeline registry");
        let request = pool_request(119, "bounded@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference bounded physical pipeline");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let mut receivers = Vec::with_capacity(MAX_PIPELINE_QUEUE);
        for index in 0..MAX_PIPELINE_QUEUE {
            let (task, receiver) = pipeline_task(
                request.clone(),
                FlightCancellation::new(),
                pool_reference.clone(),
            );
            assert_eq!(
                pool.enqueue(task).expect("enqueue bounded pipeline task"),
                index == 0
            );
            receivers.push(receiver);
        }
        let (overflow, overflow_result) =
            pipeline_task(request, FlightCancellation::new(), pool_reference);
        assert_eq!(pool.enqueue(overflow), Err("nntp_pipeline_capacity"));
        assert_eq!(
            overflow_result
                .recv()
                .expect_err("overflow sender was released"),
            mpsc::RecvError
        );
    }

    #[test]
    fn scheduler_reserves_rounded_bytes_and_releases_every_admission_exactly_once() {
        assert_eq!(rounded_article_cost(1), Ok(ARTICLE_RESERVATION_QUANTUM));
        assert_eq!(
            rounded_article_cost(ARTICLE_RESERVATION_QUANTUM),
            Ok(ARTICLE_RESERVATION_QUANTUM)
        );
        assert_eq!(
            rounded_article_cost(ARTICLE_RESERVATION_QUANTUM + 1),
            Ok(2 * ARTICLE_RESERVATION_QUANTUM)
        );
        assert_eq!(rounded_article_cost(0), Err("invalid_nntp_article_cost"));
        assert_eq!(
            rounded_article_cost(MAX_DECLARED_ARTICLE_BYTES + 1),
            Err("invalid_nntp_article_cost")
        );
        assert_eq!(
            article_read_limit(MAX_DECLARED_ARTICLE_BYTES as usize),
            Ok(MAX_DECLARED_ARTICLE_BYTES as usize)
        );
        assert_eq!(article_read_limit(0), Err("invalid_nntp_article_cost"));
        assert_eq!(
            article_read_limit(MAX_DECLARED_ARTICLE_BYTES as usize + 1),
            Err("invalid_nntp_article_cost")
        );

        let pools = Arc::new(PoolRegistry::new(1).expect("create byte reservation registry"));
        let request = pool_request(119, "reservation@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference byte reservation pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let full = pool
            .reserve_pipeline(MAX_DECLARED_ARTICLE_BYTES, WorkClass::Interactive)
            .expect("reserve full byte capacity");
        let reserved = pools.stats();
        assert_eq!(reserved.reserved_commands, 1);
        assert_eq!(reserved.reserved_encoded_bytes, MAX_DECLARED_ARTICLE_BYTES);
        assert_eq!(reserved.reserved_decoded_bytes, MAX_DECLARED_ARTICLE_BYTES);
        assert_eq!(
            pools.verified_part(
                pool_reference,
                request,
                None,
                1024,
                FlightCancellation::new(),
            ),
            Err("native_busy")
        );
        let rejected = pools.stats();
        assert_eq!(rejected.scheduler_busy_rejections, 1);
        assert_eq!(rejected.provider_attempts, 0);
        assert_eq!(rejected.reserved_commands, 1);
        drop(full);
        let released = pools.stats();
        assert_eq!(released.reserved_commands, 0);
        assert_eq!(released.reserved_encoded_bytes, 0);
        assert_eq!(released.reserved_decoded_bytes, 0);

        let command_pools = PoolRegistry::new(100).expect("create command reservation registry");
        let command_request = pool_request(119, "command-reservation@example.test");
        let command_reference = command_pools
            .reference(&command_request, 100, MAX_PIPELINE_DEPTH)
            .expect("reference command reservation pool");
        let command_pool = Arc::clone(&command_reference.inner.pool);
        let reservations = (0..MAX_PIPELINE_QUEUE)
            .map(|_| {
                command_pool
                    .reserve_pipeline(1, WorkClass::Interactive)
                    .expect("reserve one command")
            })
            .collect::<Vec<_>>();
        assert_eq!(
            command_pool
                .reserve_pipeline(1, WorkClass::Interactive)
                .map(|_| ()),
            Err("native_busy")
        );
        let full_commands = command_pools.stats();
        assert_eq!(full_commands.reserved_commands, MAX_PIPELINE_QUEUE);
        assert_eq!(
            full_commands.reserved_encoded_bytes,
            MAX_PIPELINE_QUEUE as u64 * ARTICLE_RESERVATION_QUANTUM
        );
        drop(reservations);
        let released_commands = command_pools.stats();
        assert_eq!(released_commands.reserved_commands, 0);
        assert_eq!(released_commands.reserved_encoded_bytes, 0);
        assert_eq!(released_commands.reserved_decoded_bytes, 0);
    }

    #[test]
    fn decoded_memory_budget_is_global_and_preserves_interactive_capacity() {
        let article = MAX_DECLARED_ARTICLE_BYTES;
        let pools = PoolRegistry::new_with_decoded_budget(2, article * 4)
            .expect("create globally byte-bounded pool registry");
        let first_request = pool_request(119, "first-budget@example.test");
        let second_request = pool_request(120, "second-budget@example.test");
        let first_reference = pools
            .reference(&first_request, 1, 2)
            .expect("reference first physical pool");
        let second_reference = pools
            .reference(&second_request, 1, 2)
            .expect("reference second physical pool");
        let first_pool = Arc::clone(&first_reference.inner.pool);
        let second_pool = Arc::clone(&second_reference.inner.pool);

        let first = first_pool
            .reserve_pipeline(article, WorkClass::Background)
            .expect("reserve background capacity");
        let second = second_pool
            .reserve_pipeline(article, WorkClass::Preparation)
            .expect("borrow otherwise idle preparation capacity");
        assert_eq!(
            second_pool
                .reserve_pipeline(1, WorkClass::Preparation)
                .map(|_| ()),
            Err("native_busy")
        );
        assert_eq!(pools.stats().reserved_decoded_bytes, article * 2);

        let interactive = second_pool
            .reserve_pipeline(article, WorkClass::Interactive)
            .expect("use the first interactive hedge reservation");
        let interactive_hedge = first_pool
            .reserve_pipeline(article, WorkClass::Interactive)
            .expect("use the second interactive hedge reservation");
        assert_eq!(pools.stats().reserved_decoded_bytes, article * 4);
        assert_eq!(
            second_pool
                .reserve_pipeline(1, WorkClass::Interactive)
                .map(|_| ()),
            Err("native_busy")
        );

        drop(first);
        drop(second);
        drop(interactive);
        drop(interactive_hedge);
        assert_eq!(pools.stats().reserved_decoded_bytes, 0);
    }

    #[test]
    fn preparation_memory_tracks_declared_article_cost() {
        let pools = PoolRegistry::new_with_decoded_budget(25, 256 * 1024 * 1024)
            .expect("create dynamically bounded pool registry");
        let request = pool_request(119, "dynamic-budget@example.test");
        let reference = pools
            .reference(&request, 25, 1)
            .expect("reference dynamic budget pool");
        let article_bytes = 768_000;
        let reservations = (0..25)
            .map(|_| {
                reference
                    .inner
                    .pool
                    .reserve_pipeline(article_bytes, WorkClass::Preparation)
                    .expect("reserve small preparation article")
            })
            .collect::<Vec<_>>();

        assert_eq!(pools.preparation_slots(), 25);
        assert_eq!(
            pools.stats().reserved_decoded_bytes,
            25 * rounded_article_cost(article_bytes).expect("rounded article cost")
        );
        drop(reservations);
        assert_eq!(pools.stats().reserved_decoded_bytes, 0);
    }

    #[test]
    fn scheduled_reader_does_not_treat_declared_cost_as_the_yenc_wire_limit() {
        let decoded = vec![19_u8; 40 * 1024];
        let mut article = format!(
            "=ybegin line=128 size={} name=expanded.bin\r\n",
            decoded.len()
        )
        .into_bytes();
        for chunk in decoded.chunks(64) {
            for byte in chunk {
                let encoded = byte.wrapping_add(42);
                assert_eq!(encoded, b'=');
                article.extend_from_slice(&[b'=', encoded.wrapping_add(64)]);
            }
            article.extend_from_slice(b"\r\n");
        }
        article.extend_from_slice(
            format!(
                "=yend size={} crc32={:08x}\r\n",
                decoded.len(),
                crc32fast::hash(&decoded),
            )
            .as_bytes(),
        );
        assert!(
            article.len()
                > usize::try_from(ARTICLE_RESERVATION_QUANTUM)
                    .expect("reservation quantum fits usize")
        );
        assert!(article.len() < 128 * 1024);

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind expanded yEnc NNTP server");
        let port = listener
            .local_addr()
            .expect("get expanded yEnc NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept expanded yEnc NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 expanded yEnc server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(
                next_command(&mut reader),
                "BODY <expanded@example.test>\r\n"
            );
            respond(&mut reader, b"222 body follows\r\n");
            respond(&mut reader, &article);
            respond(&mut reader, b".\r\n");
        });

        let pools = Arc::new(PoolRegistry::new(1).expect("create expanded yEnc pool"));
        let request = pool_request(port, "expanded@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference expanded yEnc pool");
        let result = pools
            .verified_part_scheduled(
                pool_reference,
                request,
                None,
                128 * 1024,
                FlightCancellation::new(),
                SchedulingContext {
                    work_class: WorkClass::Interactive,
                    configuration_partition: [1; 32],
                    session: "expanded-yenc".into(),
                    declared_encoded_bytes: u64::try_from(decoded.len())
                        .expect("decoded length fits u64"),
                },
            )
            .expect("read yEnc expansion beyond declared reservation");

        assert_eq!(result.bytes, decoded);
        let stats = pools.stats();
        assert_eq!(stats.provider_attempts, 1);
        assert_eq!(stats.provider_hits, 1);
        assert_eq!(stats.reserved_commands, 0);
        assert_eq!(stats.reserved_encoded_bytes, 0);
        assert_eq!(stats.reserved_decoded_bytes, 0);
        server.join().expect("join expanded yEnc NNTP server");
    }

    #[test]
    fn scheduler_prioritizes_work_class_then_round_robins_partitions_and_sessions() {
        let pools = PoolRegistry::new(1).expect("create fair pipeline registry");
        let request = pool_request(119, "scheduler@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference fair physical pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let enqueue =
            |message_id: &str, work_class: WorkClass, partition_byte: u8, session: &str| {
                let (mut task, receiver) = pipeline_task(
                    pool_request(119, message_id),
                    FlightCancellation::new(),
                    pool_reference.clone(),
                );
                task.scheduling = SchedulingContext {
                    work_class,
                    configuration_partition: [partition_byte; 32],
                    session: session.into(),
                    declared_encoded_bytes: 1024,
                };
                let _ = pool.enqueue(task).expect("enqueue scheduled task");
                receiver
            };
        let receivers = vec![
            enqueue("background@example.test", WorkClass::Background, 1, "a"),
            enqueue("preparation@example.test", WorkClass::Preparation, 1, "a"),
            enqueue("a1@example.test", WorkClass::Interactive, 1, "a"),
            enqueue("a2@example.test", WorkClass::Interactive, 1, "b"),
            enqueue("b1@example.test", WorkClass::Interactive, 2, "a"),
            enqueue("a3@example.test", WorkClass::Interactive, 1, "a"),
        ];
        let queued = pools.stats();
        assert_eq!(queued.queued_interactive, 4);
        assert_eq!(queued.queued_preparation, 1);
        assert_eq!(queued.queued_background, 1);
        let mut order = Vec::new();
        for _ in 0..receivers.len() {
            let (batch, _) = pool
                .take_pipeline_batch()
                .expect("take scheduled pipeline batch");
            assert_eq!(batch.len(), 1);
            order.push(batch[0].request.message_id.clone());
            pool.complete_pipeline_batch();
        }

        assert_eq!(
            order,
            [
                "a1@example.test",
                "b1@example.test",
                "a2@example.test",
                "a3@example.test",
                "preparation@example.test",
                "background@example.test",
            ]
        );
        drop(receivers);
        assert!(pool.take_pipeline_batch().is_none());
        let drained = pools.stats();
        assert_eq!(drained.queued_interactive, 0);
        assert_eq!(drained.queued_preparation, 0);
        assert_eq!(drained.queued_background, 0);
    }

    #[test]
    fn scheduler_observes_singleflight_priority_promotion_while_queued() {
        let pools = PoolRegistry::new(1).expect("create promoted pipeline registry");
        let request = pool_request(119, "promoted@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference promoted physical pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let cancellation = FlightCancellation::new();
        let (mut promoted, promoted_receiver) = pipeline_task(
            pool_request(119, "promoted@example.test"),
            cancellation.clone(),
            pool_reference.clone(),
        );
        promoted.scheduling.work_class = WorkClass::Background;
        let (mut preparation, preparation_receiver) = pipeline_task(
            pool_request(119, "preparation@example.test"),
            FlightCancellation::new(),
            pool_reference,
        );
        preparation.scheduling.work_class = WorkClass::Preparation;
        let _ = pool.enqueue(promoted).expect("enqueue promoted task");
        let _ = pool.enqueue(preparation).expect("enqueue preparation task");

        cancellation.promote(crate::cache::FlightPriority::Interactive);

        let queued = pools.stats();
        assert_eq!(queued.queued_interactive, 1);
        assert_eq!(queued.queued_preparation, 1);
        assert_eq!(queued.queued_background, 0);
        let (batch, _) = pool
            .take_pipeline_batch()
            .expect("take promoted pipeline batch");
        assert_eq!(batch[0].request.message_id, "promoted@example.test");
        pool.complete_pipeline_batch();
        drop(batch);
        drop(promoted_receiver);
        drop(preparation_receiver);
    }

    #[test]
    fn scheduler_starts_no_background_command_ahead_of_an_interactive_waiter() {
        for connection_limit in [4, 5] {
            let pools =
                PoolRegistry::new(connection_limit).expect("create background reserve registry");
            let request = pool_request(119, "background-reserve@example.test");
            let pool_reference = pools
                .reference(&request, connection_limit, 1)
                .expect("reference background reserve pool");
            let pool = Arc::clone(&pool_reference.inner.pool);
            let enqueue = |message_id: &str, work_class: WorkClass| {
                let (mut task, receiver) = pipeline_task(
                    pool_request(119, message_id),
                    FlightCancellation::new(),
                    pool_reference.clone(),
                );
                task.scheduling = SchedulingContext {
                    work_class,
                    configuration_partition: [1; 32],
                    session: "reserve".into(),
                    declared_encoded_bytes: 1024,
                };
                let _ = pool.enqueue(task).expect("enqueue background reserve task");
                receiver
            };
            let mut receivers = Vec::new();
            let mut batches = Vec::new();
            for index in 0..connection_limit {
                receivers.push(enqueue(
                    &format!("active-background-{index}@example.test"),
                    WorkClass::Background,
                ));
            }
            for _ in 0..connection_limit {
                let (batch, _) = pool
                    .take_pipeline_batch()
                    .expect("start borrowed background batch");
                assert_eq!(batch[0].scheduling.work_class, WorkClass::Background);
                batches.push(batch);
            }

            receivers.push(enqueue(
                "queued-background@example.test",
                WorkClass::Background,
            ));
            receivers.push(enqueue(
                "interactive-one@example.test",
                WorkClass::Interactive,
            ));
            receivers.push(enqueue(
                "interactive-two@example.test",
                WorkClass::Interactive,
            ));
            for expected in [
                "interactive-one@example.test",
                "interactive-two@example.test",
            ] {
                pool.complete_pipeline_batch();
                let (batch, _) = pool
                    .take_pipeline_batch()
                    .expect("replace drained background with interactive work");
                assert_eq!(batch[0].request.message_id, expected);
                batches.push(batch);
            }
            pool.complete_pipeline_batch();
            let (borrowed, _) = pool
                .take_pipeline_batch()
                .expect("background borrows after interactive queue drains");
            assert_eq!(
                borrowed[0].request.message_id,
                "queued-background@example.test"
            );
            batches.push(borrowed);

            for _ in 0..connection_limit {
                pool.complete_pipeline_batch();
            }
            for _ in 0..connection_limit {
                assert!(pool.take_pipeline_batch().is_none());
            }
            drop(batches);
            drop(receivers);
        }
    }

    #[test]
    fn scheduler_round_robins_compatible_sessions_within_one_pipeline_batch() {
        let pools = PoolRegistry::new(1).expect("create batched fair pipeline registry");
        let request = pool_request(119, "batched-scheduler@example.test");
        let pool_reference = pools
            .reference(&request, 1, 4)
            .expect("reference batched fair physical pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let enqueue = |message_id: &str, session: &str| {
            let (mut task, receiver) = pipeline_task(
                pool_request(119, message_id),
                FlightCancellation::new(),
                pool_reference.clone(),
            );
            task.scheduling = SchedulingContext {
                work_class: WorkClass::Interactive,
                configuration_partition: [1; 32],
                session: session.into(),
                declared_encoded_bytes: 1024,
            };
            let _ = pool.enqueue(task).expect("enqueue batched scheduled task");
            receiver
        };
        let receivers = [
            enqueue("a1@example.test", "a"),
            enqueue("a2@example.test", "a"),
            enqueue("b1@example.test", "b"),
            enqueue("b2@example.test", "b"),
        ];

        let (batch, start_dispatcher) = pool
            .take_pipeline_batch()
            .expect("take batched scheduled pipeline work");
        assert!(!start_dispatcher);
        assert_eq!(
            batch
                .iter()
                .map(|task| task.request.message_id.as_str())
                .collect::<Vec<_>>(),
            [
                "a1@example.test",
                "b1@example.test",
                "a2@example.test",
                "b2@example.test",
            ]
        );
        pool.complete_pipeline_batch();
        drop(batch);
        drop(receivers);
        assert!(pool.take_pipeline_batch().is_none());
    }

    #[test]
    fn dispatcher_state_coalesces_idle_work_and_uses_free_connections() {
        let pools = PoolRegistry::new(2).expect("create dispatcher state registry");
        let request = pool_request(119, "dispatcher@example.test");
        let pool_reference = pools
            .reference(&request, 2, 2)
            .expect("reference dispatcher state pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let (first, first_result) = pipeline_task(
            request.clone(),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        let (second, second_result) = pipeline_task(
            request.clone(),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        assert!(pool.enqueue(first).expect("enqueue first idle task"));
        assert!(
            !pool
                .enqueue(second)
                .expect("coalesce behind the idle dispatcher")
        );
        let (first_batch, start_dispatcher) =
            pool.take_pipeline_batch().expect("take coalesced batch");
        assert_eq!(first_batch.len(), 2);
        assert!(!start_dispatcher);

        let (third, third_result) =
            pipeline_task(request, FlightCancellation::new(), pool_reference);
        assert!(
            pool.enqueue(third)
                .expect("start another dispatcher while the first is busy")
        );
        let (second_batch, start_dispatcher) =
            pool.take_pipeline_batch().expect("take parallel batch");
        assert_eq!(second_batch.len(), 1);
        assert!(!start_dispatcher);

        pool.complete_pipeline_batch();
        pool.complete_pipeline_batch();
        assert!(pool.take_pipeline_batch().is_none());
        assert!(pool.take_pipeline_batch().is_none());
        drop(first_batch);
        drop(second_batch);
        assert_eq!(
            first_result
                .recv()
                .expect_err("first test sender was released"),
            mpsc::RecvError
        );
        assert_eq!(
            second_result
                .recv()
                .expect_err("second test sender was released"),
            mpsc::RecvError
        );
        assert_eq!(
            third_result
                .recv()
                .expect_err("third test sender was released"),
            mpsc::RecvError
        );
    }

    #[test]
    fn pipeline_batches_stop_at_generation_and_priority_boundaries() {
        let pools = PoolRegistry::new(1).expect("create lane-isolation registry");
        let request = pool_request(119, "lane@example.test");
        let first_generation = pools
            .reference_for_generation(&request, 1, 4, &"a".repeat(64), 0, false)
            .expect("reference first generation lane");
        let second_generation = pools
            .reference_for_generation(&request, 1, 4, &"b".repeat(64), 0, false)
            .expect("reference second generation lane");
        let second_priority = pools
            .reference_for_generation(&request, 1, 4, &"b".repeat(64), 1, false)
            .expect("reference second priority lane");
        let backup_priority = pools
            .reference_for_generation(&request, 1, 4, &"b".repeat(64), 1, true)
            .expect("reference backup priority lane");
        assert!(Arc::ptr_eq(
            &first_generation.inner.pool,
            &second_generation.inner.pool,
        ));
        assert!(Arc::ptr_eq(
            &second_generation.inner.pool,
            &second_priority.inner.pool,
        ));
        let pool = Arc::clone(&first_generation.inner.pool);
        let mut receivers = Vec::new();
        for (message_id, reference) in [
            ("first-generation@example.test", first_generation),
            ("second-generation@example.test", second_generation),
            ("second-priority-one@example.test", second_priority.clone()),
            ("second-priority-two@example.test", second_priority),
            ("backup-priority@example.test", backup_priority),
        ] {
            let (task, receiver) = pipeline_task(
                pool_request(119, message_id),
                FlightCancellation::new(),
                reference,
            );
            let _ = pool.enqueue(task).expect("enqueue isolated lane task");
            receivers.push(receiver);
        }

        for expected_length in [1, 1, 2, 1] {
            let (batch, start_dispatcher) = pool
                .take_pipeline_batch()
                .expect("take isolated lane batch");
            assert_eq!(batch.len(), expected_length);
            assert!(!start_dispatcher);
            assert!(batch.iter().all(|task| {
                task._pool_reference.inner.lane == batch[0]._pool_reference.inner.lane
            }));
            pool.complete_pipeline_batch();
            drop(batch);
        }
        assert!(pool.take_pipeline_batch().is_none());
        for receiver in receivers {
            assert_eq!(
                receiver
                    .recv()
                    .expect_err("isolated lane sender was released"),
                mpsc::RecvError
            );
        }
    }

    #[test]
    fn different_generations_never_share_one_wire_pipeline() {
        let listener =
            TcpListener::bind("127.0.0.1:0").expect("bind generation-isolation NNTP server");
        let port = listener
            .local_addr()
            .expect("get generation-isolation NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept generation-isolation NNTP client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound generation-isolation command reads");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 generation-isolation server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(
                next_command(&mut reader),
                "BODY <first-generation@example.test>\r\n"
            );

            assert!(
                reader.buffer().is_empty(),
                "a later generation command was buffered with the first command"
            );
            reader
                .get_ref()
                .set_nonblocking(true)
                .expect("make generation-isolation socket nonblocking");
            thread::sleep(Duration::from_millis(20));
            let mut command = [0_u8; 128];
            match reader.get_ref().peek(&mut command) {
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {}
                Ok(0) => panic!("generation-isolation client closed before the first response"),
                Ok(length) => panic!(
                    "a later generation command reached the wire before the first response: {:?}",
                    &command[..length]
                ),
                Err(error) => panic!("peek generation-isolation socket: {error}"),
            }
            reader
                .get_ref()
                .set_nonblocking(false)
                .expect("restore generation-isolation socket");
            respond(
                &mut reader,
                b"222 first body follows\r\n=ybegin line=128 size=1 name=first\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );

            assert_eq!(
                next_command(&mut reader),
                "BODY <second-generation@example.test>\r\n"
            );
            respond(
                &mut reader,
                b"222 second body follows\r\n=ybegin line=128 size=1 name=second\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n",
            );
        });

        let pools = Arc::new(PoolRegistry::new(1).expect("create generation-isolation registry"));
        let request = pool_request(port, "first-generation@example.test");
        let first_reference = pools
            .reference_for_generation(&request, 1, 2, &"a".repeat(64), 0, false)
            .expect("reference first wire generation");
        let second_reference = pools
            .reference_for_generation(&request, 1, 2, &"b".repeat(64), 0, false)
            .expect("reference second wire generation");
        let pool = Arc::clone(&first_reference.inner.pool);
        let (first, first_result) =
            pipeline_task(request, FlightCancellation::new(), first_reference);
        let (second, second_result) = pipeline_task(
            pool_request(port, "second-generation@example.test"),
            FlightCancellation::new(),
            second_reference,
        );
        assert!(pool.enqueue(first).expect("enqueue first wire generation"));
        assert!(
            !pool
                .enqueue(second)
                .expect("enqueue second wire generation")
        );

        pools.drive_pipeline(Arc::clone(&pool));

        assert_eq!(
            first_result
                .recv()
                .expect("receive first wire generation")
                .expect("decode first wire generation")
                .bytes,
            b"A"
        );
        assert_eq!(
            second_result
                .recv()
                .expect("receive second wire generation")
                .expect("decode second wire generation")
                .bytes,
            b"B"
        );
        server
            .join()
            .expect("join generation-isolation NNTP server");
    }

    #[test]
    fn lane_boundary_activates_another_free_connection_dispatcher() {
        let pools = PoolRegistry::new(2).expect("create work-conserving lane registry");
        let request = pool_request(119, "work-conserving@example.test");
        let first_reference = pools
            .reference_for_generation(&request, 2, 4, &"a".repeat(64), 0, false)
            .expect("reference first work-conserving lane");
        let second_reference = pools
            .reference_for_generation(&request, 2, 4, &"b".repeat(64), 0, false)
            .expect("reference second work-conserving lane");
        let pool = Arc::clone(&first_reference.inner.pool);
        let (first, first_result) = pipeline_task(
            pool_request(119, "first-work@example.test"),
            FlightCancellation::new(),
            first_reference,
        );
        let (second, second_result) = pipeline_task(
            pool_request(119, "second-work@example.test"),
            FlightCancellation::new(),
            second_reference,
        );
        assert!(pool.enqueue(first).expect("enqueue first work lane"));
        assert!(!pool.enqueue(second).expect("enqueue second work lane"));

        let (first_batch, start_dispatcher) =
            pool.take_pipeline_batch().expect("take first work lane");
        assert_eq!(first_batch.len(), 1);
        assert!(start_dispatcher);
        let (second_batch, start_dispatcher) =
            pool.take_pipeline_batch().expect("take second work lane");
        assert_eq!(second_batch.len(), 1);
        assert!(!start_dispatcher);
        pool.complete_pipeline_batch();
        pool.complete_pipeline_batch();
        assert!(pool.take_pipeline_batch().is_none());
        assert!(pool.take_pipeline_batch().is_none());
        drop(first_batch);
        drop(second_batch);
        assert_eq!(
            first_result
                .recv()
                .expect_err("first work lane sender was released"),
            mpsc::RecvError
        );
        assert_eq!(
            second_result
                .recv()
                .expect_err("second work lane sender was released"),
            mpsc::RecvError
        );
    }

    #[test]
    fn cancelled_queued_request_stops_waiting_before_dispatch() {
        let pools = Arc::new(PoolRegistry::new(1).expect("create queued cancellation registry"));
        let request = pool_request(119, "queued@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference queued cancellation pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let (blocker, blocker_result) = pipeline_task(
            request.clone(),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        assert!(pool.enqueue(blocker).expect("reserve idle dispatcher"));

        let cancellation = FlightCancellation::new();
        let caller_cancellation = cancellation.clone();
        let caller_pools = Arc::clone(&pools);
        let caller = thread::spawn(move || {
            caller_pools.verified_part(pool_reference, request, None, 1024, caller_cancellation)
        });
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            if pool
                .state
                .lock()
                .expect("inspect queued cancellation pool")
                .pipeline_queue
                .len()
                == 2
            {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "cancelled request never entered the pipeline queue"
            );
            thread::yield_now();
        }
        cancellation.cancel();
        assert_eq!(
            caller.join().expect("join cancelled queued caller"),
            Err("nntp_cancelled")
        );
        let stats = pools.stats();
        assert_eq!(stats.provider_attempts, 1);
        assert_eq!(stats.provider_cancellations, 1);
        assert_eq!(
            stats.provider_attempts,
            stats.provider_hits
                + stats.provider_missing
                + stats.provider_corrupt
                + stats.provider_failures
                + stats.provider_cancellations
        );

        let (queued, start_dispatcher) = pool
            .take_pipeline_batch()
            .expect("remove cancelled queued tasks");
        assert_eq!(queued.len(), 1);
        assert!(!start_dispatcher);
        pool.complete_pipeline_batch();
        drop(queued);
        let (queued, start_dispatcher) = pool
            .take_pipeline_batch()
            .expect("remove the second cancelled queued task");
        assert_eq!(queued.len(), 1);
        assert!(!start_dispatcher);
        pool.complete_pipeline_batch();
        assert!(pool.take_pipeline_batch().is_none());
        drop(queued);
        assert_eq!(
            blocker_result
                .recv()
                .expect_err("queued blocker sender was released"),
            mpsc::RecvError
        );
        let released = pools.stats();
        assert_eq!(released.reserved_commands, 0);
        assert_eq!(released.reserved_encoded_bytes, 0);
        assert_eq!(released.reserved_decoded_bytes, 0);
    }

    #[test]
    fn concurrent_verified_parts_share_one_wire_pipeline() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind concurrent pipeline server");
        let port = listener
            .local_addr()
            .expect("get concurrent pipeline address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept concurrent pipeline client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound concurrent command reads");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 concurrent pipeline ready\r\n");
            enter_reader_mode(&mut reader);
            let first_command = next_command(&mut reader);
            let second_command = next_command(&mut reader);
            assert_ne!(first_command, second_command);
            assert!(
                [
                    "BODY <alpha@example.test>\r\n",
                    "BODY <beta@example.test>\r\n"
                ]
                .contains(&first_command.as_str())
            );
            assert!(
                [
                    "BODY <alpha@example.test>\r\n",
                    "BODY <beta@example.test>\r\n"
                ]
                .contains(&second_command.as_str())
            );
            for command in [first_command, second_command] {
                let response = if command.contains("alpha@") {
                    b"222 alpha body follows\r\n=ybegin line=128 size=1 name=alpha\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n".as_slice()
                } else {
                    b"222 beta body follows\r\n=ybegin line=128 size=1 name=beta\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n".as_slice()
                };
                respond(&mut reader, response);
            }
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create concurrent pipeline registry"));
        let pool_reference = pools
            .reference(&pool_request(port, "alpha@example.test"), 1, 2)
            .expect("reference concurrent pipeline pool");
        let barrier = Arc::new(Barrier::new(3));
        let clients = [("alpha@example.test", b'A'), ("beta@example.test", b'B')]
            .into_iter()
            .map(|(message_id, expected)| {
                let pools = Arc::clone(&pools);
                let pool_reference = pool_reference.clone();
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    barrier.wait();
                    let part = pools
                        .verified_part(
                            pool_reference,
                            pool_request(port, message_id),
                            None,
                            1024,
                            FlightCancellation::new(),
                        )
                        .expect("fetch concurrent pipeline part");
                    assert_eq!(part.bytes, [expected]);
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();
        for client in clients {
            client.join().expect("join concurrent pipeline client");
        }
        server.join().expect("join concurrent pipeline server");
        let stats =
            wait_for_pool_stats(&pools, "concurrent pipeline connection to idle", |stats| {
                stats.open == 1 && stats.idle == 1 && stats.reserved_commands == 0
            });
        assert_eq!(stats.open, 1);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.poisoned, 0);
        assert_eq!(stats.reserved_commands, 0);
        assert_eq!(stats.reserved_encoded_bytes, 0);
        assert_eq!(stats.reserved_decoded_bytes, 0);
    }

    #[test]
    fn pipeline_drains_synchronized_failures_before_retiring_the_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind pipeline failure NNTP server");
        let port = listener
            .local_addr()
            .expect("get pipeline failure NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept pipeline failure NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 pipeline failure server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "BODY <missing@example.test>\r\n");
            assert_eq!(next_command(&mut reader), "BODY <present@example.test>\r\n");
            respond(&mut reader, b"430 no such article\r\n");
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=present\r\nl\r\n=yend size=1 crc32=4ad0cf31\r\n.\r\n",
            );
            assert_eq!(next_command(&mut reader), "BODY <failed@example.test>\r\n");
            assert_eq!(
                next_command(&mut reader),
                "BODY <second-present@example.test>\r\n"
            );
            respond(&mut reader, b"500 temporary command failure\r\n");
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=second-present\r\nm\r\n=yend size=1 crc32=3dd7ffa7\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create pipeline failure pool registry"));
        let missing_request = pool_request(port, "missing@example.test");
        let pool_reference = pools
            .reference(&missing_request, 1, 2)
            .expect("reference physical pipeline failure pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let (missing, missing_result) = pipeline_task(
            missing_request,
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        let (present, present_result) = pipeline_task(
            pool_request(port, "present@example.test"),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        assert!(pool.enqueue(missing).expect("enqueue missing task"));
        assert!(!pool.enqueue(present).expect("enqueue present task"));

        pools.drive_pipeline(Arc::clone(&pool));

        assert_eq!(
            missing_result
                .recv()
                .expect("receive missing pipeline result")
                .expect_err("report missing pipeline result"),
            "nntp_article_missing"
        );
        assert_eq!(
            present_result
                .recv()
                .expect("receive present pipeline result")
                .expect("decode present pipeline result")
                .bytes,
            b"B"
        );
        let stats = wait_for_pool_stats(
            &pools,
            "healthy pipeline connection to become idle",
            |stats| stats.open == 1 && stats.active == 0 && stats.idle == 1,
        );
        assert_eq!(stats.open, 1);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.poisoned, 0);

        let (failed, failed_result) = pipeline_task(
            pool_request(port, "failed@example.test"),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        let (second_present, second_present_result) = pipeline_task(
            pool_request(port, "second-present@example.test"),
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        assert!(pool.enqueue(failed).expect("enqueue failed task"));
        assert!(
            !pool
                .enqueue(second_present)
                .expect("enqueue second present task")
        );

        pools.drive_pipeline(Arc::clone(&pool));

        assert_eq!(
            failed_result
                .recv()
                .expect("receive failed pipeline result")
                .expect_err("report failed pipeline result"),
            "nntp_body_failed"
        );
        assert_eq!(
            second_present_result
                .recv()
                .expect("receive second present pipeline result")
                .expect("decode second present pipeline result")
                .bytes,
            b"C"
        );
        let stats = wait_for_pool_stats(&pools, "drained pipeline connection to retire", |stats| {
            stats.open == 0 && stats.active == 0 && stats.idle == 0
        });
        assert_eq!(stats.poisoned, 1);
        server.join().expect("join pipeline failure NNTP server");
    }

    #[test]
    fn cancelling_an_inflight_pipeline_response_poisons_the_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind pipeline cancellation server");
        let port = listener
            .local_addr()
            .expect("get pipeline cancellation address")
            .port();
        let (commands_sent, commands_received) = mpsc::sync_channel(1);
        let (cancelled_sent, cancelled_received) = mpsc::sync_channel(1);
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept pipeline cancellation client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound cancellation close wait");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 pipeline cancellation ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "BODY <winner@example.test>\r\n");
            assert_eq!(
                next_command(&mut reader),
                "BODY <cancelled@example.test>\r\n"
            );
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=winner\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
            commands_sent.send(()).expect("signal submitted commands");
            cancelled_received
                .recv()
                .expect("wait for pipeline cancellation");
            let mut after_cancel = String::new();
            assert_eq!(
                reader
                    .read_line(&mut after_cancel)
                    .expect("observe cancelled connection close"),
                0
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create pipeline cancellation registry"));
        let winner_request = pool_request(port, "winner@example.test");
        let pool_reference = pools
            .reference(&winner_request, 1, 2)
            .expect("reference physical pipeline cancellation pool");
        let pool = Arc::clone(&pool_reference.inner.pool);
        let (mut winner, winner_result) = pipeline_task(
            winner_request,
            FlightCancellation::new(),
            pool_reference.clone(),
        );
        let cancellation = FlightCancellation::new();
        let (mut cancelled, cancelled_result) = pipeline_task(
            pool_request(port, "cancelled@example.test"),
            cancellation.clone(),
            pool_reference,
        );
        winner._reservation = Some(
            pool.reserve_pipeline(1024, WorkClass::Interactive)
                .expect("reserve pipeline winner"),
        );
        cancelled._reservation = Some(
            pool.reserve_pipeline(1024, WorkClass::Interactive)
                .expect("reserve cancelled pipeline task"),
        );
        assert!(pool.enqueue(winner).expect("enqueue pipeline winner"));
        assert!(
            !pool
                .enqueue(cancelled)
                .expect("enqueue cancelled pipeline task")
        );
        let dispatcher_registry = Arc::clone(&pools);
        let dispatcher_pool = Arc::clone(&pool);
        let dispatcher = thread::spawn(move || dispatcher_registry.drive_pipeline(dispatcher_pool));

        commands_received
            .recv()
            .expect("wait for submitted pipeline commands");
        assert_eq!(
            winner_result
                .recv()
                .expect("receive pipeline winner")
                .expect("decode pipeline winner")
                .bytes,
            b"A"
        );
        let winner_released = pools.stats();
        assert_eq!(winner_released.reserved_commands, 1);
        assert_eq!(
            winner_released.reserved_encoded_bytes,
            ARTICLE_RESERVATION_QUANTUM
        );
        assert_eq!(
            winner_released.reserved_decoded_bytes,
            ARTICLE_RESERVATION_QUANTUM
        );
        cancellation.cancel();
        cancelled_sent
            .send(())
            .expect("release cancellation server");
        assert_eq!(
            cancelled_result
                .recv()
                .expect("receive cancelled pipeline result")
                .expect_err("cancel in-flight response"),
            "nntp_cancelled"
        );
        let cancelled_released = pools.stats();
        assert_eq!(cancelled_released.reserved_commands, 0);
        assert_eq!(cancelled_released.reserved_encoded_bytes, 0);
        assert_eq!(cancelled_released.reserved_decoded_bytes, 0);
        dispatcher.join().expect("join pipeline dispatcher");
        server.join().expect("join pipeline cancellation server");
        let stats = pools.stats();
        assert_eq!(stats.open, 0);
        assert_eq!(stats.active, 0);
        assert_eq!(stats.idle, 0);
        assert_eq!(stats.poisoned, 1);
    }

    #[test]
    fn corrupt_decode_retains_the_lease_and_balances_provider_telemetry() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind corrupt-lease NNTP server");
        let port = listener
            .local_addr()
            .expect("get corrupt-lease NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept corrupt NNTP client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 corrupt-lease test server ready\r\n");
            enter_reader_mode(&mut reader);
            assert_eq!(next_command(&mut reader), "BODY <corrupt@example.test>\r\n");
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=00000000\r\n.\r\n",
            );
            assert_eq!(next_command(&mut reader), "BODY <missing@example.test>\r\n");
            respond(&mut reader, b"430 no such article\r\n");
            assert_eq!(next_command(&mut reader), "BODY <valid@example.test>\r\n");
            respond(
                &mut reader,
                b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
            );
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create corrupt-lease pool registry"));
        let corrupt_request = pool_request(port, "corrupt@example.test");
        let pool_reference = pools
            .reference(&corrupt_request, 1, 1)
            .expect("reference corrupt-lease pool");

        assert_eq!(
            pools
                .verified_part(
                    pool_reference.clone(),
                    corrupt_request,
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect_err("reject corrupt pooled response"),
            "yenc_crc_mismatch"
        );
        assert_eq!(
            pools
                .verified_part(
                    pool_reference.clone(),
                    pool_request(port, "missing@example.test"),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect_err("report missing pooled response"),
            "nntp_article_missing"
        );
        assert_eq!(
            pools
                .verified_part(
                    pool_reference.clone(),
                    pool_request(port, "valid@example.test"),
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect("fetch through retained connection")
                .bytes,
            b"A"
        );
        let stats = wait_for_pool_stats(&pools, "retained connection to become idle", |stats| {
            stats.open == 1 && stats.active == 0 && stats.idle == 1
        });
        assert_eq!(stats.pools, 1);
        assert_eq!(stats.open, 1);
        assert_eq!(stats.active, 0);
        assert_eq!(stats.idle, 1);
        assert_eq!(stats.poisoned, 0);
        assert_eq!(stats.provider_attempts, 3);
        assert_eq!(stats.provider_suppliers, 1);
        assert_eq!(stats.provider_hits, 1);
        assert_eq!(stats.provider_missing, 1);
        assert_eq!(stats.provider_corrupt, 1);
        assert_eq!(stats.provider_failures, 0);
        assert_eq!(stats.provider_cancellations, 0);
        assert_eq!(
            stats.provider_attempts,
            stats.provider_hits
                + stats.provider_missing
                + stats.provider_corrupt
                + stats.provider_failures
                + stats.provider_cancellations
        );
        server.join().expect("join corrupt-lease NNTP server");
    }

    #[test]
    fn global_ceiling_reclaims_an_idle_socket_before_opening_another_pool() {
        let (first_port, first_server) = one_article_pool_server();
        let (second_port, second_server) = one_article_pool_server();
        let pools = Arc::new(PoolRegistry::new(1).expect("create globally bounded pool registry"));
        let first_request = pool_request(first_port, "first@example.test");
        let first_reference = pools
            .reference(&first_request, 8, 1)
            .expect("reference first bounded pool");

        pools
            .verified_part(
                first_reference.clone(),
                first_request,
                None,
                1024,
                FlightCancellation::new(),
            )
            .expect("fetch through first physical pool");
        let second_request = pool_request(second_port, "second@example.test");
        let second_reference = pools
            .reference(&second_request, 8, 1)
            .expect("reference second bounded pool");
        pools
            .verified_part(
                second_reference.clone(),
                second_request,
                None,
                1024,
                FlightCancellation::new(),
            )
            .expect("reclaim idle socket for second physical pool");

        first_server.join().expect("join first bounded NNTP server");
        second_server
            .join()
            .expect("join second bounded NNTP server");
    }

    #[test]
    fn rejects_unsafe_request_fields_before_connecting() {
        let request = BodyRequest {
            host: "host\n".into(),
            port: 119,
            tls_mode: "plaintext".into(),
            allow_private: false,
            username: None,
            password: None,
            message_id: "id".into(),
        };
        assert_eq!(
            validate_body_template(&request),
            Err("nntp_invalid_request")
        );
        assert!(!valid_group("alt.video\r\nSTAT <injected>"));
        assert!(valid_group("future/hierarchy@v2"));
    }

    #[test]
    fn rejects_unknown_tls_modes_before_connecting() {
        let request = BodyRequest {
            host: "news.example.test".into(),
            port: 563,
            tls_mode: "unknown".into(),
            allow_private: false,
            username: Some("user".into()),
            password: Some("secret".into()),
            message_id: "id".into(),
        };
        assert_eq!(
            validate_body_template(&request),
            Err("nntp_invalid_request")
        );
    }

    #[test]
    fn rejects_ambiguous_message_ids_before_connecting() {
        let request = BodyRequest {
            host: "news.example.test".into(),
            port: 119,
            tls_mode: "plaintext".into(),
            allow_private: false,
            username: None,
            password: None,
            message_id: "<one><two>".into(),
        };
        assert_eq!(body(request, 1024).err(), Some("nntp_invalid_request"));
    }

    #[test]
    fn fetches_and_unstuffs_a_real_nntp_body_exchange() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "BODY <article@example.test>\r\n");
            respond(
                reader,
                b"222 0 <article@example.test> body follows\r\n..first\r\nsecond\r\n.\r\n",
            );
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "article@example.test".into(),
        };

        assert_eq!(body(request, 1024), Ok(b".first\r\nsecond\r\n".to_vec()));
    }

    #[test]
    fn stats_articles_in_one_bounded_pipeline_without_reading_bodies() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "GROUP alt.video\r\n");
            respond(reader, b"211 2 1 2 alt.video\r\n");
            assert_eq!(next_command(reader), "STAT <present@example.test>\r\n");
            assert_eq!(next_command(reader), "STAT <missing@example.test>\r\n");
            respond(
                reader,
                b"223 1 <present@example.test>\r\n430 no such article\r\n",
            );
        });
        let request = pool_request(port, "template@example.test");
        let pools = PoolRegistry::new(1).expect("create STAT pool registry");
        let reference = pools
            .reference(&request, 1, 2)
            .expect("reference STAT pool");

        let result = pools.stat_batch(
            &reference,
            &request,
            Some("alt.video"),
            &["present@example.test", "missing@example.test"],
            &|| false,
        );

        assert_eq!(result, Ok(vec![true, false]));
    }

    #[test]
    fn unsupported_pipelined_stat_discards_the_unread_connection() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "STAT <first@example.test>\r\n");
            assert_eq!(next_command(reader), "STAT <second@example.test>\r\n");
            respond(
                reader,
                b"500 command unsupported\r\n500 command unsupported\r\n",
            );
        });
        let request = pool_request(port, "template@example.test");
        let pools = PoolRegistry::new(1).expect("create unsupported STAT pool registry");
        let reference = pools
            .reference(&request, 1, 2)
            .expect("reference unsupported STAT pool");

        let result = pools.stat_batch(
            &reference,
            &request,
            None,
            &["first@example.test", "second@example.test"],
            &|| false,
        );

        assert_eq!(result, Err("nntp_stat_unsupported"));
        let stats = pools.stats();
        assert_eq!(stats.open, 0);
        assert_eq!(stats.idle, 0);
        assert_eq!(stats.poisoned, 1);
    }

    #[test]
    fn pipeline_enforces_one_total_deadline_against_a_trickling_article() {
        for _ in 0..5 {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind trickling NNTP server");
            let port = listener
                .local_addr()
                .expect("get trickling NNTP address")
                .port();
            let server = thread::spawn(move || {
                let (stream, _) = listener.accept().expect("accept trickling NNTP client");
                let mut reader = BufReader::new(stream);
                respond(&mut reader, b"200 trickling server ready\r\n");
                enter_reader_mode(&mut reader);
                assert_eq!(next_command(&mut reader), "BODY <trickle@example.test>\r\n");
                respond(&mut reader, b"222 body follows\r\n");
                for _ in 0..8 {
                    thread::sleep(Duration::from_millis(80));
                    if reader.get_mut().write_all(b"line\r\n").is_err() {
                        return;
                    }
                }
                let _ = reader.get_mut().write_all(b".\r\n");
            });
            let request = pool_request(port, "trickle@example.test");
            let pools = PoolRegistry::new(1).expect("create trickling pool registry");
            let pool_reference = pools
                .reference(&request, 1, 1)
                .expect("reference trickling physical pool");
            let cancellation = FlightCancellation::new();
            let routing = Arc::clone(&pool_reference.inner.group_routing);
            let (task, result) =
                pipeline_task(request.clone(), cancellation.clone(), pool_reference);
            let mut group_dispatch = routing
                .dispatch(&cancellation)
                .expect("dispatch trickling article");
            let mut connection =
                NntpConnection::open(&request, &|| false).expect("open trickling connection");
            let started = Instant::now();
            let mut tasks = [task];

            assert!(!connection.pipeline_until(
                &mut tasks,
                &mut group_dispatch,
                started + Duration::from_millis(250),
            ));
            assert_eq!(
                result.recv().expect("receive trickling result"),
                Err("nntp_article_timeout")
            );
            assert!(started.elapsed() < Duration::from_secs(1));

            drop(connection);
            server.join().expect("join trickling NNTP server");
        }
    }

    #[test]
    fn rejects_an_unstuffed_leading_dot() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "BODY <article@example.test>\r\n");
            respond(reader, b"222 body follows\r\n.single-dot\r\n.\r\n");
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "article@example.test".into(),
        };

        assert_eq!(body(request, 1024), Err("nntp_invalid_response"));
    }

    #[test]
    fn preserves_non_utf8_article_payload_bytes() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "BODY <article@example.test>\r\n");
            respond(reader, b"222 body follows\r\n\xff\r\n.\r\n");
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "article@example.test".into(),
        };

        assert_eq!(body(request, 1024), Ok(b"\xff\r\n".to_vec()));
    }

    #[test]
    fn reports_missing_articles_without_misclassifying_the_server_failure() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "BODY <missing@example.test>\r\n");
            respond(reader, b"430 no such article\r\n");
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "missing@example.test".into(),
        };

        assert_eq!(body(request, 1024), Err("nntp_article_missing"));
    }

    #[test]
    fn fetches_and_verifies_a_yenc_part_in_one_exchange() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
            assert_eq!(next_command(reader), "BODY <article@example.test>\r\n");
            respond(reader, b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n");
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "article@example.test".into(),
        };

        assert_eq!(verified_part(request, 1024).unwrap().bytes, b"A");
    }

    #[test]
    fn negotiates_raw_deflate_after_auth_and_decodes_the_compressed_body() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind compressed NNTP server");
        let port = listener
            .local_addr()
            .expect("get compressed NNTP address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept compressed NNTP client");
            let mut reader =
                BufReader::new(stream.try_clone().expect("clone compressed NNTP client"));
            respond(&mut reader, b"200 compressed server ready\r\n");
            assert_eq!(next_command(&mut reader), "CAPABILITIES\r\n");
            respond(
                &mut reader,
                b"101 capabilities follow\r\nVERSION 2\r\nCOMPRESS DEFLATE\r\n.\r\n",
            );
            assert_eq!(next_command(&mut reader), "MODE READER\r\n");
            respond(&mut reader, b"200 reader enabled\r\n");
            assert_eq!(next_command(&mut reader), "AUTHINFO USER user\r\n");
            respond(&mut reader, b"381 password required\r\n");
            assert_eq!(next_command(&mut reader), "AUTHINFO PASS secret\r\n");
            respond(&mut reader, b"281 authentication accepted\r\n");
            assert_eq!(next_command(&mut reader), "CAPABILITIES\r\n");
            respond(
                &mut reader,
                b"101 capabilities follow\r\nVERSION 2\r\nCOMPRESS DEFLATE\r\n.\r\n",
            );
            assert_eq!(next_command(&mut reader), "COMPRESS DEFLATE\r\n");
            respond(&mut reader, b"206 compression active\r\n");
            assert!(reader.buffer().is_empty());
            let stream = reader.into_inner();
            let mut reader = BufReader::new(NntpStream::Plain(stream).enable_deflate());
            let mut command = String::new();
            reader
                .read_line(&mut command)
                .expect("read compressed capabilities command");
            assert_eq!(command, "CAPABILITIES\r\n");
            reader
                .get_mut()
                .write_all(b"101 capabilities follow\r\nVERSION 2\r\n.\r\n")
                .expect("write compressed capabilities");
            command.clear();
            reader
                .read_line(&mut command)
                .expect("read compressed BODY command");
            assert_eq!(command, "BODY <article@example.test>\r\n");
            reader
                .get_mut()
                .write_all(
                    b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
                )
                .expect("write compressed body");
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: Some("user".into()),
            password: Some("secret".into()),
            message_id: "article@example.test".into(),
        };

        assert_eq!(verified_part(request, 1024).unwrap().bytes, b"A");
        server.join().expect("join compressed NNTP server");
    }

    #[test]
    fn compressed_group_selection_starts_with_the_current_pipeline_budget() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind grouped DEFLATE server");
        let port = listener
            .local_addr()
            .expect("get grouped DEFLATE address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener.accept().expect("accept grouped DEFLATE client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound grouped DEFLATE reads");
            stream
                .set_write_timeout(Some(Duration::from_secs(1)))
                .expect("bound grouped DEFLATE writes");
            let mut reader = BufReader::new(NntpStream::Plain(stream).enable_deflate());
            assert_eq!(next_compressed_command(&mut reader), "GROUP alt.test\r\n");
            reader
                .get_mut()
                .write_all(b"211 group selected\r\n")
                .expect("write compressed GROUP response");
            assert_eq!(
                next_compressed_command(&mut reader),
                "BODY <grouped@example.test>\r\n"
            );
            reader
                .get_mut()
                .write_all(
                    b"222 body follows\r\n=ybegin line=128 size=1 name=test\r\nk\r\n=yend size=1 crc32=d3d99e8b\r\n.\r\n",
                )
                .expect("write grouped compressed body");
        });
        let stream = std::net::TcpStream::connect(("127.0.0.1", port))
            .expect("connect grouped DEFLATE client");
        stream
            .set_read_timeout(Some(CANCELLATION_POLL))
            .expect("bound grouped DEFLATE client reads");
        stream
            .set_write_timeout(Some(CANCELLATION_POLL))
            .expect("bound grouped DEFLATE client writes");
        let mut connection = NntpConnection {
            reader: BufReader::new(NntpStream::Plain(stream).enable_deflate()),
            selected_group: None,
            pool_epoch: 0,
        };
        let NntpStream::Deflate(deflate) = connection.reader.get_mut() else {
            panic!("expected compressed client stream");
        };
        deflate.compressed_read = 1;
        deflate.compressed_read_limit = Some(1);

        let pools = PoolRegistry::new(1).expect("create grouped DEFLATE pool");
        let request = pool_request(port, "grouped@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference grouped DEFLATE pool");
        let routing = Arc::clone(&pool_reference.inner.group_routing);
        let cancellation = FlightCancellation::new();
        let (result_sender, result_receiver) = mpsc::sync_channel(1);
        let task = PipelineTask {
            request,
            group: Some("alt.test".into()),
            maximum_wire_bytes: 1024,
            maximum_decoded_bytes: 1024,
            cancellation,
            scheduling: SchedulingContext {
                work_class: WorkClass::Interactive,
                configuration_partition: [0; 32],
                session: "grouped-deflate".into(),
                declared_encoded_bytes: 1024,
            },
            _reservation: None,
            _pool_reference: pool_reference,
            result: result_sender,
        };
        let mut group_dispatch = GroupDispatch {
            routing,
            requirement: GroupRequirement::Required,
            probing: false,
        };
        let mut tasks = [task];

        assert!(connection.pipeline(&mut tasks, &mut group_dispatch));
        assert_eq!(
            result_receiver
                .recv()
                .expect("receive grouped DEFLATE result")
                .expect("decode grouped DEFLATE result")
                .bytes,
            b"A"
        );
        server.join().expect("join grouped DEFLATE server");
    }

    #[test]
    fn failed_compression_negotiation_poisons_the_opened_connection() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind compression failure server");
        let port = listener
            .local_addr()
            .expect("get compression failure address")
            .port();
        let server = thread::spawn(move || {
            let (stream, _) = listener
                .accept()
                .expect("accept compression failure client");
            let mut reader = BufReader::new(stream);
            respond(&mut reader, b"200 compression failure server ready\r\n");
            assert_eq!(next_command(&mut reader), "CAPABILITIES\r\n");
            respond(
                &mut reader,
                b"101 capabilities follow\r\nVERSION 2\r\nCOMPRESS DEFLATE\r\n.\r\n",
            );
            assert_eq!(next_command(&mut reader), "MODE READER\r\n");
            respond(&mut reader, b"200 reader enabled\r\n");
            assert_eq!(next_command(&mut reader), "CAPABILITIES\r\n");
            respond(
                &mut reader,
                b"101 capabilities follow\r\nVERSION 2\r\nCOMPRESS DEFLATE\r\n.\r\n",
            );
            assert_eq!(next_command(&mut reader), "COMPRESS DEFLATE\r\n");
            respond(&mut reader, b"403 compression unavailable\r\n");
        });
        let pools = Arc::new(PoolRegistry::new(1).expect("create compression failure pool"));
        let request = pool_request(port, "article@example.test");
        let pool_reference = pools
            .reference(&request, 1, 1)
            .expect("reference compression failure pool");

        assert_eq!(
            pools
                .verified_part(
                    pool_reference,
                    request,
                    None,
                    1024,
                    FlightCancellation::new(),
                )
                .expect_err("reject failed compression negotiation"),
            "nntp_compression_failed"
        );
        let stats = pools.stats();
        assert_eq!(stats.open, 0);
        assert_eq!(stats.idle, 0);
        assert_eq!(stats.poisoned, 1);
        server.join().expect("join compression failure server");
    }

    #[test]
    fn starttls_requires_an_advertised_capability_before_upgrade() {
        let port = scripted_server(|reader| {
            capabilities(reader);
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "starttls".into(),
            allow_private: true,
            username: None,
            password: None,
            message_id: "article@example.test".into(),
        };

        assert_eq!(
            NntpConnection::open(&request, &|| false).err(),
            Some("nntp_starttls_unavailable")
        );
    }

    #[test]
    fn refreshes_capabilities_after_authentication() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            assert_eq!(next_command(reader), "AUTHINFO USER user\r\n");
            respond(reader, b"381 password required\r\n");
            assert_eq!(next_command(reader), "AUTHINFO PASS secret\r\n");
            respond(reader, b"281 authentication accepted\r\n");
            capabilities(reader);
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: Some("user".into()),
            password: Some("secret".into()),
            message_id: "article@example.test".into(),
        };

        NntpConnection::open(&request, &|| false)
            .expect("refresh capabilities after authentication");
    }

    #[test]
    fn accepted_username_authentication_does_not_send_the_password() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            assert_eq!(next_command(reader), "AUTHINFO USER user\r\n");
            respond(reader, b"281 authentication accepted\r\n");
            capabilities(reader);
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: Some("user".into()),
            password: Some("unused-secret".into()),
            message_id: "article@example.test".into(),
        };

        NntpConnection::open(&request, &|| false).expect("accept authentication completed by USER");
    }

    #[test]
    fn authenticates_when_reader_mode_requires_credentials() {
        let port = scripted_server(|reader| {
            capabilities(reader);
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"480 authentication required\r\n");
            assert_eq!(next_command(reader), "AUTHINFO USER user\r\n");
            respond(reader, b"381 password required\r\n");
            assert_eq!(next_command(reader), "AUTHINFO PASS secret\r\n");
            respond(reader, b"281 authentication accepted\r\n");
            assert_eq!(next_command(reader), "MODE READER\r\n");
            respond(reader, b"200 reader enabled\r\n");
            capabilities(reader);
        });
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port,
            tls_mode: "plaintext".into(),
            allow_private: true,
            username: Some("user".into()),
            password: Some("secret".into()),
            message_id: "article@example.test".into(),
        };

        NntpConnection::open(&request, &|| false)
            .expect("authenticate before retrying reader mode");
    }

    #[test]
    fn rejects_private_addresses_for_untrusted_server_sources() {
        let request = BodyRequest {
            host: "127.0.0.1".into(),
            port: 119,
            tls_mode: "plaintext".into(),
            allow_private: false,
            username: None,
            password: None,
            message_id: "article@example.test".into(),
        };
        assert_eq!(
            NntpConnection::open(&request, &|| false).err(),
            Some("nntp_address_denied")
        );
    }

    #[test]
    fn retries_later_resolved_addresses_after_a_connect_failure() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fallback listener");
        let address = listener
            .local_addr()
            .expect("get fallback listener address");
        let rejected = std::net::SocketAddr::from(([127, 0, 0, 2], address.port()));
        let accept = thread::spawn(move || listener.accept().expect("accept fallback connection"));

        let stream = connect_addresses(&[rejected, address]).expect("connect to fallback address");

        assert_eq!(stream.peer_addr().expect("get peer address"), address);
        accept.join().expect("join fallback listener");
    }

    #[test]
    fn bounded_resolver_workers_return_hostname_addresses() {
        let addresses =
            resolve_cancellable("localhost", 8119, &|| false).expect("resolve localhost");

        assert!(!addresses.is_empty());
        assert!(addresses.len() <= super::MAX_RESOLVED_ADDRESSES);
        assert!(addresses.iter().all(|address| address.port() == 8119));
        assert!(addresses.iter().all(|address| address.ip().is_loopback()));
    }

    #[test]
    fn resolver_queue_is_bounded_and_reclaims_abandoned_jobs() {
        let resolver = super::Resolver {
            queue: std::sync::Mutex::new(std::collections::VecDeque::new()),
            changed: std::sync::Condvar::new(),
        };
        let deadline = Instant::now() + Duration::from_secs(1);
        let mut abandonments = Vec::new();
        let mut receivers = Vec::new();
        for index in 0..super::MAX_DNS_QUEUE {
            let (result, receiver) = mpsc::sync_channel(1);
            let abandoned = Arc::new(std::sync::atomic::AtomicBool::new(false));
            resolver
                .submit(super::ResolverJob {
                    host: format!("queued-{index}.example.test"),
                    port: 119,
                    deadline,
                    abandoned: Arc::clone(&abandoned),
                    result,
                })
                .expect("enqueue bounded DNS job");
            abandonments.push(abandoned);
            receivers.push(receiver);
        }
        let (overflow, _overflow_receiver) = mpsc::sync_channel(1);
        assert_eq!(
            resolver.submit(super::ResolverJob {
                host: "overflow.example.test".into(),
                port: 119,
                deadline,
                abandoned: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                result: overflow,
            }),
            Err("nntp_dns_busy")
        );

        abandonments[0].store(true, Ordering::Release);
        let (replacement, replacement_receiver) = mpsc::sync_channel(1);
        assert!(
            resolver
                .submit(super::ResolverJob {
                    host: "replacement.example.test".into(),
                    port: 119,
                    deadline,
                    abandoned: Arc::new(std::sync::atomic::AtomicBool::new(false)),
                    result: replacement,
                })
                .is_ok()
        );
        receivers.push(replacement_receiver);
    }

    #[test]
    fn pending_dns_wait_observes_cancellation_before_its_deadline() {
        let (_withheld, receiver) = mpsc::sync_channel(1);
        let cancellation = FlightCancellation::new();
        let trigger = cancellation.clone();
        let canceller = thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            trigger.cancel();
        });
        let started = Instant::now();

        assert_eq!(
            wait_for_resolution(
                &receiver,
                &|| cancellation.is_cancelled(),
                started + Duration::from_secs(1),
            ),
            Err("nntp_cancelled")
        );
        assert!(started.elapsed() < Duration::from_millis(500));
        canceller.join().expect("join DNS canceller");
    }

    #[test]
    fn connects_to_an_ipv6_address_with_the_polled_dialer() {
        let listener = TcpListener::bind("[::1]:0").expect("bind IPv6 fallback listener");
        let address = listener
            .local_addr()
            .expect("get IPv6 fallback listener address");
        let accept =
            thread::spawn(move || listener.accept().expect("accept IPv6 fallback connection"));

        let stream = connect_addresses(&[address]).expect("connect to IPv6 fallback address");

        assert_eq!(stream.peer_addr().expect("get IPv6 peer address"), address);
        accept.join().expect("join IPv6 fallback listener");
    }

    #[test]
    fn pending_connect_poll_observes_cancellation_before_retry() {
        let polls = Cell::new(0);
        let result = wait_for_connect_with(
            &|| polls.get() != 0,
            Instant::now() + Duration::from_secs(1),
            |timeout| {
                assert!(timeout <= Duration::from_millis(100));
                polls.set(polls.get() + 1);
                Ok(None)
            },
        );

        assert_eq!(result, Err("nntp_cancelled"));
        assert_eq!(polls.get(), 1);
    }

    #[test]
    fn pending_tls_handshake_observes_cancellation_before_retry() {
        let polls = Cell::new(0);
        let result = complete_tls_with(
            &|| polls.get() != 0,
            Instant::now() + Duration::from_secs(1),
            || {
                polls.set(polls.get() + 1);
                Ok(false)
            },
        );

        assert_eq!(result, Err("nntp_cancelled"));
        assert_eq!(polls.get(), 1);
    }

    #[test]
    fn cancellation_interrupts_an_actual_stalled_tls_handshake() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind stalled TLS listener");
        let port = listener
            .local_addr()
            .expect("get stalled TLS listener address")
            .port();
        let (accepted, connected) = mpsc::sync_channel(1);
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept stalled TLS client");
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .expect("bound stalled TLS close");
            let mut client_hello = [0_u8; 1024];
            assert!(
                stream
                    .read(&mut client_hello)
                    .expect("read stalled TLS ClientHello")
                    > 0
            );
            accepted.send(()).expect("signal stalled TLS accept");
            let mut bytes = Vec::new();
            stream
                .read_to_end(&mut bytes)
                .expect("observe stalled TLS close");
        });
        let cancellation = FlightCancellation::new();
        let caller_cancellation = cancellation.clone();
        let caller = thread::spawn(move || {
            let mut request = pool_request(port, "stalled-tls@example.test");
            request.tls_mode = "implicit".into();
            body_with_cancellation(request, 1024, &|| caller_cancellation.is_cancelled())
        });
        connected.recv().expect("wait for stalled TLS connection");
        let cancelled_at = Instant::now();
        cancellation.cancel();

        assert_eq!(
            caller.join().expect("join stalled TLS caller"),
            Err("nntp_cancelled")
        );
        assert!(cancelled_at.elapsed() < Duration::from_secs(1));
        server.join().expect("join stalled TLS server");
    }

    #[test]
    fn cancellation_interrupts_an_actual_stalled_socket_write() {
        for compressed in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind stalled write listener");
            let address = listener
                .local_addr()
                .expect("get stalled write listener address");
            let (accepted, connected) = mpsc::sync_channel(1);
            let (release, released) = mpsc::sync_channel(1);
            let server = thread::spawn(move || {
                let (_stream, _) = listener.accept().expect("accept stalled write client");
                accepted.send(()).expect("signal stalled write accept");
                released.recv().expect("hold stalled write socket");
            });
            let stream = connect_addresses(&[address]).expect("connect stalled write client");
            stream
                .set_write_timeout(Some(CANCELLATION_POLL))
                .expect("bound stalled socket write");
            let stream = NntpStream::Plain(stream);
            let mut stream = if compressed {
                stream.enable_deflate()
            } else {
                stream
            };
            connected.recv().expect("wait for stalled write connection");
            let mut state = 0x9e37_79b9_u32;
            let payload = (0..16 * 1024 * 1024)
                .map(|_| {
                    state ^= state << 13;
                    state ^= state >> 17;
                    state ^= state << 5;
                    state as u8
                })
                .collect::<Vec<_>>();
            let cancellation = FlightCancellation::new();
            let caller_cancellation = cancellation.clone();
            let cancel = thread::spawn(move || {
                thread::sleep(Duration::from_millis(50));
                caller_cancellation.cancel();
            });
            let started = Instant::now();

            assert_eq!(
                stream.write_all_cancellable(&payload, &|| cancellation.is_cancelled(), TIMEOUT,),
                Err("nntp_cancelled")
            );
            assert!(started.elapsed() < Duration::from_secs(1));
            cancel.join().expect("join stalled write cancellation");
            drop(stream);
            release.send(()).expect("release stalled write server");
            server.join().expect("join stalled write server");
        }
    }
}
