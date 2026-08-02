from http import HTTPStatus
from http.client import HTTPConnection

from comet.core.server_settings import ServerSettings


def main() -> int:
    connection = None
    try:
        port = ServerSettings().FASTAPI_PORT
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/ready")
        return 0 if connection.getresponse().status == HTTPStatus.OK else 1
    except Exception:
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main()) from None
