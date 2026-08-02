import importlib.util
import json
import unittest
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parents[1] / "kodi" / "plugin.video.comet" / "lib" / "http_json.py"
)
SPEC = importlib.util.spec_from_file_location("comet_kodi_http_json", MODULE_PATH)
http_json = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(http_json)


class _Raw:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def read(self, *, decode_content):
        self.calls.append(decode_content)
        return self.payload


class _Response:
    def __init__(self, payload, *, status=200, headers=None):
        self.status_code = status
        self.raw = _Raw(payload)
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request failed")

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


class KodiHttpTests(unittest.TestCase):
    def test_request_uses_one_nonredirecting_identity_response(self):
        payload = json.dumps({"streams": []}).encode()
        response = _Response(
            payload,
            headers={"Content-Length": str(len(payload))},
        )
        session = _Session(response)

        result = http_json.request_json(
            session,
            "GET",
            "https://comet.test/config/stream/movie/tt1234567.json",
            timeout=20,
        )

        self.assertEqual(result, {"streams": []})
        self.assertEqual(
            session.calls,
            [
                (
                    "GET",
                    "https://comet.test/config/stream/movie/tt1234567.json",
                    {
                        "timeout": 20,
                        "allow_redirects": False,
                        "stream": True,
                        "headers": {
                            "Accept": "application/json",
                            "Accept-Encoding": "identity",
                        },
                    },
                )
            ],
        )
        self.assertEqual(response.raw.calls, [True])
        self.assertTrue(response.closed)

    def test_response_boundary_rejects_redirects_encoding_and_ambiguous_shape(self):
        rejected = (
            _Response(b"{}", status=302),
            _Response(b"[]"),
            _Response(b'{"value": NaN}'),
            _Response(b"{", headers={"Content-Length": "1"}),
        )
        for response in rejected:
            with self.subTest(status=response.status_code, headers=response.headers):
                with self.assertRaises(http_json.JsonHttpError):
                    http_json.request_json(
                        _Session(response),
                        "GET",
                        "https://comet.test/manifest.json",
                        timeout=20,
                    )
                self.assertTrue(response.closed)
        for response in (
            _Response(b"{}", headers={"Content-Encoding": "gzip"}),
            _Response(b'{"value": 1, "value": 2}'),
        ):
            self.assertIsInstance(
                http_json.request_json(
                    _Session(response),
                    "GET",
                    "https://comet.test/manifest.json",
                    timeout=20,
                ),
                dict,
            )

    def test_url_and_api_prefix_domains_reject_pivots(self):
        for url in (
            "",
            "file:///tmp/video",
            "https://member:secret@comet.test/manifest.json",
            "https://comet.test/manifest.json#fragment",
            "https://comet.test/\nredirect",
        ):
            with self.subTest(url=url[:80]), self.assertRaises(http_json.JsonHttpError):
                http_json.validate_http_url(url)

        self.assertEqual(http_json.normalize_api_prefix("/s/token/"), "s/token")
        for prefix in ("https://attacker.test", "../escape", "s//token", "\n"):
            with (
                self.subTest(prefix=prefix),
                self.assertRaises(http_json.JsonHttpError),
            ):
                http_json.normalize_api_prefix(prefix)

    def test_origin_label_never_contains_path_or_userinfo(self):
        self.assertEqual(
            http_json.origin_label("https://comet.test/private/config/stream"),
            "comet.test",
        )
        self.assertEqual(
            http_json.origin_label("https://member:secret@comet.test/private"),
            "configured service",
        )


if __name__ == "__main__":
    unittest.main()
