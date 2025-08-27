import json
import base64

from comet.utils.models import settings


def associate_mediafusion_urls_passwords(
    urls: str | list[str] | None, passwords: str | list[str] | None
):
    if not urls:
        return []

    if isinstance(urls, str):
        urls = [urls]

    if len(urls) == 1:
        if passwords is None:
            password = None
        elif isinstance(passwords, str):
            password = passwords
        elif isinstance(passwords, list) and len(passwords) > 0:
            password = passwords[0]
        else:
            password = None

        passwords_list = [password]
    else:
        if passwords is None:
            passwords_list = [None] * len(urls)
        elif isinstance(passwords, str):
            passwords_list = [passwords] * len(urls)
        elif isinstance(passwords, list):
            passwords_list = []
            for i in range(len(urls)):
                if i < len(passwords):
                    passwords_list.append(passwords[i])
                else:
                    passwords_list.append(None)

    return list(zip(urls, passwords_list))


def encode_mediafusion_api_password(api_password: str):
    user_config = {
        "ap": api_password,
        "nf": ["Disable"],
        "cf": ["Disable"],
        "lss": settings.MEDIAFUSION_LIVE_SEARCH,
    }

    json_str = json.dumps(user_config)
    encoded = base64.urlsafe_b64encode(json_str.encode()).decode()

    return encoded


class MediaFusionConfig:
    def __init__(self):
        self._encoded_user_data = encode_mediafusion_api_password(
            settings.MEDIAFUSION_API_PASSWORD
        )

    @property
    def headers(self):
        return {"encoded_user_data": self._encoded_user_data}


mediafusion_config = MediaFusionConfig()
