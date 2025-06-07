import json
import base64

from comet.utils.models import settings


def encode_mediafusion_api_password(api_password: str) -> str:
    user_config = {"ap": api_password}

    json_str = json.dumps(user_config)
    encoded = base64.urlsafe_b64encode(json_str.encode()).decode()

    return encoded


class MediaFusionConfig:
    def __init__(self):
        self._encoded_user_data = None
        self._has_api_password = bool(settings.MEDIAFUSION_API_PASSWORD)

        if self._has_api_password:
            self._encoded_user_data = encode_mediafusion_api_password(
                settings.MEDIAFUSION_API_PASSWORD.strip()
            )

    @property
    def headers(self) -> dict:
        if self._has_api_password and self._encoded_user_data:
            return {"encoded_user_data": self._encoded_user_data}
        return {}


mediafusion_config = MediaFusionConfig()
