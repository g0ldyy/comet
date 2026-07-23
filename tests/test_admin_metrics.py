import unittest

from comet.api.endpoints.admin import _decode_cached_metrics


class AdminMetricsTests(unittest.TestCase):
    def test_cached_metrics_require_current_object_schema(self):
        self.assertEqual(
            _decode_cached_metrics('{"torrents":{"total":1}}'),
            {"torrents": {"total": 1}},
        )
        self.assertIsNone(_decode_cached_metrics("not-json"))
        self.assertIsNone(_decode_cached_metrics("[]"))
