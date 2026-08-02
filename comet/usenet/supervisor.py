"""Replica-local child-process supervision for the bundled Usenet engine."""

import asyncio
import fcntl
import json
import os
import secrets
import stat
import time
from pathlib import Path

from comet.usenet.engine_client import EngineClient
from comet.usenet.engine_transport import EngineUnavailable
from comet.usenet.limits import MAX_UNIX_SOCKET_PATH_BYTES


def _validate_socket_path(path: Path) -> None:
    try:
        encoded = os.fsencode(path)
    except UnicodeEncodeError:
        raise ValueError("Usenet engine socket path is invalid") from None
    if b"\0" in encoded or len(encoded) > MAX_UNIX_SOCKET_PATH_BYTES:
        raise ValueError("Usenet engine socket path is invalid")


def _write_private_descriptor(path: Path, payload: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    file_fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(file_fd, "wb", closefd=False) as descriptor:
            descriptor.write(payload)
            descriptor.flush()
            os.fsync(file_fd)
    finally:
        os.close(file_fd)


class EngineSupervisor:
    def __init__(
        self,
        runtime_dir: str,
        local_data_dir: str,
        engine_binary: str,
        *,
        artifact_dir: str | None = None,
        memory_cache_bytes: int = 268435456,
        disk_cache_bytes: int = 2147483648,
        minimum_free_disk_bytes: int = 5368709120,
        maximum_nntp_connections: int = 32,
        maximum_spool_bytes: int = 107374182400,
        maximum_archive_jobs: int = 2,
        maximum_repair_jobs: int = 1,
        par2_binary: str = "/app/bin/par2",
        libarchive_library: str = "/app/lib/libarchive.so.13",
        parser_only: bool = False,
        log_profile: str = "normal",
        log_format: str = "pretty",
        no_color: bool = False,
    ):
        self.runtime_dir = Path(runtime_dir).resolve()
        self.local_data_dir = Path(local_data_dir).resolve()
        self.artifact_dir = (
            Path(artifact_dir).resolve()
            if artifact_dir is not None
            else self.local_data_dir
        )
        self.engine_binary = engine_binary
        self.memory_cache_bytes = memory_cache_bytes
        self.disk_cache_bytes = disk_cache_bytes
        self.minimum_free_disk_bytes = minimum_free_disk_bytes
        self.maximum_nntp_connections = maximum_nntp_connections
        self.maximum_spool_bytes = maximum_spool_bytes
        self.maximum_archive_jobs = maximum_archive_jobs
        self.maximum_repair_jobs = maximum_repair_jobs
        self.par2_binary = par2_binary
        self.libarchive_library = libarchive_library
        self.parser_only = parser_only
        if log_profile not in {"quiet", "normal", "verbose", "debug"}:
            raise ValueError("invalid native logging profile")
        if log_format not in {"pretty", "json"}:
            raise ValueError("invalid native logging format")
        if type(no_color) is not bool:
            raise ValueError("invalid native color policy")
        self.log_profile = log_profile
        self.log_format = log_format
        self.no_color = no_color
        self.engine_generation = 0
        self.socket_path = self.runtime_dir / "engine.sock"
        self.descriptor_path = self.runtime_dir / "engine.json"
        self._lock_fd: int | None = None

    def prepare_runtime_dir(self) -> None:
        if self._lock_fd is not None:
            raise RuntimeError("the Usenet engine runtime is already prepared")
        _validate_socket_path(self.socket_path)
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime_dir, 0o700)
        self.local_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.local_data_dir, 0o700)
        lock_path = self.runtime_dir / "engine.lock"
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        self._lock_fd = os.open(lock_path, flags, 0o600)
        try:
            lock_stat = os.fstat(self._lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode):
                raise RuntimeError("Usenet engine runtime lock is invalid")
            os.fchmod(self._lock_fd, 0o600)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError(
                    "a Usenet engine supervisor already owns this runtime directory"
                ) from None
            self.descriptor_path.unlink(missing_ok=True)
        except BaseException:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise

    def withdraw_descriptor(self) -> None:
        """Stop advertising a runtime that is not health-checked and ready."""
        self.descriptor_path.unlink(missing_ok=True)

    def close(self) -> None:
        """Withdraw readiness and release this process's runtime ownership."""
        self.withdraw_descriptor()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def engine_command(self) -> list[str]:
        command = [
            self.engine_binary,
            "--socket",
            str(self.socket_path),
            "--local-data-dir",
            str(self.local_data_dir),
            "--artifact-dir",
            str(self.artifact_dir),
            "--memory-cache-bytes",
            str(self.memory_cache_bytes),
            "--disk-cache-bytes",
            str(self.disk_cache_bytes),
            "--minimum-free-disk-bytes",
            str(self.minimum_free_disk_bytes),
            "--maximum-nntp-connections",
            str(self.maximum_nntp_connections),
        ]
        if self.parser_only:
            command.append("--parser-only")
        else:
            command.extend(
                [
                    "--spool-max-bytes",
                    str(self.maximum_spool_bytes),
                    "--archive-jobs",
                    str(self.maximum_archive_jobs),
                    "--repair-jobs",
                    str(self.maximum_repair_jobs),
                    "--par2-binary",
                    self.par2_binary,
                    "--libarchive-library",
                    self.libarchive_library,
                ]
            )
        return command

    def next_engine_generation(self) -> int:
        self.engine_generation += 1
        return self.engine_generation

    def engine_environment(self) -> dict[str, str]:
        """Keep operator and user credentials out of the engine process."""
        if self.engine_generation <= 0:
            raise RuntimeError("native engine generation is not initialized")
        environment = {
            "PATH": os.environ.get(
                "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "RUST_BACKTRACE": "0",
            "COMET_SUPERVISOR_PID": str(os.getpid()),
            "LOG_PROFILE": self.log_profile,
            "LOG_FORMAT": "json",
            "COMET_ENGINE_GENERATION": str(self.engine_generation),
        }
        if self.no_color:
            environment["NO_COLOR"] = "1"
        return environment

    async def publish_descriptor(self, timeout: float = 30) -> None:
        # A descriptor must not become visible until the engine accepts the
        # versioned health handshake on its private socket.
        temporary = self.runtime_dir / f".engine-{secrets.token_hex(8)}.json"
        payload = {
            "version": 1,
            "socket_path": str(self.socket_path),
            "runtime_id": secrets.token_urlsafe(16),
            "api_version": 1,
        }
        document = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        published = False
        _write_private_descriptor(temporary, document)
        try:
            client = EngineClient(temporary)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    await client.health()
                    os.replace(temporary, self.descriptor_path)
                    directory_fd = os.open(
                        self.runtime_dir,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
                    )
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    published = True
                    return
                except EngineUnavailable:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.05)
        finally:
            if not published:
                temporary.unlink(missing_ok=True)
