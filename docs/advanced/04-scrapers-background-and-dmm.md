# Discovery adapters, background discovery, and DMM

## Discovery execution model

`TorrentAdapterRegistry` discovers the server-configured torrent adapters. All
enabled adapters implement the common `DiscoveryAdapter.search()` contract and
are scheduled concurrently by the same transport-neutral `SearchCoordinator`
used for Usenet discovery.

Enablement is controlled by `SCRAPE_*` settings with context modes:

- `both` / `true`
- `live`
- `background`
- `false`

For URL-based scrapers, each URL can also override context with a suffix:

- `:both` (default when omitted)
- `:live`
- `:background`

Effective execution is the intersection of scraper-level mode (`SCRAPE_*`) and URL-level mode.

Anime-only gates are enforced for specific scrapers (`NYAA_ANIME_ONLY`, `ANIMETOSHO_ANIME_ONLY`, `SEADEX_ANIME_ONLY`, `NEKOBT_ANIME_ONLY`).

AnimeTosho Usenet discovery is an instance capability rather than a
user-configurable source. Enable it with `SCRAPE_ANIMETOSHO_USENET=True`;
its torrent scraper remains controlled separately by `SCRAPE_ANIMETOSHO`.

### Scraper Timeouts

Each scraper invocation has a context-specific runtime budget:

- `LIVE_SCRAPE_TIMEOUT`
- `BACKGROUND_SCRAPE_TIMEOUT`

`SCRAPER_TIMEOUT_OVERRIDES` accepts a JSON object for provider-specific tuning.
Selectors are case-insensitive and may optionally include a `:live` or
`:background` suffix:

```env
LIVE_SCRAPE_TIMEOUT=10
BACKGROUND_SCRAPE_TIMEOUT=60
SCRAPER_TIMEOUT_OVERRIDES={"Zilean":90,"Jackett:live":20}
```

Resolution order is context-specific scraper override, scraper override, then
the context default. A timeout cancels only the affected adapter invocation;
other providers continue and their results are retained.

Without an explicit override, Jackett and Prowlarr also reserve
`INDEXER_MANAGER_WAIT_TIMEOUT` for cold indexer-manager initialization before
their context-specific scrape budget begins.

`HTTP_CLIENT_TIMEOUT_TOTAL` remains the timeout for one HTTP request. Scraper
budgets cover the complete provider operation, including pagination and retries.

## Indexer Manager (Jackett/Prowlarr)

`IndexerManager` periodically refreshes active indexers:

- Jackett: Torznab indexer listing.
- Prowlarr: indexer and status endpoints.

Refresh interval is `INDEXER_MANAGER_UPDATE_INTERVAL`.

## Torrent result processing

`TorrentResultAccumulator` retains the established torrent filtering, ranking,
and compatibility view around the unified discovery result. It combines:

1. cached torrents from DB
2. candidates returned by `SearchCoordinator`
3. filter pass (`filter_worker`)
4. ranking pass (`rank_worker`)
5. async cache write queue

There is no separate scraper scheduler: live and background torrent discovery
both enter `SearchCoordinator`, with their respective work class and deadline.

## Background Scraper

`BackgroundScraperWorker` provides autonomous discovery/scraping cycles with:

- distributed lock (`background_scraper_lock`)
- queue watermark/hard-cap policy
- run budgeting (`BACKGROUND_SCRAPER_RUN_TIME_BUDGET`)
- pause/resume/start/immediate-stop controls
- a cancellable drain mode that finishes the active run before stopping
- dead-item requeue API
- run history and SLO-style status output

Dashboard APIs are under `/api/v1/admin/scraping/*`.

## DMM Ingester

`DMMIngester` downloads and ingests DMM hashlist data into local DB tables:

- `dmm_entries`
- `dmm_ingested_files`

It runs in cycles controlled by `DMM_INGEST_*` settings and uses a distributed lock to avoid concurrent ingests across instances.

## Debrid Account Snapshot Scraper

`debrid_account_scraper.py` can sync user account magnets and merge matched account torrents into stream results.

Account snapshots are matched against the current request only. The result is
not persisted as a public discovery association, and stale snapshots expire
through the configured TTL.

Relevant controls:

- `DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL`
- `DEBRID_ACCOUNT_SCRAPE_CACHE_TTL`
- `DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS`
- `DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS`

## Next

- [Database and Operations](05-database-and-operations.md)
