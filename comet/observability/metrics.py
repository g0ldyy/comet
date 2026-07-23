import os
import re
from pathlib import Path

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
_SCRAPER_INSTANCE_SUFFIX = re.compile(r"\s+#\d+$")


def _normalize_scraper_label(name: str) -> str:
    return _SCRAPER_INSTANCE_SUFFIX.sub("", name).strip().casefold()


class CometMetrics:
    """Lazy, low-cardinality Prometheus instrumentation for Comet."""

    def __init__(self) -> None:
        self.enabled = False
        self.auth_token: str | None = None
        self._initialized = False
        self._children = {}

    def configure(self, enabled: bool, auth_token: str | None = None) -> None:
        self.enabled = bool(enabled)
        self.auth_token = auth_token
        if self.enabled and not self._initialized:
            self._initialize()

    def _initialize(self) -> None:
        from prometheus_client import Counter, Gauge, Histogram

        latency_buckets = (
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1,
            2.5,
            5,
            10,
            30,
            60,
            120,
        )
        db_buckets = (
            0.0005,
            0.001,
            0.0025,
            0.005,
            0.01,
            0.025,
            0.05,
            0.1,
            0.25,
            0.5,
            1,
            2.5,
            5,
        )

        self.http_requests = Counter(
            "comet_http_requests_total",
            "HTTP requests handled by Comet.",
            ("method", "route", "status"),
        )
        self.http_duration = Histogram(
            "comet_http_request_duration_seconds",
            "End-to-end HTTP request duration.",
            ("method", "route"),
            buckets=latency_buckets,
        )
        self.http_in_progress = Gauge(
            "comet_http_requests_in_progress",
            "HTTP requests currently being processed.",
            ("method",),
            multiprocess_mode="livesum",
        )
        self.http_response_size = Histogram(
            "comet_http_response_size_bytes",
            "HTTP response size when Content-Length is known.",
            ("route",),
            buckets=(
                128,
                512,
                1024,
                4096,
                16384,
                65536,
                262144,
                1048576,
                4194304,
            ),
        )

        self.stream_requests = Counter(
            "comet_stream_requests_total",
            "Stream endpoint responses.",
            ("media_type", "client", "cache", "outcome"),
        )
        self.stream_results = Histogram(
            "comet_stream_results",
            "Number of streams returned per response.",
            ("media_type", "client"),
            buckets=(0, 1, 2, 5, 10, 20, 40, 80, 160, 320),
        )
        self.torrent_cache_lookups = Counter(
            "comet_torrent_cache_lookups_total",
            "Torrent cache lookups by result.",
            ("media_type", "result"),
        )
        self.torrent_cache_results = Histogram(
            "comet_torrent_cache_results",
            "Usable unique torrents loaded by a cache lookup.",
            ("media_type",),
            buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000),
        )

        self.scraper_requests = Counter(
            "comet_scraper_requests_total",
            "Scraper executions by terminal outcome.",
            ("scraper", "context", "outcome"),
        )
        self.scraper_duration = Histogram(
            "comet_scraper_request_duration_seconds",
            "Scraper execution duration.",
            ("scraper", "context", "outcome"),
            buckets=latency_buckets,
        )
        self.scraper_results = Counter(
            "comet_scraper_torrents_total",
            "Torrent candidates returned by scrapers.",
            ("scraper", "context"),
        )

        self.debrid_requests = Counter(
            "comet_debrid_requests_total",
            "Debrid availability operations.",
            ("service", "operation", "outcome"),
        )
        self.debrid_duration = Histogram(
            "comet_debrid_request_duration_seconds",
            "Debrid availability operation duration.",
            ("service", "operation", "outcome"),
            buckets=latency_buckets,
        )
        self.debrid_results = Counter(
            "comet_debrid_results_total",
            "Availability entries returned by debrid operations.",
            ("service", "operation"),
        )

        self.database_operations = Counter(
            "comet_database_operations_total",
            "Database operations by target and outcome.",
            ("operation", "target", "outcome"),
        )
        self.database_duration = Histogram(
            "comet_database_operation_duration_seconds",
            "Database operation duration.",
            ("operation", "target", "outcome"),
            buckets=db_buckets,
        )
        self.database_replica_fallbacks = Counter(
            "comet_database_replica_fallbacks_total",
            "Read replica failures retried on the primary.",
            ("operation",),
        )

        self.background_queue = Gauge(
            "comet_background_scraper_queue_items",
            "Ready background scraper queue items.",
            ("kind",),
            multiprocess_mode="livemax",
        )
        self.background_oldest = Gauge(
            "comet_background_scraper_oldest_queue_item_age_seconds",
            "Age of the oldest ready background scraper queue item.",
            multiprocess_mode="livemax",
        )
        self.background_runs = Counter(
            "comet_background_scraper_runs_total",
            "Completed background scraper runs.",
            ("status",),
        )
        self.background_run_duration = Histogram(
            "comet_background_scraper_run_duration_seconds",
            "Background scraper run duration.",
            ("status",),
            buckets=(1, 5, 10, 30, 60, 300, 900, 1800, 3600, 7200),
        )
        self.background_items = Counter(
            "comet_background_scraper_items_total",
            "Items processed by completed background scraper runs.",
            ("outcome",),
        )
        self.background_torrents = Counter(
            "comet_background_scraper_torrents_total",
            "Torrents found by completed background scraper runs.",
        )

        self.proxy_connections = Gauge(
            "comet_proxy_stream_active_connections",
            "Active proxied debrid stream connections.",
            multiprocess_mode="livesum",
        )
        self.proxy_completed = Counter(
            "comet_proxy_stream_connections_total",
            "Completed proxied debrid stream connections.",
        )
        self.proxy_bytes = Counter(
            "comet_proxy_stream_bytes_total",
            "Bytes transferred through completed proxied debrid streams.",
        )
        self.proxy_duration = Histogram(
            "comet_proxy_stream_duration_seconds",
            "Duration of completed proxied debrid streams.",
            buckets=(1, 5, 15, 30, 60, 300, 900, 1800, 3600, 7200, 14400),
        )

        self._initialized = True

    def _child(self, collector_name: str, *label_values: str):
        key = (collector_name, *label_values)
        child = self._children.get(key)
        if child is None:
            child = getattr(self, collector_name).labels(*label_values)
            self._children[key] = child
        return child

    def http_started(self, method: str) -> None:
        if self.enabled:
            self._child("http_in_progress", method.upper()).inc()

    def http_finished(
        self,
        method: str,
        route: str,
        status: int,
        duration: float,
        response_size: int | None,
    ) -> None:
        if not self.enabled:
            return
        method = method.upper()
        self._child("http_in_progress", method).dec()
        self._child("http_requests", method, route, str(status)).inc()
        self._child("http_duration", method, route).observe(duration)
        if response_size is not None:
            self._child("http_response_size", route).observe(response_size)

    def observe_stream(
        self,
        media_type: str,
        client: str,
        cache: str,
        outcome: str,
        result_count: int,
    ) -> None:
        if not self.enabled:
            return
        self._child("stream_requests", media_type, client, cache, outcome).inc()
        self._child("stream_results", media_type, client).observe(result_count)

    def observe_torrent_cache(
        self, media_type: str, result: str, result_count: int
    ) -> None:
        if not self.enabled:
            return
        self._child("torrent_cache_lookups", media_type, result).inc()
        self._child("torrent_cache_results", media_type).observe(result_count)

    def observe_scraper(
        self,
        scraper: str,
        context: str,
        outcome: str,
        duration: float,
        result_count: int,
    ) -> None:
        if not self.enabled:
            return
        scraper = _normalize_scraper_label(scraper)
        self._child("scraper_requests", scraper, context, outcome).inc()
        self._child("scraper_duration", scraper, context, outcome).observe(duration)
        if result_count:
            self._child("scraper_results", scraper, context).inc(result_count)

    def observe_debrid(
        self,
        service: str,
        operation: str,
        outcome: str,
        duration: float,
        result_count: int,
    ) -> None:
        if not self.enabled:
            return
        service = service.casefold()
        self._child("debrid_requests", service, operation, outcome).inc()
        self._child("debrid_duration", service, operation, outcome).observe(duration)
        if result_count:
            self._child("debrid_results", service, operation).inc(result_count)

    def observe_database(
        self, operation: str, target: str, outcome: str, duration: float
    ) -> None:
        if not self.enabled:
            return
        self._child("database_operations", operation, target, outcome).inc()
        self._child("database_duration", operation, target, outcome).observe(duration)

    def observe_database_fallback(self, operation: str) -> None:
        if self.enabled:
            self._child("database_replica_fallbacks", operation).inc()

    def set_background_queue(self, snapshot: dict) -> None:
        if not self.enabled:
            return
        for kind in ("movies", "series", "episodes"):
            self._child("background_queue", kind).set(snapshot[kind])
        self.background_oldest.set(snapshot["oldest_age_s"])

    def observe_background_run(self, status: str, stats) -> None:
        if not self.enabled:
            return
        self._child("background_runs", status).inc()
        self._child("background_run_duration", status).observe(stats.duration)
        self._child("background_items", "success").inc(stats.total_success)
        self._child("background_items", "failed").inc(stats.total_failed)
        self.background_torrents.inc(stats.total_torrents_found)

    def proxy_connection_started(self) -> None:
        if self.enabled:
            self.proxy_connections.inc()

    def proxy_connection_finished(self, byte_count: int, duration: float) -> None:
        if not self.enabled:
            return
        self.proxy_connections.dec()
        self.proxy_completed.inc()
        self.proxy_bytes.inc(byte_count)
        self.proxy_duration.observe(duration)


metrics = CometMetrics()


def load_auth_token(token: str | None, token_file: str | None) -> str | None:
    if token is not None:
        return token
    if token_file is None:
        return None

    value = Path(token_file).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("PROMETHEUS_AUTH_TOKEN_FILE must not be empty")
    return value


def configure_multiprocess_directory(path: str) -> Path:
    directory = Path(path).resolve()
    if directory == Path(directory.anchor) or len(directory.parts) < 3:
        raise ValueError("PROMETHEUS_MULTIPROC_DIR must be a dedicated directory")

    directory.mkdir(parents=True, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(directory)
    return directory


def prepare_multiprocess_directory(path: str) -> None:
    """Clear client_python mmap state once, before the server forks workers."""
    directory = configure_multiprocess_directory(path)
    for entry in directory.glob("*.db"):
        if entry.is_file() or entry.is_symlink():
            entry.unlink()


def mark_process_dead(pid: int, path: str) -> None:
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(pid, path=path)


def render_metrics() -> bytes:
    from prometheus_client import CollectorRegistry, generate_latest, multiprocess

    multiprocess_path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiprocess_path:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=multiprocess_path)
        return generate_latest(registry)

    return generate_latest()
