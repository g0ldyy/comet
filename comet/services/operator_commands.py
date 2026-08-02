from __future__ import annotations

import asyncio
import inspect
import os
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from comet.background_scraper.worker import background_scraper
from comet.core.models import database, settings
from comet.core.operator_settings import request_runtime_restart
from comet.core.runtime_registry import RuntimeIdentity, restart_target_pid
from comet.observability import create_detached_task, log
from comet.usenet.engine_client import EngineClient

ScraperCommand = Literal[
    "scraper.start",
    "scraper.stop",
    "scraper.pause",
    "scraper.resume",
    "scraper.drain",
    "scraper.cancel_drain",
]
UsenetCommand = Literal["usenet.drain", "usenet.resume"]
_COMMAND_TIMEOUT_SECONDS = 3
_POLL_INTERVAL_SECONDS = 0.1
_OWNER_STALE_SECONDS = 1
_COMMAND_RETENTION_SECONDS = 5 * 60


def _finish_scraper_task(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        background_scraper.last_error = type(error).__name__
    if background_scraper.task is task:
        background_scraper.task = None


def _start_scraper() -> bool:
    background_scraper.clear_finished_task()
    if background_scraper.task is not None:
        return True
    task = create_detached_task(
        background_scraper.start(),
        name="background.scraper",
    )
    task.add_done_callback(_finish_scraper_task)
    background_scraper.task = task
    return True


async def _stop_scraper() -> bool:
    await background_scraper.stop()
    return True


async def _pause_scraper() -> bool:
    return await background_scraper.pause()


async def _resume_scraper() -> bool:
    return await background_scraper.resume()


async def _drain_scraper() -> bool:
    if not background_scraper.is_running:
        return False
    await background_scraper.drain()
    return True


async def _cancel_scraper_drain() -> bool:
    return background_scraper.cancel_drain()


async def _control_usenet(action: Literal["drain", "resume"]) -> bool:
    engine = EngineClient(Path(settings.USENET_RUNTIME_DIR) / "engine.json")
    await getattr(engine, action)()
    return True


async def _apply_settings() -> bool:
    from comet.core.live_settings import apply_latest_settings

    await apply_latest_settings(database)
    return True


async def _terminate_web_master(process_id: int) -> None:
    await asyncio.sleep(1)
    request_runtime_restart()
    os.kill(
        process_id,
        signal.SIGINT if process_id == os.getpid() else signal.SIGTERM,
    )


def _restart_runtime() -> bool:
    process_id = restart_target_pid()
    if process_id is None:
        return False
    create_detached_task(
        _terminate_web_master(process_id),
        name="runtime.restart",
    )
    return True


_HANDLERS: dict[str, Callable[[], bool | Awaitable[bool]]] = {
    "scraper.start": _start_scraper,
    "scraper.stop": _stop_scraper,
    "scraper.pause": _pause_scraper,
    "scraper.resume": _resume_scraper,
    "scraper.drain": _drain_scraper,
    "scraper.cancel_drain": _cancel_scraper_drain,
    "usenet.drain": lambda: _control_usenet("drain"),
    "usenet.resume": lambda: _control_usenet("resume"),
    "runtime.restart": _restart_runtime,
    "settings.apply": _apply_settings,
}


async def _execute(
    handlers: dict[str, Callable[[], bool | Awaitable[bool]]],
    kind: str,
) -> bool:
    result = handlers[kind]()
    return await result if inspect.isawaitable(result) else result


async def _consume_pending_command(
    identity: RuntimeIdentity,
    process_id: int,
    handlers: dict[str, Callable[[], bool | Awaitable[bool]]],
) -> None:
    now = time.time()
    command = await database.fetch_one(
        """
        SELECT id
        FROM operator_commands
        WHERE target_instance_id = :instance_id
          AND target_process_id = :process_id
          AND status = 'pending'
          AND created_at >= :cutoff
        ORDER BY created_at, id
        LIMIT 1
        """,
        {
            "instance_id": identity.instance_id,
            "process_id": process_id,
            "cutoff": now - _COMMAND_TIMEOUT_SECONDS,
        },
        force_primary=True,
    )
    if command is None:
        return
    claimed = await database.fetch_one(
        """
        UPDATE operator_commands
        SET status = 'acknowledged', acknowledged_at = :acknowledged_at
        WHERE id = :id AND status = 'pending'
        RETURNING id, kind
        """,
        {"id": command["id"], "acknowledged_at": now},
        force_primary=True,
    )
    if claimed is None:
        return
    outcome = "succeeded"
    error_code = None
    try:
        if not await _execute(handlers, claimed["kind"]):
            outcome = "rejected"
            error_code = "invalid_runtime_state"
    except Exception as error:
        outcome = "failed"
        error_code = "command_failed"
        log.warning(
            "operator.command.failed",
            "Operational command failed",
            error_code="command_failed",
            exc=error,
        )
    await database.execute(
        """
        UPDATE operator_commands
        SET status = 'finished',
            finished_at = :finished_at,
            outcome = :outcome,
            error_code = :error_code
        WHERE id = :id
        """,
        {
            "id": claimed["id"],
            "finished_at": time.time(),
            "outcome": outcome,
            "error_code": error_code,
        },
    )


async def run_settings_command_dispatcher(
    handler: Callable[[], Awaitable[bool]],
) -> None:
    identity = RuntimeIdentity.current()
    process_id = os.getpid()
    handlers = {"settings.apply": handler}
    while True:
        await _consume_pending_command(identity, process_id, handlers)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def run_command_dispatcher() -> None:
    identity = RuntimeIdentity.current()
    process_id = os.getpid()
    next_cleanup = 0.0
    while True:
        now = time.time()
        state = (
            "paused"
            if background_scraper.is_paused
            else "running"
            if background_scraper.is_running
            else "stopped"
        )
        await database.execute(
            """
            INSERT INTO background_scraper_runtimes (
                instance_id, process_id, state, draining, run_id, started_at,
                processed, success, failed, torrents_found,
                discovered_items, errors, last_heartbeat
            ) VALUES (
                :instance_id, :process_id, :state, :draining, :run_id, :started_at,
                :processed, :success, :failed, :torrents_found,
                :discovered_items, :errors, :last_heartbeat
            )
            ON CONFLICT (instance_id, process_id) DO UPDATE SET
                state = excluded.state,
                draining = excluded.draining,
                run_id = excluded.run_id,
                started_at = excluded.started_at,
                processed = excluded.processed,
                success = excluded.success,
                failed = excluded.failed,
                torrents_found = excluded.torrents_found,
                discovered_items = excluded.discovered_items,
                errors = excluded.errors,
                last_heartbeat = excluded.last_heartbeat
            """,
            {
                "instance_id": identity.instance_id,
                "process_id": process_id,
                "state": state,
                "draining": int(background_scraper.drain_requested),
                "run_id": background_scraper.current_run_id,
                "started_at": background_scraper.stats.start_time or None,
                "processed": background_scraper.stats.total_processed,
                "success": background_scraper.stats.total_success,
                "failed": background_scraper.stats.total_failed,
                "torrents_found": background_scraper.stats.total_torrents_found,
                "discovered_items": background_scraper.stats.discovered_items,
                "errors": background_scraper.stats.errors,
                "last_heartbeat": now,
            },
        )
        await _consume_pending_command(identity, process_id, _HANDLERS)
        if now >= next_cleanup:
            await database.execute(
                """
                DELETE FROM operator_commands
                WHERE finished_at < :cutoff
                """,
                {"cutoff": now - _COMMAND_RETENTION_SECONDS},
            )
            await database.execute(
                """
                DELETE FROM background_scraper_runtimes
                WHERE last_heartbeat < :cutoff
                """,
                {"cutoff": now - _OWNER_STALE_SECONDS * 10},
            )
            next_cleanup = now + 60
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def withdraw_command_dispatcher() -> None:
    identity = RuntimeIdentity.current()
    await database.execute(
        """
        DELETE FROM background_scraper_runtimes
        WHERE instance_id = :instance_id AND process_id = :process_id
        """,
        {"instance_id": identity.instance_id, "process_id": os.getpid()},
    )


async def _dispatch_commands(
    kind: str,
    owners: list[dict],
    *,
    command_database=None,
) -> list[dict]:
    if not owners:
        return []
    command_database = command_database or database
    now = time.time()
    commands = [
        {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "target_instance_id": owner["instance_id"],
            "target_process_id": owner["process_id"],
            "created_at": now,
        }
        for owner in owners
    ]
    await command_database.execute_many(
        """
        INSERT INTO operator_commands (
            id, kind, target_instance_id, target_process_id, status, created_at
        ) VALUES (
            :id, :kind, :target_instance_id, :target_process_id, 'pending', :created_at
        )
        """,
        commands,
    )
    identifiers = [command["id"] for command in commands]
    deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        rows = await asyncio.gather(
            *(
                command_database.fetch_one(
                    """
                    SELECT id, outcome, error_code
                    FROM operator_commands
                    WHERE id = :id AND status = 'finished'
                    """,
                    {"id": identifier},
                    force_primary=True,
                )
                for identifier in identifiers
            )
        )
        if all(row is not None for row in rows):
            return [dict(row) for row in rows if row is not None]
        await asyncio.sleep(0.05)
    await command_database.execute_many(
        """
        UPDATE operator_commands
        SET status = 'finished',
            finished_at = :finished_at,
            outcome = 'expired',
            error_code = 'command_timeout'
        WHERE id = :id AND status = 'pending'
        """,
        [{"id": identifier, "finished_at": time.time()} for identifier in identifiers],
    )
    return []


async def dispatch_scraper_command(kind: ScraperCommand) -> list[dict]:
    owners = await database.fetch_all(
        """
        SELECT instance_id, process_id
        FROM background_scraper_runtimes
        WHERE last_heartbeat >= :cutoff
          AND state != 'stopped'
        ORDER BY instance_id, process_id
        """,
        {"cutoff": time.time() - _OWNER_STALE_SECONDS},
        force_primary=True,
    )
    if kind == "scraper.start" and not owners:
        identity = RuntimeIdentity.current()
        owners = [{"instance_id": identity.instance_id, "process_id": os.getpid()}]
    return await _dispatch_commands(kind, [dict(owner) for owner in owners])


async def dispatch_usenet_command(
    kind: UsenetCommand,
    *,
    instance_id: str,
    process_id: int,
) -> dict | None:
    results = await _dispatch_commands(
        kind,
        [{"instance_id": instance_id, "process_id": process_id}],
    )
    return results[0] if results else None


async def dispatch_runtime_restart(instance_id: str) -> dict | None:
    owner = await database.fetch_one(
        """
        SELECT process_id
        FROM runtime_processes
        WHERE instance_id = :instance_id
          AND role = 'web_worker'
          AND last_heartbeat >= :cutoff
        ORDER BY process_id
        LIMIT 1
        """,
        {
            "instance_id": instance_id,
            "cutoff": time.time() - settings.RUNTIME_STALE_SECONDS,
        },
        force_primary=True,
    )
    if owner is None:
        return None
    results = await _dispatch_commands(
        "runtime.restart",
        [{"instance_id": instance_id, "process_id": owner["process_id"]}],
    )
    return results[0] if results else None


async def dispatch_settings_apply(command_database=None) -> list[dict]:
    command_database = command_database or database
    owners = await command_database.fetch_all(
        """
        SELECT instance_id, process_id
        FROM runtime_processes
        WHERE role IN ('web_worker', 'cometnet')
          AND last_heartbeat >= :cutoff
        ORDER BY instance_id, process_id
        """,
        {"cutoff": time.time() - settings.RUNTIME_STALE_SECONDS},
        force_primary=True,
    )
    return await _dispatch_commands(
        "settings.apply",
        [dict(owner) for owner in owners],
        command_database=command_database,
    )
