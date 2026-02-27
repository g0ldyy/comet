# Scrapers, Background Scraper, and DMM

## Scraper Execution Model

`ScraperManager` discovers scraper classes dynamically and runs enabled scrapers concurrently.

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

## Indexer Manager (Jackett/Prowlarr)

`IndexerManager` periodically refreshes active indexers:

- Jackett: Torznab indexer listing.
- Prowlarr: indexer and status endpoints.

Refresh interval is `INDEXER_MANAGER_UPDATE_INTERVAL`.

## Torrent Orchestration

`TorrentManager` combines:

1. cached torrents from DB
2. live scraper results
3. filter pass (`filter_worker`)
4. ranking pass (`rank_worker`)
5. async cache write queue

## Background Scraper

`BackgroundScraperWorker` provides autonomous discovery/scraping cycles with:

- distributed lock (`background_scraper_lock`)
- queue watermark/hard-cap policy
- run budgeting (`BACKGROUND_SCRAPER_RUN_TIME_BUDGET`)
- pause/resume/start/stop controls
- dead-item requeue API
- run history and SLO-style status output

Dashboard APIs are under `/admin/api/background-scraper/*`.

## DMM Ingester

`DMMIngester` downloads and ingests DMM hashlist data into local DB tables:

- `dmm_entries`
- `dmm_ingested_files`

It runs in cycles controlled by `DMM_INGEST_*` settings and uses a distributed lock to avoid concurrent ingests across instances.

## Debrid Account Snapshot Scraper

`debrid_account_scraper.py` can sync user account magnets and merge matched account torrents into stream results.

Relevant controls:

- `DEBRID_ACCOUNT_SCRAPE_REFRESH_INTERVAL`
- `DEBRID_ACCOUNT_SCRAPE_CACHE_TTL`
- `DEBRID_ACCOUNT_SCRAPE_MAX_SNAPSHOT_ITEMS`
- `DEBRID_ACCOUNT_SCRAPE_MAX_MATCH_ITEMS`

## Next

- [Database and Operations](05-database-and-operations.md)
