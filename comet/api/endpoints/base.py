import asyncio
import os
import sqlite3
from pathlib import Path

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from comet.core.models import database, settings
from comet.core.schema_migrations import MIGRATIONS
from comet.observability.readiness import (
    ReadinessSnapshot,
    evaluate_readiness,
    readiness_tracker,
)
from comet.usenet.engine_client import EngineClient
from comet.usenet.engine_transport import EngineUnavailable

router = APIRouter()


@router.get(
    "/",
    tags=["General"],
    summary="Root Redirect",
    description="Redirects to the configuration page.",
)
async def root():
    return RedirectResponse("/configure")


@router.get(
    "/health",
    tags=["General"],
    summary="Health Check",
    description="Returns the health status of the application.",
)
async def health():
    return {"status": "ok"}


@router.get(
    "/ready",
    tags=["General"],
    summary="Readiness Check",
    description="Returns whether this worker can safely receive traffic.",
)
async def ready(request: Request):
    snapshot = await current_readiness(
        worker_ready=getattr(request.app.state, "worker_initialized", False)
    )
    readiness_tracker.observe(snapshot)
    return JSONResponse(
        {"status": _response_status(snapshot), "components": snapshot.components},
        status_code=snapshot.status_code,
        headers={"Cache-Control": "no-store"},
    )


async def current_readiness(*, worker_ready: bool) -> ReadinessSnapshot:
    database_check = _database_schema_ready()
    if settings.USENET_ENABLED:
        (
            (database_ready, schema_current),
            storage_ready,
            engine_ready,
        ) = await asyncio.gather(
            database_check,
            _artifact_storage_ready(),
            _engine_ready(),
        )
    else:
        database_ready, schema_current = await database_check
        storage_ready = engine_ready = None
    return evaluate_readiness(
        worker_ready=worker_ready,
        database_ready=database_ready,
        schema_current=schema_current,
        usenet_enabled=settings.USENET_ENABLED,
        artifact_storage_ready=storage_ready,
        engine_ready=engine_ready,
        engine_required=settings.USENET_ENGINE_REQUIRED,
    )


def _response_status(snapshot: ReadinessSnapshot) -> str:
    if snapshot.state == "unavailable":
        return "unready"
    return snapshot.state


async def _database_schema_ready() -> tuple[bool, bool]:
    try:
        async with asyncio.timeout(2):
            row = await database.fetch_one(
                """
                SELECT version
                FROM schema_migrations
                WHERE version = :version
                """,
                {"version": MIGRATIONS[-1][0]},
                force_primary=True,
            )
    except (TimeoutError, OSError, sqlite3.Error, asyncpg.PostgresError):
        return False, False
    return True, row is not None


async def _artifact_storage_ready() -> bool:
    try:
        async with asyncio.timeout(2):
            return await asyncio.to_thread(
                _artifact_root_ready,
                Path(settings.USENET_ARTIFACT_DIR),
            )
    except TimeoutError:
        return False


async def _engine_ready() -> bool:
    try:
        async with asyncio.timeout(2):
            await EngineClient(
                Path(settings.USENET_RUNTIME_DIR) / "engine.json"
            ).health()
    except (EngineUnavailable, TimeoutError):
        return False
    return True


def _artifact_root_ready(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        return False
