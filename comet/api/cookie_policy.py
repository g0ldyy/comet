from urllib.parse import urlsplit

from starlette.requests import Request


def secure_session_cookie(request: Request, public_base_url: str | None) -> bool:
    if request.url.scheme == "https":
        return True
    if not public_base_url:
        return False
    try:
        return urlsplit(public_base_url).scheme == "https"
    except ValueError:
        return False
