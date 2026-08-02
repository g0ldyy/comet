import unittest

import aiohttp
import pytest

from comet.usenet.easynews import (
    GENERATE_NZB_URL,
    EasynewsNzbError,
    bounded_retry_after,
    credential,
    generate_nzb,
    generated_nzb_form,
)


class _Content:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


class _Response:
    def __init__(self, status, chunks, headers=None):
        self.status = status
        self.content = _Content(chunks)
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, response):
        self.response = response

    def post(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return self.response


def _payload(**changes):
    payload = {
        "hash": "hash",
        "filename": "Movie Name",
        "extension": "mkv",
        "signature": None,
    }
    payload.update(changes)
    return payload


def test_generated_nzb_form_encodes_the_complete_signature_field_name():
    assert generated_nzb_form(_payload(signature="abc+/=|def")) == (
        b"autoNZB=1&0%26sig%3Dabc%2B%2F%3D%7Cdef=hash%7CTW92aWUgTmFtZQ%3AbWt2"
    )
    assert generated_nzb_form(_payload()) == (
        b"autoNZB=1&0=hash%7CTW92aWUgTmFtZQ%3AbWt2"
    )


def test_generated_nzb_form_quotes_provider_signatures():
    form = generated_nzb_form(_payload(signature="unsafe&other=value"))
    assert b"unsafe%26other%3Dvalue" in form


def test_easynews_text_fields_keep_character_bounds_and_require_valid_utf8():
    assert credential("é" * 512) == "é" * 512
    with pytest.raises(ValueError, match="credential"):
        credential("é" * 513)
    with pytest.raises(ValueError, match="credential"):
        credential("\ud800")
    generated_nzb_form(_payload(extension="é" * 32))
    for field, value in (
        ("hash", "\ud800"),
        ("extension", "é" * 33),
        ("signature", "\ud800"),
    ):
        with pytest.raises(ValueError):
            generated_nzb_form(_payload(**{field: value}))


class EasynewsGeneratedNzbTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_exact_account_form_and_returns_bounded_xml(self):
        document = b'<?xml version="1.0"?><nzb></nzb>'
        session = _Session(
            _Response(
                200,
                [document[:8], document[8:]],
                {
                    "Content-Type": "application/x-nzb",
                    "Content-Length": str(len(document)),
                },
            )
        )

        result = await generate_nzb(
            session,
            _payload(signature="signature"),
            "member",
            "secret",
        )

        self.assertEqual(result, document)
        self.assertEqual(session.url, GENERATE_NZB_URL)
        self.assertFalse(session.kwargs["allow_redirects"])
        self.assertEqual(session.kwargs["timeout"].total, 120)
        self.assertEqual(session.kwargs["timeout"].connect, 5)
        self.assertEqual(session.kwargs["timeout"].sock_read, 30)
        self.assertEqual(
            session.kwargs["headers"]["Content-Type"],
            "application/x-www-form-urlencoded",
        )
        self.assertTrue(session.kwargs["headers"]["Authorization"].startswith("Basic "))
        self.assertIn(b"0%26sig%3Dsignature=", session.kwargs["data"])

    async def test_generation_classifies_auth_and_rate_failures(self):
        for status, expected_code in (
            (401, "easynews_auth_failed"),
            (429, "easynews_rate_limited"),
            (503, "easynews_generate_unavailable"),
        ):
            with self.subTest(status=status):
                session = _Session(
                    _Response(
                        status,
                        [],
                        {"Retry-After": "900"},
                    )
                )
                with self.assertRaises(EasynewsNzbError) as error:
                    await generate_nzb(
                        session,
                        _payload(),
                        "member",
                        "secret",
                    )
                self.assertEqual(error.exception.code, expected_code)
                if status == 429:
                    self.assertEqual(error.exception.retry_after, 300)

    async def test_generation_maps_only_transport_failures(self):
        class TransportFailureSession:
            def post(self, *_args, **_kwargs):
                raise aiohttp.ClientConnectionError

        with self.assertRaises(EasynewsNzbError) as error:
            await generate_nzb(
                TransportFailureSession(),
                _payload(),
                "member",
                "secret",
            )
        self.assertEqual(error.exception.code, "easynews_generate_unavailable")
        self.assertTrue(error.exception.retryable)

        class ProgrammingFailureSession:
            def post(self, *_args, **_kwargs):
                raise RuntimeError("programming fault")

        with self.assertRaisesRegex(RuntimeError, "programming fault"):
            await generate_nzb(
                ProgrammingFailureSession(),
                _payload(),
                "member",
                "secret",
            )

    async def test_generation_uses_the_observed_body_length(self):
        document = b"<nzb></nzb>"
        for content_length in ("99999999999", str(len(document) + 1)):
            with self.subTest(content_length=content_length):
                session = _Session(
                    _Response(
                        200,
                        [document],
                        {
                            "Content-Type": "application/x-nzb",
                            "Content-Length": content_length,
                        },
                    )
                )
                self.assertEqual(
                    await generate_nzb(
                        session,
                        _payload(),
                        "member",
                        "secret",
                    ),
                    document,
                )

    async def test_generation_leaves_document_validation_to_the_broker(self):
        session = _Session(_Response(200, [b"opaque document"]))

        self.assertEqual(
            await generate_nzb(
                session,
                _payload(),
                "member",
                "secret",
            ),
            b"opaque document",
        )

    def test_retry_after_accepts_only_bounded_delta_seconds(self):
        self.assertEqual(bounded_retry_after("0"), 1)
        self.assertEqual(bounded_retry_after("9999"), 300)
        self.assertEqual(bounded_retry_after("١"), 1)
        for value in (True, 1.5, " 10", "9" * 11):
            with self.subTest(value=value):
                self.assertIsNone(bounded_retry_after(value))
