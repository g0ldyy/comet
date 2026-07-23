import os
import uvicorn

from comet.api.app import app
from comet.core.logger import log_startup_info, logger
from comet.core.models import settings


def run_with_uvicorn():
    """Run the server with uvicorn only"""
    config = uvicorn.Config(
        app,
        host=settings.FASTAPI_HOST,
        port=settings.FASTAPI_PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
        workers=settings.FASTAPI_WORKERS,
        log_config=None,
    )
    server = uvicorn.Server(config=config)

    log_startup_info(settings)
    try:
        server.run()
    except KeyboardInterrupt:
        logger.log("COMET", "Server stopped by user")
    except Exception as error:
        logger.exception(f"Unexpected server error: {error}")
        raise
    finally:
        logger.log("COMET", "Server Shutdown")


def run_with_gunicorn():
    """Run the server with gunicorn and uvicorn workers"""
    import gunicorn.app.base

    class StandaloneApplication(gunicorn.app.base.BaseApplication):
        def __init__(self, app, options=None):
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self):
            config = {
                key: value
                for key, value in self.options.items()
                if key in self.cfg.settings and value is not None
            }
            for key, value in config.items():
                self.cfg.set(key.lower(), value)

        def load(self):
            return self.application

    workers = settings.FASTAPI_WORKERS
    if workers < 1:
        workers = min((os.cpu_count() or 1) * 2 + 1, 12)

    options = {
        "bind": f"{settings.FASTAPI_HOST}:{settings.FASTAPI_PORT}",
        "workers": workers,
        "worker_class": "uvicorn.workers.UvicornWorker",
        "timeout": 120,
        "keepalive": 5,
        "preload_app": settings.GUNICORN_PRELOAD_APP,
        "proxy_protocol": True,
        "forwarded_allow_ips": "*",
        "loglevel": "warning",
    }

    log_startup_info(settings)
    logger.log("COMET", f"Starting with gunicorn using {workers} workers")

    StandaloneApplication(app, options).run()


if __name__ == "__main__":
    if os.name == "nt" or not settings.USE_GUNICORN:
        run_with_uvicorn()
    else:
        run_with_gunicorn()
