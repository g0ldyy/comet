import base64

from comet.utils.models import settings
from comet.utils.general import associate_urls_credentials


def encode_aiostreams_auth_header(uuid_password: str):
    auth_string = base64.b64encode(uuid_password.encode()).decode()
    return {"Authorization": f"Basic {auth_string}"}


class AIOStreamsConfig:
    def __init__(self):
        self.headers_cache = {}
        self.default_headers = {}
        self.precompute_headers()

    def precompute_headers(self):
        urls = settings.AIOSTREAMS_URL
        credentials = settings.AIOSTREAMS_USER_UUID_AND_PASSWORD

        url_credentials_pairs = associate_urls_credentials(urls, credentials)

        for _, uuid_password in url_credentials_pairs:
            if uuid_password is not None and uuid_password not in self.headers_cache:
                self.headers_cache[uuid_password] = encode_aiostreams_auth_header(
                    uuid_password
                )

    def get_headers_for_credential(self, uuid_password: str | None):
        if uuid_password is None:
            return self.default_headers

        return self.headers_cache.get(uuid_password, self.default_headers)


aiostreams_config = AIOStreamsConfig()
