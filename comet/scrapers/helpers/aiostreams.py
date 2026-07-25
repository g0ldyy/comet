import base64

from comet.core.models import settings
from comet.utils.parsing import associate_urls_credentials


class AIOStreamsConfig:
    def __init__(self):
        self.headers_cache: dict[str, dict[str, str]] = {}
        self.precompute_headers()

    @staticmethod
    def encode_auth_header(uuid_password: str):
        if type(uuid_password) is not str or not uuid_password:
            raise TypeError("AIOStreams credential must be a non-empty string")
        auth_string = base64.b64encode(uuid_password.encode()).decode()
        return {"Authorization": f"Basic {auth_string}"}

    def precompute_headers(self):
        urls = settings.AIOSTREAMS_URL
        credentials = settings.AIOSTREAMS_USER_UUID_AND_PASSWORD

        url_credentials_pairs = associate_urls_credentials(urls, credentials)

        headers_cache = {}
        for _, uuid_password in url_credentials_pairs:
            if uuid_password is not None and uuid_password not in headers_cache:
                headers_cache[uuid_password] = self.encode_auth_header(uuid_password)
        self.headers_cache = headers_cache

    def get_headers_for_credential(self, uuid_password: str | None):
        if uuid_password is None:
            return {}
        if type(uuid_password) is not str or not uuid_password:
            raise TypeError("AIOStreams credential must be a non-empty string or None")
        try:
            headers = self.headers_cache[uuid_password]
        except KeyError as error:
            raise KeyError("unknown AIOStreams credential") from error

        return headers.copy()


aiostreams_config = AIOStreamsConfig()
