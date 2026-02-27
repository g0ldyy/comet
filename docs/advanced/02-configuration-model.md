# Configuration Model and Environment Variables

## Source of Truth

For runtime behavior, use `AppSettings` in `comet/core/models.py` as authoritative.

Practical rule:

- `comet/core/models.py`: real defaults and parsing behavior.
- `.env-sample`: human template and comments.

## Important Parsing Behavior

`AppSettings` applies normalization and compatibility logic:

- URL settings have trailing `/` removed.
- URL list entries may include an instance mode suffix (`:live`, `:background`, `:both`).
- `DATABASE_TYPE` aliases (`postgres`, `pgsql`, `sqlite3`, etc.) are normalized.
- Scraper context mode supports `true/both/live/background/false`.
- `PUBLIC_API_TOKEN` and `PUBLIC_API_TOKEN_FILE` support auto-generation when API protection is enabled.
- Optional secret-like fields treat empty/`none` as unset.

## Stremio API Prefix Protection

`STREMIO_API_PREFIX` is generated when either is set:

- `CONFIGURE_PAGE_PASSWORD`
- `PUBLIC_API_TOKEN`

Behavior:

- Protected prefix format: `/s/<token>`
- Token can come from env, token file, or be auto-generated/persisted

## High-Impact Configuration Groups

1. Server/runtime
- `FASTAPI_HOST`, `FASTAPI_PORT`, `FASTAPI_WORKERS`
- `USE_GUNICORN`, `GUNICORN_PRELOAD_APP`
- `EXECUTOR_MAX_WORKERS`

2. Security/UI access
- `ADMIN_DASHBOARD_PASSWORD`, `ADMIN_DASHBOARD_SESSION_TTL`
- `CONFIGURE_PAGE_PASSWORD`, `CONFIGURE_PAGE_SESSION_TTL`
- `PUBLIC_API_TOKEN`, `PUBLIC_API_TOKEN_FILE`

3. Database/cache
- `DATABASE_TYPE`, `DATABASE_URL`, `DATABASE_PATH`
- `DATABASE_READ_REPLICA_URLS`, `DATABASE_FORCE_IPV4_RESOLUTION`
- `METADATA_CACHE_TTL`, `TORRENT_CACHE_TTL`, `LIVE_TORRENT_CACHE_TTL`, `DEBRID_CACHE_TTL`

4. Streaming and proxy
- `PROXY_DEBRID_STREAM`
- `PROXY_DEBRID_STREAM_PASSWORD`
- `PROXY_DEBRID_STREAM_MAX_CONNECTIONS`
- `DISABLE_TORRENT_STREAMS`

5. Scrapers/indexers
- `SCRAPE_*` flags and related URL/API key variables
- Jackett/Prowlarr indexer manager settings

6. Background jobs
- `BACKGROUND_SCRAPER_*`
- `DMM_INGEST_*`
- `DEBRID_ACCOUNT_SCRAPE_*`

7. CometNet
- `COMETNET_*` (documented separately in `docs/cometnet/`)

## Backward-Compatibility Settings

`INDEXER_MANAGER_TYPE`, `INDEXER_MANAGER_URL`, and related `INDEXER_MANAGER_*` values are still supported in runtime and mapped into Jackett/Prowlarr settings in `model_post_init`.

## Recommendation for Operators

- Keep a small `.env` containing only overrides.
- Use `.env-sample` as a catalog of available keys.
- Validate high-risk settings (workers, DB type, proxy, CometNet mode) before production rollout.

## Next

- [Streaming, Playback, and Debrid Flow](03-streaming-and-debrid-flow.md)
