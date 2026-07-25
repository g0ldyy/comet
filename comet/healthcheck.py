from http import HTTPStatus
from http.client import HTTPConnection, HTTPException

from comet.core.server_settings import ServerSettings


def main() -> int:
    port = ServerSettings().FASTAPI_PORT
    connection = HTTPConnection("127.0.0.1", port, timeout=5)

    try:
        connection.request("GET", "/health")
        return 0 if connection.getresponse().status == HTTPStatus.OK else 1
    except (HTTPException, OSError):
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
