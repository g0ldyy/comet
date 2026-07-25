import math
import unittest
from unittest.mock import AsyncMock, patch

from comet.services import kodi_pairing


class KodiPairingTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_setup_code_rejects_invalid_ttl_before_database_access(self):
        for ttl in (None, True, 0, -1, 1.5, "300"):
            with (
                self.subTest(ttl=ttl),
                patch.object(kodi_pairing, "fetch_flag", new=AsyncMock()) as fetch,
                self.assertRaises(ValueError),
            ):
                await kodi_pairing.create_setup_code(ttl)
            fetch.assert_not_awaited()

        with (
            patch.object(kodi_pairing.time, "time", return_value=math.inf),
            patch.object(kodi_pairing, "fetch_flag", new=AsyncMock()) as fetch,
            self.assertRaisesRegex(ValueError, "finite"),
        ):
            await kodi_pairing.create_setup_code()
        fetch.assert_not_awaited()

    async def test_create_setup_code_retries_collision_and_returns_current_ttl(self):
        fetch = AsyncMock(side_effect=[False, True])
        with (
            patch.object(
                kodi_pairing.secrets,
                "token_hex",
                side_effect=["11111111", "22222222"],
            ),
            patch.object(kodi_pairing, "fetch_flag", fetch),
        ):
            result = await kodi_pairing.create_setup_code(60)

        self.assertEqual(result, ("22222222", 60))
        self.assertEqual(fetch.await_count, 2)
        self.assertTrue(fetch.await_args.kwargs["force_primary"])

    async def test_association_is_first_writer_wins_and_uses_primary(self):
        fetch = AsyncMock(return_value=True)
        with patch.object(kodi_pairing, "fetch_flag", fetch):
            self.assertTrue(
                await kodi_pairing.associate_setup_code_with_b64config(
                    "1234abcd", "config"
                )
            )

        query = fetch.await_args.args[0]
        self.assertIn("config_b64 IS NULL", query)
        self.assertTrue(fetch.await_args.kwargs["force_primary"])

    async def test_pairing_inputs_enforce_current_schema(self):
        invalid_codes = (
            None,
            False,
            1,
            "",
            "1234567",
            "123456789",
            "1234ABCD",
            "zzzzzzzz",
        )
        for code in invalid_codes:
            with self.subTest(code=code), self.assertRaises(ValueError):
                await kodi_pairing.associate_setup_code_with_b64config(code, "config")
            with self.subTest(code=code), self.assertRaises(ValueError):
                await kodi_pairing.consume_b64config_for_setup_code(code)

        with self.assertRaises(TypeError):
            await kodi_pairing.associate_setup_code_with_b64config("1234abcd", None)

    async def test_consume_requires_exact_database_row_schema(self):
        for row in (
            {"config_b64": "config"},
            None,
        ):
            with (
                self.subTest(row=row),
                patch.object(
                    kodi_pairing.database,
                    "fetch_one",
                    new=AsyncMock(return_value=row),
                ) as fetch,
            ):
                result = await kodi_pairing.consume_b64config_for_setup_code("1234abcd")
            self.assertEqual(result, None if row is None else "config")
            self.assertTrue(fetch.await_args.kwargs["force_primary"])

        for row in (
            {},
            {"config_b64": None},
            {"config_b64": 1},
            {"config_b64": "ok", "extra": 1},
        ):
            with (
                self.subTest(row=row),
                patch.object(
                    kodi_pairing.database,
                    "fetch_one",
                    new=AsyncMock(return_value=row),
                ),
                self.assertRaises(ValueError),
            ):
                await kodi_pairing.consume_b64config_for_setup_code("1234abcd")


if __name__ == "__main__":
    unittest.main()
