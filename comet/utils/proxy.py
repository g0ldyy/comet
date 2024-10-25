import base64
from functools import cache
from typing import MutableMapping
from urllib.parse import quote_plus

from comet.utils.models import settings


@cache
def has_proxy_url_template():
    return bool(settings.PROXY_URL_TEMPLATE)


@cache
def get_proxy_basic_auth_credential():
    if not settings.PROXY_AUTH_CREDENTIAL:
        return settings.PROXY_DEBRID_STREAM_PASSWORD or ""
    if ":" in settings.PROXY_AUTH_CREDENTIAL:
        return base64.b64encode(settings.PROXY_AUTH_CREDENTIAL.encode()).decode("ascii")
    return settings.PROXY_AUTH_CREDENTIAL


def get_proxy_url(url: str, include_credential=False):
    if not settings.PROXY_URL_TEMPLATE:
        return url
    url = settings.PROXY_URL_TEMPLATE.replace("{URL}", quote_plus(f"{url}")).replace(
        "{TOKEN}", (get_proxy_basic_auth_credential() if include_credential else "")
    )
    return url


def set_proxy_auth_header(headers: MutableMapping):
    if not settings.PROXY_AUTH_CREDENTIAL:
        return headers
    headers["Proxy-Authorization"] = f"Basic {get_proxy_basic_auth_credential()}"
    return headers
