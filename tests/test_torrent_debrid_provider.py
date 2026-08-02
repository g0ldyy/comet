import asyncio

import orjson
import pytest

from comet.playback.base import Readiness
from comet.playback.providers.torrent_debrid import TorrentDebridProvider


class _Response:
    def __init__(self, status: int, payload: object):
        self.status = status
        self._body = orjson.dumps(payload)
        self._read = False
        self.content = self
        self.headers = {
            "Content-Type": "application/json",
            "Content-Encoding": "identity",
            "Content-Length": str(len(self._body)),
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self, _maximum=-1):
        if self._read:
            return b""
        self._read = True
        return self._body


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_debrid_validation_is_read_only_bounded_and_non_redirecting():
    session = _Session(
        _Response(
            200,
            {"data": {"subscription_status": "premium"}},
        )
    )
    provider = TorrentDebridProvider(
        session,
        "realdebrid",
        "account-key",
        "203.0.113.4",
    )

    status = asyncio.run(provider.validate_config({}))

    assert status.readiness is Readiness.READY
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url.endswith("/v0/store/user")
    assert kwargs["params"] == {"client_ip": "203.0.113.4"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert kwargs["headers"]["X-StremThru-Store-Authorization"] == (
        "Bearer account-key"
    )


def test_debrid_validation_accepts_extended_active_subscription_status():
    provider = TorrentDebridProvider(
        _Session(_Response(200, {"data": {"subscription_status": "active"}})),
        "realdebrid",
        "account-key",
        "",
    )

    status = asyncio.run(provider.validate_config({}))

    assert status.readiness is Readiness.READY


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            _Response(401, {"error": "denied"}),
            Readiness.TERMINAL_FAILURE,
        ),
        (
            _Response(503, {"error": "offline"}),
            Readiness.RETRYABLE_FAILURE,
        ),
        (
            _Response(200, {"data": {"subscription_status": "expired"}}),
            Readiness.READY,
        ),
    ],
)
def test_debrid_validation_classifies_transport_and_response_states(
    response,
    expected,
):
    provider = TorrentDebridProvider(
        _Session(response),
        "realdebrid",
        "account-key",
        "",
    )

    status = asyncio.run(provider.validate_config({}))

    assert status.readiness is expected


def test_debrid_validation_does_not_hide_client_construction_failures(monkeypatch):
    provider = TorrentDebridProvider(
        object(),
        "realdebrid",
        "account-key",
        "",
    )
    monkeypatch.setattr(
        "comet.playback.providers.torrent_debrid.get_debrid",
        lambda *_args: (_ for _ in ()).throw(ValueError("implementation failed")),
    )

    with pytest.raises(ValueError, match="implementation failed"):
        asyncio.run(provider.validate_config({}))
