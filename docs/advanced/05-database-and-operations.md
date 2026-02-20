# Database and Operations

## Database Backends

Supported backends:

- SQLite (`DATABASE_TYPE=sqlite`)
- PostgreSQL (`DATABASE_TYPE=postgresql`)

Production recommendation from runtime logs and code path: PostgreSQL for concurrency-heavy workloads.

## Startup Behavior

At startup, Comet:

1. connects database(s)
2. ensures schema/tables/indexes
3. performs legacy index migration cleanup
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

When SQLite is used, startup applies PRAGMA tuning and still supports all core features, but high-concurrency operation is limited.

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

Export/import uses `DatabaseManager` with batched I/O and optional parallel processing.

## Operational Advice

- Keep `DATABASE_BATCH_SIZE` tuned to your hardware for import/export.
- Keep `DATABASE_STARTUP_CLEANUP_INTERVAL` non-zero in larger deployments to avoid heavy cleanup every restart.

## Next

- [HTTP API Reference](06-http-api-reference.md)
