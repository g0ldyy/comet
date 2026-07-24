"""API package bootstrap, run before FastAPI and endpoint imports."""

from comet.core.models import settings
from comet.observability.metrics import (
    configure_multiprocess_directory,
    load_auth_token,
    metrics,
)

if settings.PROMETHEUS_ENABLED:
    configure_multiprocess_directory(settings.PROMETHEUS_MULTIPROC_DIR)
    auth_token = load_auth_token(
        settings.PROMETHEUS_AUTH_TOKEN,
        settings.PROMETHEUS_AUTH_TOKEN_FILE,
    )
else:
    auth_token = None
metrics.configure(
    settings.PROMETHEUS_ENABLED,
    auth_token,
)
