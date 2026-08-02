"""The ASGI web-master launcher; it never owns the native Usenet runtime."""

from __future__ import annotations

import logging
import os
import sys
import time

from comet.core.operator_settings import (
    consume_runtime_restart_request,
    fresh_runtime_environment,
    prepare_effective_settings_environment,
)
from comet.observability.logging import (
    bootstrap_failure,
    configuration_invalid,
    configure_entrypoint,
    configure_stdlib_bridge,
    install_safe_hooks,
    set_process_role,
)
from comet.observability.stderr import install_stderr_proxy

if __name__ == "__main__":
    try:
        prepare_effective_settings_environment()
    except Exception:
        bootstrap_failure()
        raise SystemExit(78) from None
configure_entrypoint(process_role="web_master")
os.environ["COMET_WEB_MASTER_PID"] = str(os.getpid())

try:
    import uvicorn

    from comet.core.models import database, settings
except Exception as exc:
    configuration_invalid(exception=exc)
    raise SystemExit(78) from None

from comet.core.server_settings import MAX_FASTAPI_WORKERS
from comet.observability import clear_context, log
from comet.observability.events import start_event_persistence, stop_event_persistence
from comet.observability.metrics import mark_process_dead
from comet.observability.readiness import readiness_tracker
from comet.observability.startup import (
    log_runtime_starting,
    log_startup_configuration,
)


def resolved_workers() -> int:
    value = os.environ.get("COMET_RESOLVED_FASTAPI_WORKERS")
    if value is None:
        value = settings.FASTAPI_WORKERS
    try:
        workers = int(value)
    except (TypeError, ValueError):
        raise RuntimeError("FASTAPI_WORKERS must resolve to an integer") from None
    if workers < 1:
        return min((os.cpu_count() or 1) * 2 + 1, 12)
    if workers > MAX_FASTAPI_WORKERS:
        raise RuntimeError(f"FASTAPI_WORKERS cannot exceed {MAX_FASTAPI_WORKERS}")
    return workers


def run_with_uvicorn(workers: int) -> None:
    configure_stdlib_bridge()
    single_process = workers == 1
    marker = os.environ.get("COMET_WEB_MASTER_PID")
    if single_process:
        set_process_role("web_worker")
        os.environ["COMET_WEB_MASTER_PID"] = "0"
    try:
        uvicorn.run(
            "comet.api.app:app",
            host=settings.FASTAPI_HOST,
            port=settings.FASTAPI_PORT,
            proxy_headers=True,
            forwarded_allow_ips="*",
            workers=workers,
            log_config=None,
            access_log=False,
        )
    finally:
        if single_process:
            set_process_role("web_master")
            if marker is None:
                os.environ.pop("COMET_WEB_MASTER_PID", None)
            else:
                os.environ["COMET_WEB_MASTER_PID"] = marker


def _post_fork(_server, _worker) -> None:
    install_stderr_proxy()
    set_process_role("web_worker")
    clear_context()
    readiness_tracker.reset()
    install_safe_hooks()
    start_event_persistence(str(database.url))


def _child_exit(_server, worker) -> None:
    try:
        mark_process_dead(worker.pid, settings.PROMETHEUS_MULTIPROC_DIR)
    except Exception as exc:
        log.warning(
            "metrics.worker.degraded",
            "Worker metrics cleanup failed",
            error_code="cleanup_failure",
            exc=exc,
        )


def run_with_gunicorn(workers: int) -> None:
    # Gunicorn remains a lazy import so logging and stderr policy are installed
    # before its module-level setup.
    install_stderr_proxy()
    import gunicorn.app.base
    import gunicorn.glogging

    class SafeGunicornLogger(gunicorn.glogging.Logger):
        def setup(self, cfg):
            self.loglevel = self.LOG_LEVELS[cfg.loglevel.lower()]
            configure_stdlib_bridge()
            self.error_log = logging.getLogger("gunicorn.error")
            self.access_log = logging.getLogger("gunicorn.access")
            self.access_log.disabled = True
            self.error_handlers = list(self.error_log.handlers)
            self.access_handlers = []

    class StandaloneApplication(gunicorn.app.base.BaseApplication):
        def __init__(self, options):
            self.options = options
            super().__init__()

        def load_config(self):
            for key, value in self.options.items():
                self.cfg.set(key, value)

        def load(self):
            from comet.api.app import app

            return app

    StandaloneApplication(
        {
            "bind": f"{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}",
            "workers": workers,
            "worker_class": "uvicorn_worker.UvicornWorker",
            "timeout": 120,
            "keepalive": 5,
            "preload_app": settings.GUNICORN_PRELOAD_APP,
            "proxy_protocol": True,
            "forwarded_allow_ips": "*",
            "loglevel": "warning",
            "accesslog": None,
            "errorlog": None,
            "logger_class": SafeGunicornLogger,
            "post_fork": _post_fork,
            "child_exit": _child_exit,
            "control_socket_disable": True,
        }
    ).run()


def main() -> None:
    workers = resolved_workers()
    started_at = time.monotonic_ns()
    owns_runtime = not settings.USENET_ENABLED
    use_gunicorn = os.name != "nt" and settings.USE_GUNICORN
    start_event_persistence(str(database.url))
    try:
        if owns_runtime:
            log_runtime_starting(settings)
        log_startup_configuration(
            settings,
            workers=workers,
            server_name="Gunicorn" if use_gunicorn else "Uvicorn",
        )
        try:
            if use_gunicorn:
                run_with_gunicorn(workers)
            else:
                run_with_uvicorn(workers)
        except KeyboardInterrupt:
            pass
        except SystemExit as exc:
            code = exc.code if type(exc.code) is int else 1
            if code and owns_runtime:
                log.critical(
                    "runtime.failed",
                    "Comet runtime failed",
                    error_code="web_master_failure",
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                )
            raise SystemExit(code) from None
        except Exception as exc:
            if owns_runtime:
                log.critical(
                    "runtime.failed",
                    "Comet runtime failed",
                    error_code="web_master_failure",
                    duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
                    exc=exc,
                )
            raise SystemExit(1) from None
        if owns_runtime:
            log.terminal(
                "runtime.stopped",
                "Comet runtime stopped",
                outcome="ok",
                duration_ms=(time.monotonic_ns() - started_at) / 1_000_000,
            )
    finally:
        stop_event_persistence()
    if owns_runtime and consume_runtime_restart_request():
        os.execvpe(
            sys.executable,
            [sys.executable, "-m", "comet.main"],
            fresh_runtime_environment(),
        )


if __name__ == "__main__":
    main()
