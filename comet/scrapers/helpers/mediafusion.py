import base64
import json

from comet.core.models import settings
from comet.utils.parsing import associate_urls_credentials


class MediaFusionConfig:
    def __init__(self):
        self.password_cache = {}
        self.default_headers = {"encoded_user_data": self.encode_api_password("")}
        self.precompute_encodings()

    @staticmethod
    def encode_api_password(api_password: str):
        user_config = {
            "ap": api_password,
            "nf": ["Disable"],
            "cf": ["Disable"],
            "lss": settings.MEDIAFUSION_LIVE_SEARCH,
        }

        json_str = json.dumps(user_config)
        encoded = base64.urlsafe_b64encode(json_str.encode()).decode()

        return encoded

    def precompute_encodings(self):
        url_credentials_pairs = associate_urls_credentials(
            settings.MEDIAFUSION_URL, settings.MEDIAFUSION_API_PASSWORD
        )

        for _, password in url_credentials_pairs:
            if password is not None and password not in self.password_cache:
                self.password_cache[password] = self.encode_api_password(password)

    def get_headers_for_password(self, api_password: str | None):
        if api_password is None:
            return self.default_headers

        return {"encoded_user_data": self.password_cache[api_password]}


mediafusion_config = MediaFusionConfig()
