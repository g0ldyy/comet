import unittest

import aiohttp

from comet.metadata.http import MetadataHttpError, get_metadata_json


class _Content:
    def __init__(self, *chunks):
        self.chunks = list(chunks)
        self.reads = 0

    async def read(self, _size):
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""


class _Response:
    def __init__(self, status, body=b'{"ok":true}', **headers):
        self.status = status
        self.content = _Content(body, b"")
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            **headers,
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class MetadataHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_uses_closed_request_and_response_contract(self):
        session = _Session(_Response(200))

        response = await get_metadata_json(
            session,
            "https://metadata.example/item",
            headers={"Authorization": "Bearer opaque"},
        )

        self.assertEqual(response.payload, {"ok": True})
        _, kwargs = session.requests[0]
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Accept"], "application/json")
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer opaque")
        self.assertEqual(kwargs["timeout"].total, 15)

    async def test_non_success_does_not_read_or_retain_provider_body(self):
        response = _Response(302, b'{"secret":"signed-value"}')

        result = await get_metadata_json(
            _Session(response),
            "https://metadata.example/item",
        )

        self.assertEqual(result.status, 302)
        self.assertIsNone(result.payload)
        self.assertEqual(response.content.reads, 0)

    async def test_invalid_or_oversized_success_is_one_safe_error(self):
        bodies = (
            b"not-json",
            b"{" + b'"value":"' + (b"x" * (2 * 1024 * 1024)) + b'"}',
        )
        for body in bodies:
            with self.subTest(size=len(body)):
                with self.assertRaisesRegex(
                    MetadataHttpError,
                    "metadata service returned an invalid response",
                ):
                    await get_metadata_json(
                        _Session(_Response(200, body)),
                        "https://metadata.example/item",
                    )

    async def test_transport_details_are_not_retained(self):
        with self.assertRaisesRegex(
            MetadataHttpError,
            "^metadata service request failed$",
        ) as raised:
            await get_metadata_json(
                _Session(error=aiohttp.ClientError("credential=do-not-retain")),
                "https://metadata.example/item",
            )

        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("credential", str(raised.exception))

    async def test_unexpected_request_failure_is_not_reclassified(self):
        with self.assertRaisesRegex(RuntimeError, "implementation failure"):
            await get_metadata_json(
                _Session(error=RuntimeError("implementation failure")),
                "https://metadata.example/item",
            )
