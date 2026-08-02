import hashlib
import urllib.error

import pytest

from deployment import build_download


class _Response:
    payload = b"verified download"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return "https://downloads.example/archive"

    def read(self, _limit):
        return self.payload


def test_transient_download_failure_is_retried(monkeypatch):
    attempts = iter(
        (
            urllib.error.HTTPError(
                "https://downloads.example/archive",
                504,
                "Gateway Timeout",
                None,
                None,
            ),
            _Response(),
        )
    )

    def open_response(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(build_download.urllib.request, "urlopen", open_response)
    monkeypatch.setattr(build_download.time, "sleep", lambda _seconds: None)

    assert (
        build_download.download_https(
            "https://downloads.example/archive",
            1024,
            hashlib.sha256(_Response.payload).hexdigest(),
        )
        == _Response.payload
    )


def test_permanent_http_failure_is_not_retried(monkeypatch):
    calls = 0

    def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://downloads.example/missing",
            404,
            "Not Found",
            None,
            None,
        )

    monkeypatch.setattr(build_download.urllib.request, "urlopen", fail)

    with pytest.raises(urllib.error.HTTPError) as error:
        build_download.download_https(
            "https://downloads.example/missing",
            1024,
            "0" * 64,
        )

    assert error.value.code == 404
    assert calls == 1


def test_corrupt_download_is_retried(monkeypatch):
    valid = _Response()
    corrupt = _Response()
    corrupt.payload = b"corrupt"
    responses = iter((corrupt, valid))

    monkeypatch.setattr(
        build_download.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: next(responses),
    )
    monkeypatch.setattr(build_download.time, "sleep", lambda _seconds: None)

    assert (
        build_download.download_https(
            "https://downloads.example/archive",
            1024,
            hashlib.sha256(valid.payload).hexdigest(),
        )
        == valid.payload
    )
