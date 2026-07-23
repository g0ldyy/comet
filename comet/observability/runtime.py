"""One-time process-manager preparation for Prometheus multiprocess mode."""

from comet.core.models import settings
from comet.observability.metrics import prepare_multiprocess_directory

if settings.PROMETHEUS_ENABLED:
    prepare_multiprocess_directory(settings.PROMETHEUS_MULTIPROC_DIR)
