import base64
import unittest

import orjson

from comet.core.config_validation import config_check


class LegacyDebridConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
