# Database and Operations

## Database Backends

Supported backends:

- SQLite (`DATABASE_TYPE=sqlite`)
- PostgreSQL (`DATABASE_TYPE=postgresql`)

Production recommendation from runtime logs and code path: PostgreSQL for concurrency-heavy workloads.

## Startup Behavior

At startup, Comet:

1. connects database(s)
2. applies additive schema migrations tracked in `schema_migrations`
3. ensures current indexes and removes legacy superseded indexes
4. clears transient tables (`active_connections`, `metrics_cache`)
5. runs startup cleanup sweep depending on `DATABASE_STARTUP_CLEANUP_INTERVAL`

Startup cleanup handles TTL-based deletion for cache tables and job-history retention.

## Read Replicas

Replica routing is implemented by `ReplicaAwareDatabase`:

- writes always go to primary
- reads can go to replicas
- transactions force primary
- replica read failure falls back to primary

Configured via `DATABASE_READ_REPLICA_URLS`.

## SQLite Notes

When SQLite is used, two layers of PRAGMA configuration apply. Per-connection PRAGMAs (`foreign_keys` and `busy_timeout`) are enforced on each acquired connection via the acquire hook in `comet.core.models`. Broader PRAGMA tuning (`journal_mode`, `synchronous`, `mmap_size`, `page_size`, `cache_size`, etc.) is configured once at startup in the database initialization code in `comet.core.database`. Core features still work, but high-concurrency operation is limited compared with PostgreSQL.

## DB Import/Export CLI

Entry point:

```bash
python -m comet.db_cli
```

Supported commands:

- `list-tables`
- `info --table <name>`
- `export --output <dir> [--tables ...]`
- `import --input <dir> [--tables ...]`
- `cleanup-debrid-account [--provider imdb|kitsu] [--media-id ...] [--min-rows ...] [--apply]`

Export/import uses `DatabaseManager` with batched I/O and optional parallel processing.

### Repairing DebridAccount torrent associations

The account scraper cleanup revalidates persisted `DebridAccount|*` torrents with
the current title, alias, and year matcher. It keeps independently discovered
rows and conservatively retains hashes whose legacy parse data cannot be
verified.

Audit the known affected movie first (dry-run is the default):

```bash
python -m comet.db_cli cleanup-debrid-account \
  --media-id tt29552248 --media-type movie
```

Apply the reviewed result:

```bash
python -m comet.db_cli cleanup-debrid-account \
  --media-id tt29552248 --media-type movie --apply
```

To discover and process every affected media ID, omit `--media-id`. Large
instances can start with the most polluted entries using `--min-rows 1000`
and/or `--limit`, then lower the threshold. Processing uses keyset pagination
and bounded delete batches; `--batch-size` tunes the number of distinct hashes
validated per read.

Use `--provider imdb` or `--provider kitsu` to restrict discovery to one ID
provider. Kitsu cache IDs are stored numerically; the cleanup resolves their
movie/series type from the persisted anime mapping.

For example, audit and then clean only Kitsu associations:

```bash
python -m comet.db_cli cleanup-debrid-account --provider kitsu
python -m comet.db_cli cleanup-debrid-account --provider kitsu --apply
```

## Operational Advice

- Keep `DATABASE_BATCH_SIZE` tuned to your hardware for import/export.
- Keep `DATABASE_STARTUP_CLEANUP_INTERVAL` non-zero in larger deployments to avoid heavy cleanup every restart.

## Next

- [HTTP API Reference](06-http-api-reference.md)
