# Prometheus and Grafana Observability

Comet exposes an optional Prometheus endpoint and ships a provisioned production
dashboard. Instrumentation is disabled by default and adds no collectors or
timers to the hot path until `PROMETHEUS_ENABLED=True`.

This endpoint is separate from `/admin/api/metrics`. The admin API provides
cached, human-oriented database aggregates. `/metrics` provides inexpensive
time-series telemetry for Prometheus and never performs database scans while it
is being scraped.

## Quick Start with Docker Compose

The overlay requires the same PostgreSQL password as the base deployment:

```bash
cd deployment
openssl rand -hex 32 > metrics-token.txt

export POSTGRES_PASSWORD="$(openssl rand -hex 32)"
export PROMETHEUS_AUTH_TOKEN_FILE="$PWD/metrics-token.txt"
export GRAFANA_ADMIN_PASSWORD="$(openssl rand -hex 32)"
echo "Grafana admin password: $GRAFANA_ADMIN_PASSWORD"

docker compose \
  -f docker-compose.yml \
  -f monitoring/docker-compose.monitoring.yml \
  up -d
```

For an existing database, export its current `POSTGRES_PASSWORD` instead of
generating a new one. Persist these values in `deployment/.env` for future runs.

Open `http://localhost:3000` (or `GRAFANA_PORT`) and select
**Comet / Comet · Production Overview**. The overlay does not publish the
Prometheus port.

The supplied scrape target is `comet:8000`. In
`deployment/monitoring/prometheus.yml`, update the target when `FASTAPI_PORT`
changes and the job's `metrics_path` when `PROMETHEUS_PATH` changes.

The dashboard and data source are provisioned from version-controlled files.
Grafana intentionally prevents UI saves because a later provisioning refresh
would overwrite them; edit the JSON source instead.

## Native Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROMETHEUS_ENABLED` | `False` | Enables instrumentation and the scrape endpoint. |
| `PROMETHEUS_PATH` | `/metrics` | Static endpoint path. |
| `PROMETHEUS_AUTH_TOKEN` | unset | Optional Bearer token supplied directly. |
| `PROMETHEUS_AUTH_TOKEN_FILE` | unset | Preferred secret file; read once at startup. |
| `PROMETHEUS_MULTIPROC_DIR` | `/tmp/comet-prometheus` | Dedicated mmap state for worker aggregation. |

When either token setting is configured, Prometheus must send:

```http
Authorization: Bearer <token>
```

If both token settings are present, the direct token takes precedence. Keep the
endpoint behind a private network or require a token; operational metrics reveal
traffic volume and enabled dependencies even though they contain no user data.

Example manual scrape configuration:

```yaml
scrape_configs:
  - job_name: comet
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /run/secrets/comet_metrics_token
    static_configs:
      - targets: ["comet:8000"]
```

## Metric Catalog

All labels are selected from bounded application vocabularies. Comet never uses
media IDs, info hashes, request URLs, client IPs, upstream URLs, credentials, or
error messages as labels.

### HTTP and stream service

| Metric | Type | Meaning |
| --- | --- | --- |
| `comet_http_requests_total` | counter | Requests by method, normalized route template, and status. |
| `comet_http_request_duration_seconds` | histogram | End-to-end request latency. |
| `comet_http_requests_in_progress` | gauge | Requests currently executing, summed across workers. |
| `comet_http_response_size_bytes` | histogram | Response size when `Content-Length` is known. |
| `comet_stream_requests_total` | counter | Stream responses by media type, client, cache state, and outcome. |
| `comet_stream_results` | histogram | Number of stream entries returned per request. |
| `comet_torrent_cache_lookups_total` | counter | Torrent-cache hit and miss count. |
| `comet_torrent_cache_results` | histogram | Usable unique torrents loaded per lookup. |

Protected API routes are exported as `/s/{token}/...`; the real public API token
is never exposed in Prometheus.

### Scrapers and debrid

| Metric | Type | Meaning |
| --- | --- | --- |
| `comet_scraper_requests_total` | counter | Scraper runs by scraper, live/background context, and success/error/timeout. |
| `comet_scraper_request_duration_seconds` | histogram | Individual scraper latency. |
| `comet_scraper_torrents_total` | counter | Raw torrent candidates returned by scrapers. |
| `comet_debrid_requests_total` | counter | Availability and cache operations by service and outcome. |
| `comet_debrid_request_duration_seconds` | histogram | Debrid operation latency. |
| `comet_debrid_results_total` | counter | Availability entries returned by debrid operations. |

Multiple configured instances of one scraper are intentionally aggregated under
the scraper name. This preserves a stable time-series count when URLs change and
prevents upstream topology from leaking into labels.

### Database and background work

| Metric | Type | Meaning |
| --- | --- | --- |
| `comet_database_operations_total` | counter | Database calls by operation, primary/replica target, and outcome. |
| `comet_database_operation_duration_seconds` | histogram | Actual primary or replica attempt latency. |
| `comet_database_replica_fallbacks_total` | counter | Replica failures retried on the primary. |
| `comet_background_scraper_queue_items` | gauge | Ready movie, series, and episode queue depth. |
| `comet_background_scraper_oldest_queue_item_age_seconds` | gauge | Age of the oldest ready queue item. |
| `comet_background_scraper_runs_total` | counter | Finished runs by status. |
| `comet_background_scraper_run_duration_seconds` | histogram | Finished run duration. |
| `comet_background_scraper_items_total` | counter | Successful and failed items from finished runs. |
| `comet_background_scraper_torrents_total` | counter | Torrents found by finished runs. |

Replica attempts and their primary fallback are timed independently. Queue
gauges reuse snapshots already required by the worker and do not add polling
queries.

### Debrid stream proxy

| Metric | Type | Meaning |
| --- | --- | --- |
| `comet_proxy_stream_active_connections` | gauge | Active proxy connections across workers. |
| `comet_proxy_stream_connections_total` | counter | Completed or expired proxy connections. |
| `comet_proxy_stream_bytes_total` | counter | Bytes from completed proxy connections. |
| `comet_proxy_stream_duration_seconds` | histogram | Completed connection duration. |

Bytes are committed to Prometheus once per connection, not once per network
chunk, to keep the streaming hot loop free of metric locks.

## Multiprocess Correctness

Gunicorn workers do not share Python memory. Comet uses the official
`client_python` multiprocess mmap registry, clears stale metric files once before
the server starts, aggregates on each scrape, and marks exited Gunicorn workers
dead. Live gauges use explicit sum or max semantics.

The default `python -m comet.main` entrypoint handles this automatically. A
custom process manager must:

1. set `PROMETHEUS_MULTIPROC_DIR` before importing `prometheus_client`;
2. clear that dedicated directory before the process manager starts;
3. call `prometheus_client.multiprocess.mark_process_dead()` when workers exit.

Counters reset after a complete Comet process-manager restart. Prometheus
`rate()` and `increase()` handle counter resets correctly.

## Alerts

`deployment/monitoring/alerts.yml` includes starting rules for:

- scrape availability;
- HTTP 5xx ratio and p95 latency;
- scraper failure/timeout ratio;
- database errors and replica fallbacks;
- stale background queue and failed background runs.

Thresholds are conservative defaults, not universal SLOs. Tune them after
observing normal production traffic. The overlay evaluates and displays rules
but does not bundle Alertmanager; connect one when notifications are required.

## Performance and Cardinality

- Disabled mode uses a boolean branch only at instrumented boundaries.
- `/metrics` performs serialization only; it does not query Comet's database.
- Histograms use fixed buckets chosen for Comet's sub-millisecond database calls
  and potentially long scraper/debrid calls.
- Metrics are recorded once per operation. Proxy byte metrics avoid the
  per-chunk loop.
- Route labels use FastAPI templates, so configured payloads and media IDs never
  create time series.

Prometheus itself accounts for some process overhead. A 15-second scrape interval
is the recommended balance; scraping more frequently rarely improves incident
detection enough to justify the extra serialization and storage.

## Upstream References

- [Prometheus Python client multiprocess mode](https://prometheus.github.io/client_python/multiprocess/)
- [Prometheus instrumentation practices](https://prometheus.io/docs/practices/instrumentation/)
- [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)

## Next

- [HTTP API Reference](06-http-api-reference.md)
