import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from enum import Enum

from comet.core.operator_settings import (
    BootstrapSettings,
    _GeneratedSecretInputs,
    _prepare_effective_payload,
    consume_runtime_restart_request,
    fresh_runtime_environment,
    prepare_effective_settings_environment,
)
from comet.observability.logging import (
    bootstrap_failure,
    configuration_invalid,
    configure_entrypoint,
    current_settings,
    set_engine_generation,
)

if __name__ == "__main__":
    try:
        prepare_effective_settings_environment()
    except Exception:
        bootstrap_failure()
        raise SystemExit(78) from None
configure_entrypoint(process_role="supervisor")

try:
    from comet.core.models import (
        AppSettings,
        database,
        finalize_app_settings,
        settings,
    )
except Exception as exc:
    configuration_invalid(exception=exc)
    raise SystemExit(78) from None

from comet.observability import log
from comet.observability.events import start_event_persistence, stop_event_persistence
from comet.observability.logging import ingest_native_event
from comet.observability.metrics import (
    increment_usenet_engine_restarts,
    prepare_multiprocess_directory,
)
from comet.observability.startup import log_runtime_starting
from comet.usenet.engine_client import EngineClient
from comet.usenet.engine_transport import EngineUnavailable
from comet.usenet.supervisor import EngineSupervisor

_ENGINE_STABLE_SECONDS = 30.0
_MAX_ENGINE_RESTART_DELAY_SECONDS = 5.0
_KILL_REAP_TIMEOUT_SECONDS = 5.0
_ENGINE_REMINDER_SECONDS = 900.0


class StopResult(str, Enum):
    ALREADY_STOPPED = "already_stopped"
    TERMINATED = "terminated"
    KILLED = "killed"
    KILL_TIMEOUT = "kill_timeout"


def main():
    prepare_multiprocess_directory(settings.PROMETHEUS_MULTIPROC_DIR)

    if settings.USENET_ENABLED:
        os.environ["COMET_RUNTIME_SUPERVISOR_PID"] = str(os.getpid())
        start_event_persistence(str(database.url))
        try:
            started_at = time.monotonic_ns()
            log_runtime_starting(settings)
            try:
                _run_supervisor()
            except Exception as exc:
                log.critical(
                    "runtime.failed",
                    "Comet runtime failed",
                    error_code="supervisor_failure",
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                    exc=exc,
                )
                raise SystemExit(1) from None
            log.terminal(
                "runtime.stopped",
                "Comet runtime stopped",
                outcome="ok",
                duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            )
        finally:
            stop_event_persistence()
        if consume_runtime_restart_request():
            os.execvpe(
                sys.executable,
                [sys.executable, "-m", "comet.main"],
                fresh_runtime_environment(),
            )
        return

    os.execvpe(
        sys.executable,
        [sys.executable, "-m", "comet.web"],
        os.environ.copy(),
    )


def _run_supervisor():
    """Keep the replica-local engine and its web master as sibling children."""
    runtime = _build_engine_supervisor()
    runtime.prepare_runtime_dir()
    engine = None
    web = None
    shutdown_signal = None
    engine_reload_requested = False
    previous_handlers = {}

    def request_shutdown(signum, _frame):
        nonlocal shutdown_signal
        shutdown_signal = signum

    def request_engine_reload(_signum, _frame):
        nonlocal engine_reload_requested
        engine_reload_requested = True

    def consume_engine_reload() -> bool:
        nonlocal engine_reload_requested
        requested = engine_reload_requested
        engine_reload_requested = False
        return requested

    try:
        environment = _web_environment()
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        previous_handlers[signal.SIGUSR1] = signal.signal(
            signal.SIGUSR1,
            request_engine_reload,
        )
        engine_starting_at = time.monotonic_ns()
        engine = _spawn_engine(runtime)
        asyncio.run(
            runtime.publish_descriptor(timeout=settings.USENET_START_TIMEOUT_SECONDS)
        )
        log.info(
            "native_engine.ready",
            "Native Usenet engine is ready",
            duration_ms=(time.monotonic_ns() - engine_starting_at) / 1_000_000,
        )
        if shutdown_signal is not None:
            return
        web = subprocess.Popen(
            [sys.executable, "-m", "comet.web"],
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
        returncode, engine, runtime = _supervise_engine(
            runtime,
            engine,
            web,
            shutdown_requested=lambda: shutdown_signal is not None,
            engine_reload_requested=consume_engine_reload,
        )
        if returncode:
            raise SystemExit(returncode)
    finally:
        try:
            _shutdown_children(
                runtime,
                web,
                engine,
                signal_number=shutdown_signal or signal.SIGTERM,
            )
        finally:
            try:
                runtime.close()
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)


def _build_engine_supervisor() -> EngineSupervisor:
    logging_settings = current_settings()
    return EngineSupervisor(
        settings.USENET_RUNTIME_DIR,
        settings.USENET_LOCAL_DATA_DIR,
        settings.COMET_USENET_ENGINE_BINARY,
        artifact_dir=settings.USENET_ARTIFACT_DIR,
        memory_cache_bytes=settings.USENET_MEMORY_CACHE_BYTES,
        disk_cache_bytes=settings.USENET_DISK_CACHE_BYTES,
        minimum_free_disk_bytes=settings.USENET_MIN_FREE_DISK_BYTES,
        maximum_nntp_connections=(
            settings.USENET_NATIVE_MAX_STREAMS // settings.USENET_REPLICA_COUNT
        ),
        maximum_spool_bytes=settings.USENET_SPOOL_MAX_BYTES,
        maximum_archive_jobs=settings.USENET_ARCHIVE_JOBS,
        maximum_repair_jobs=settings.USENET_REPAIR_JOBS,
        parser_only=not settings.USENET_ENGINE_ENABLED,
        log_profile=logging_settings.LOG_PROFILE.value,
        log_format=logging_settings.LOG_FORMAT.value,
        no_color=logging_settings.no_color,
    )


def _reload_supervisor_settings() -> bool:
    payload = asyncio.run(
        _prepare_effective_payload(
            BootstrapSettings(),
            deployment_values=_GeneratedSecretInputs(),
        )
    )
    current = settings.active_snapshot()
    if payload.revision == current.APPLIED_SETTINGS_REVISION:
        return False
    desired = AppSettings(**payload.values)
    values = desired.model_dump()
    from comet.core.settings_policy import settings_requiring_restart

    changed = (
        key
        for key in AppSettings.model_fields
        if key.isupper() and getattr(current, key) != getattr(desired, key)
    )
    for key in settings_requiring_restart(changed):
        values[key] = getattr(current, key)
    candidate = finalize_app_settings(
        AppSettings(_env_file=None, **values),
        generated_keys=payload.generated_keys,
        revision=payload.revision,
    )
    settings.publish(candidate)
    return True


def _spawn_engine(runtime: EngineSupervisor):
    generation = runtime.next_engine_generation()
    set_engine_generation(generation)
    child = subprocess.Popen(
        runtime.engine_command(),
        env=runtime.engine_environment(),
        close_fds=True,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    reader = threading.Thread(
        target=_forward_native_events,
        args=(child.stderr,),
        name=f"native-events-{child.pid}",
        daemon=True,
    )
    reader.start()
    log.verbose(
        "native_engine.spawned",
        "Native Usenet engine was spawned",
    )
    return child


def _forward_native_events(stream) -> None:
    if stream is None:
        return
    try:
        for line in stream:
            ingest_native_event(line)
    finally:
        stream.close()


def _web_environment() -> dict[str, str]:
    environment = os.environ.copy()
    workers = settings.FASTAPI_WORKERS
    if workers == 0:
        workers = min((os.cpu_count() or 1) * 2 + 1, 12)
    environment["COMET_RESOLVED_FASTAPI_WORKERS"] = str(workers)
    return environment


def _supervise_engine(
    runtime: EngineSupervisor,
    engine,
    web,
    *,
    shutdown_requested=lambda: False,
    engine_reload_requested=lambda: False,
) -> tuple[int, object, EngineSupervisor]:
    """Restart only the private runtime while the web master remains healthy."""
    restart_delay = 0.25
    engine_started_at = time.monotonic()
    web_started_at = engine_started_at
    degraded_since: float | None = None
    last_reminder_at = engine_started_at
    attempts = 0
    suppressed_count = 0
    published_generation = runtime.engine_generation
    while not shutdown_requested() and web.poll() is None:
        if engine_reload_requested():
            if not _reload_supervisor_settings():
                continue
            _drain_and_stop_engine(runtime, engine)
            runtime.close()
            runtime = _build_engine_supervisor()
            runtime.prepare_runtime_dir()
            engine = _spawn_engine(runtime)
            engine_started_at = time.monotonic()
            asyncio.run(
                runtime.publish_descriptor(
                    timeout=settings.USENET_START_TIMEOUT_SECONDS
                )
            )
            published_generation = runtime.engine_generation
            continue
        engine_returncode = engine.poll()
        now = time.monotonic()
        if engine_returncode is None:
            if (
                degraded_since is not None
                and published_generation == runtime.engine_generation
                and now - engine_started_at >= _ENGINE_STABLE_SECONDS
            ):
                log.info(
                    "native_engine.recovered",
                    "Native Usenet engine recovered",
                    downtime_ms=(now - degraded_since) * 1000,
                    attempt_count=attempts,
                    suppressed_count=suppressed_count,
                )
                degraded_since = None
                attempts = 0
                suppressed_count = 0
                restart_delay = 0.25
            time.sleep(0.1)
            continue
        uptime_ms = (now - engine_started_at) * 1000
        set_engine_generation(runtime.engine_generation)
        if degraded_since is None:
            degraded_since = now
            last_reminder_at = now
            if engine_returncode < 0:
                log.warning(
                    "native_engine.exited",
                    "Native Usenet engine exited unexpectedly",
                    signal=-engine_returncode,
                    uptime_ms=uptime_ms,
                    error_code="unexpected_exit",
                )
            else:
                log.warning(
                    "native_engine.exited",
                    "Native Usenet engine exited unexpectedly",
                    exit_code=engine_returncode,
                    uptime_ms=uptime_ms,
                    error_code="unexpected_exit",
                )
        else:
            suppressed_count += 1
        runtime.withdraw_descriptor()
        _stop_child(engine)
        attempts += 1
        increment_usenet_engine_restarts(enabled=settings.PROMETHEUS_ENABLED)
        if attempts == 1 or now - last_reminder_at >= _ENGINE_REMINDER_SECONDS:
            log.warning(
                "native_engine.restart.scheduled",
                "Native Usenet engine restart scheduled",
                backoff_ms=restart_delay * 1000,
                attempt_count=attempts,
                suppressed_count=suppressed_count,
                error_code="engine_unavailable",
            )
            last_reminder_at = now
            suppressed_count = 0
        time.sleep(restart_delay)
        if shutdown_requested() or web.poll() is not None:
            break
        engine = _spawn_engine(runtime)
        engine_started_at = time.monotonic()
        published_generation = None
        try:
            asyncio.run(
                runtime.publish_descriptor(
                    timeout=settings.USENET_START_TIMEOUT_SECONDS
                )
            )
        except EngineUnavailable:
            _stop_child(engine)
            restart_delay = min(
                restart_delay * 2,
                _MAX_ENGINE_RESTART_DELAY_SECONDS,
            )
            continue
        except BaseException:
            _stop_child(engine)
            raise
        published_generation = runtime.engine_generation
        restart_delay = min(
            restart_delay * 2,
            _MAX_ENGINE_RESTART_DELAY_SECONDS,
        )
    if shutdown_requested():
        return 0, engine, runtime
    web_returncode = web.returncode
    if web_returncode < 0:
        log.terminal(
            "web_master.exited",
            "Web master exited unexpectedly",
            outcome="failed",
            signal=-web_returncode,
            uptime_ms=(time.monotonic() - web_started_at) * 1000,
            error_code="unexpected_exit",
        )
    else:
        log.terminal(
            "web_master.exited",
            "Web master exited unexpectedly",
            outcome="failed",
            exit_code=web_returncode,
            uptime_ms=(time.monotonic() - web_started_at) * 1000,
            error_code="unexpected_exit",
        )
    return web_returncode or 1, engine, runtime


def _drain_and_stop_engine(runtime: EngineSupervisor, engine) -> StopResult:
    if engine is None or engine.poll() is not None:
        return StopResult.ALREADY_STOPPED
    started_at = time.monotonic()
    try:
        asyncio.run(EngineClient(runtime.descriptor_path).drain())
        engine.wait(timeout=settings.USENET_DRAIN_TIMEOUT_SECONDS)
        log.verbose(
            "native_engine.stopped",
            "Native Usenet engine stopped",
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
        return StopResult.TERMINATED
    except EngineUnavailable:
        log.warning(
            "native_engine.drain.failed",
            "Native Usenet engine drain failed",
            error_code="engine_unavailable",
        )
    except subprocess.TimeoutExpired:
        log.warning(
            "native_engine.drain.failed",
            "Native Usenet engine drain failed",
            error_code="drain_timeout",
            timeout_ms=settings.USENET_DRAIN_TIMEOUT_SECONDS * 1000,
        )
    result = _stop_child(engine)
    if result == StopResult.KILL_TIMEOUT:
        log.critical(
            "native_engine.stop.failed",
            "Native Usenet engine could not be reaped",
            error_code="kill_timeout",
            timeout_ms=_KILL_REAP_TIMEOUT_SECONDS * 1000,
        )
    return result


def _shutdown_children(
    runtime: EngineSupervisor,
    web,
    engine,
    *,
    signal_number=signal.SIGTERM,
) -> None:
    """Drain public HTTP first, then stop native admission and reap the engine."""
    _stop_child(web, signal_number=signal_number)
    _drain_and_stop_engine(runtime, engine)


def _stop_child(child, *, signal_number=signal.SIGTERM) -> StopResult:
    if child is None or child.poll() is not None:
        return StopResult.ALREADY_STOPPED
    try:
        os.killpg(child.pid, signal_number)
    except ProcessLookupError:
        try:
            child.wait(timeout=0)
        except ChildProcessError:
            pass
        return StopResult.ALREADY_STOPPED
    try:
        child.wait(timeout=settings.USENET_DRAIN_TIMEOUT_SECONDS)
        return StopResult.TERMINATED
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=_KILL_REAP_TIMEOUT_SECONDS)
        except ChildProcessError:
            return StopResult.KILLED
        except subprocess.TimeoutExpired:
            return StopResult.KILL_TIMEOUT
        return StopResult.KILLED


if __name__ == "__main__":
    main()
