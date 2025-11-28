import base64

import orjson

from comet.core.models import settings


class DebridioConfig:
    def __init__(self):
        self.config_b64 = None
        self.precompute_config()

    def precompute_config(self):
        config = {
            "api_key": settings.DEBRIDIO_API_KEY,
            "provider": settings.DEBRIDIO_PROVIDER,
            "providerKey": settings.DEBRIDIO_PROVIDER_KEY,
            "disableUncached": False,
            "maxSize": "",
            "maxReturnPerQuality": "",
            "resolutions": ["4k", "1440p", "1080p", "720p", "480p", "360p", "unknown"],
            "excludedQualities": [],
        }
        self.config_b64 = base64.b64encode(orjson.dumps(config)).decode("utf-8")

    def get_config(self):
        return self.config_b64


debridio_config = DebridioConfig()
