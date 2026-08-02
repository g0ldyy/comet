"""Contracts for the shared bounded external-provider JSON codec."""

import unittest

from comet.core.provider_json import (
    ProviderJsonError,
    decode_provider_json,
    is_success_status,
    read_provider_body,
)


class _Content:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _maximum):
        return next(self._chunks, b"")


class _Response:
    def __init__(self, chunks, headers=None):
        self.content = _Content(chunks)
        self.headers = {
            "Content-Type": "application/json",
            **(headers or {}),
        }


class ProviderHttpJsonTests(unittest.IsolatedAsyncioTestCase):
    def test_success_status_accepts_the_full_http_success_class(self):
        self.assertTrue(
            all(is_success_status(status) for status in (200, 201, 207, 299))
        )
        self.assertFalse(any(is_success_status(status) for status in (199, 300, 404)))

    async def test_body_is_bounded_by_observed_bytes_not_advisory_headers(self):
        for headers in (
            {"Content-Length": "999999999"},
            {"Content-Type": "text/plain"},
            {"Content-Encoding": "gzip"},
        ):
            with self.subTest(headers=headers):
                body = await read_provider_body(
                    _Response([b'{"data":', b"{}}"], headers)
                )
                self.assertEqual(body, b'{"data":{}}')

    async def test_body_rejects_invalid_limits_before_reading(self):
        for maximum in (True, 0, -1, 2 * 1024 * 1024 + 1):
            with self.subTest(maximum=maximum):
                with self.assertRaisesRegex(ValueError, "limit"):
                    await read_provider_body(
                        _Response([b"{}"]),
                        maximum=maximum,
                    )

    def test_decoder_requires_canonical_utf8_and_maps_parser_failures(self):
        hostile_bodies = (
            '{"data":{}}'.encode("utf-16"),
            b'\xef\xbb\xbf{"data":{}}',
            b'{"value":' + b"1" * 5_000 + b"}",
            b'{"value":NaN}',
        )

        for body in hostile_bodies:
            with self.subTest(prefix=body[:16]):
                with self.assertRaises(ProviderJsonError):
                    decode_provider_json(body)

        self.assertEqual(
            decode_provider_json(b'{"duplicate":1,"duplicate":2}'),
            {"duplicate": 2},
        )


if __name__ == "__main__":
    unittest.main()
