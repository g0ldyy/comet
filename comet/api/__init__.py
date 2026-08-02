"""Import-safe API package bootstrap."""

from __future__ import annotations


def configure_metrics(settings: object) -> None:
    from comet.observability.metrics import (
        configure_multiprocess_directory,
        load_auth_token,
        metrics,
    )

    configure_multiprocess_directory(settings.PROMETHEUS_MULTIPROC_DIR)
    if settings.PROMETHEUS_ENABLED:
        auth_token = load_auth_token(
            settings.PROMETHEUS_AUTH_TOKEN,
            settings.PROMETHEUS_AUTH_TOKEN_FILE,
        )
    else:
        auth_token = None
    metrics.configure(True, auth_token)
    metrics.set_usenet_engine_configured(settings.USENET_ENABLED)


__all__ = ("configure_metrics",)
