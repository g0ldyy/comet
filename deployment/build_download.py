"""Bounded HTTPS downloads shared by checksum-pinned image installers."""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request

_ATTEMPTS = 3


def download_https(url: str, maximum_bytes: int, expected_sha256: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Comet-container-build"},
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.geturl().split(":", 1)[0] != "https":
                    raise RuntimeError("download redirected away from HTTPS")
                payload = response.read(maximum_bytes + 1)
            if len(payload) > maximum_bytes:
                raise RuntimeError("download exceeds its size limit")
            if hashlib.sha256(payload).hexdigest() == expected_sha256:
                return payload
            if attempt + 1 == _ATTEMPTS:
                raise RuntimeError("download SHA-256 mismatch")
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429} and not 500 <= error.code <= 599:
                raise
            if attempt + 1 == _ATTEMPTS:
                raise
        except (TimeoutError, urllib.error.URLError):
            if attempt + 1 == _ATTEMPTS:
                raise
        time.sleep(2**attempt)
        attempt += 1
