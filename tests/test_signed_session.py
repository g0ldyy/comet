import base64
import unittest
from unittest.mock import patch

from comet.utils.signed_session import (
    derive_session_secret,
    encode_signed_session,
    verify_signed_session,
)


class SignedSessionContractTests(unittest.TestCase):
    def setUp(self):
        self.secret = derive_session_secret("current-password", "test-scope")

    def test_current_token_round_trip_and_expiration(self):
        with patch("comet.utils.signed_session.time.time", return_value=1_700_000_000):
            token = encode_signed_session(self.secret, 60)
            self.assertTrue(verify_signed_session(token, self.secret))

        with patch("comet.utils.signed_session.time.time", return_value=1_700_000_060):
            self.assertFalse(verify_signed_session(token, self.secret))

    def test_token_parser_rejects_oversize_and_non_current_fields(self):
        invalid_raw_tokens = (
            b"1700000060:short:0" * 8,
            b"01700000060:0123456789abcdef:" + (b"0" * 64),
            b"1700000060:0123456789abcdeg:" + (b"0" * 64),
            b"1700000060:0123456789abcdef:" + (b"G" * 64),
        )

        for raw_token in invalid_raw_tokens:
            token = base64.urlsafe_b64encode(raw_token).decode().rstrip("=")
            with self.subTest(raw_token=raw_token):
                self.assertFalse(verify_signed_session(token, self.secret))

        self.assertFalse(verify_signed_session("A" * 161, self.secret))
        self.assertFalse(verify_signed_session("not+urlsafe", self.secret))

    def test_secret_and_ttl_inputs_are_exact(self):
        invalid_secret_inputs = (b"short", bytearray(32), "x" * 32, None)
        for secret in invalid_secret_inputs:
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                encode_signed_session(secret, 60)
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                verify_signed_session("token", secret)

        for ttl in (True, 59, 1.5, "60", None):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                encode_signed_session(self.secret, ttl)

        for password, scope in (("", "scope"), (None, "scope"), ("password", "")):
            with (
                self.subTest(password=password, scope=scope),
                self.assertRaises(ValueError),
            ):
                derive_session_secret(password, scope)


if __name__ == "__main__":
    unittest.main()
