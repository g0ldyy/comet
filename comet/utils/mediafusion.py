import json
import base64

from comet.utils.models import settings


def encode_mediafusion_api_password(api_password: str) -> str:
    user_config = {"ap": api_password, "nf": ["Disable"], "cf": ["Disable"]}

    json_str = json.dumps(user_config)
    encoded = base64.urlsafe_b64encode(json_str.encode()).decode()

    return encoded


class MediaFusionConfig:
    def __init__(self):
        self._encoded_user_data = encode_mediafusion_api_password(
            settings.MEDIAFUSION_API_PASSWORD
        )

    @property
    def headers(self) -> dict:
        return {"encoded_user_data": self._encoded_user_data}


mediafusion_config = MediaFusionConfig()
