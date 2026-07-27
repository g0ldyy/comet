import base64
import unittest

import orjson

from comet.core.config_validation import config_check
from comet.core.models import rtn_settings_default


class ConfigCompatibilityTests(unittest.TestCase):
    def test_single_debrid_config_normalizes_without_reinstallation(self):
        encoded = base64.b64encode(
            orjson.dumps(
                {
                    "debridService": "realdebrid",
                    "debridApiKey": "existing-install-key",
                }
            )
        ).decode()

        config = config_check(encoded, strict_b64config=True)

        self.assertIsNotNone(config)
        self.assertEqual(
            config["_debridEntries"],
            [{"service": "realdebrid", "apiKey": "existing-install-key"}],
        )
        self.assertFalse(config["_enableTorrent"])

    def test_invalid_remove_ranks_under_uses_default_threshold(self):
        expected = rtn_settings_default.options.remove_ranks_under

        for invalid_value in (None, "invalid", True):
            with self.subTest(value=invalid_value):
                encoded = base64.b64encode(
                    orjson.dumps(
                        {
                            "options": {
                                "remove_ranks_under": invalid_value,
                            },
                        }
                    )
                ).decode()

                config = config_check(encoded, strict_b64config=True)

                self.assertIsNotNone(config)
                self.assertEqual(config["options"]["remove_ranks_under"], expected)
                self.assertEqual(
                    config["rtnSettings"].options["remove_ranks_under"],
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
