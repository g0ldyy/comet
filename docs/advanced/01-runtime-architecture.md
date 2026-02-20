# Runtime and Architecture

## Server Process Model

Comet runs as a FastAPI app and starts through `python -m comet.main`.

Runtime mode:

- Uses **Gunicorn + Uvicorn workers** when `USE_GUNICORN=True` and not on Windows.
- Uses plain **Uvicorn** otherwise.

Worker behavior:

- `FASTAPI_WORKERS >= 1`: exact worker count.
- `FASTAPI_WORKERS < 1`: computed as `min((cpu_count * 2 + 1), 12)` in gunicorn mode.

CPU-bound filtering/ranking jobs run in a `ProcessPoolExecutor` controlled by `EXECUTOR_MAX_WORKERS`.

## Application Lifecycle

At startup (`lifespan`):

1. Database setup and migrations/index preparation.
2. Process pool setup.
3. Shared HTTP client initialization.
4. Optional trackers download (`DOWNLOAD_GENERIC_TRACKERS`).
5. Anime mapping load.
6. Optional bandwidth monitor init (`PROXY_DEBRID_STREAM`).
7. Periodic cleanup tasks start.
8. Optional background scraper start.
9. Optional DMM ingester start.
10. CometNet startup:
- Relay mode if `COMETNET_RELAY_URL` is set.
- Integrated mode if `COMETNET_ENABLED=True`.
11. Indexer manager background loop start.

At shutdown, the reverse order is applied (tasks cancelled/stopped, network clients closed, DB disconnected, executor shutdown).

## Routing Model

Routers are mounted in `comet/api/app.py`.

- Base routes: health, configure, admin, kodi, cometnet.
- Stremio routes: manifest, stream, playback, debrid-sync, chilllink.
- If API prefix protection is active, Stremio endpoints are served under `/s/<token>/...`.

## Next

- [Configuration Model and Environment Variables](02-configuration-model.md)
