import unittest
from types import SimpleNamespace

from comet.utils.cache import CacheControl, check_etag_match


class HttpCacheContractTests(unittest.TestCase):
    @staticmethod
    def _request(if_none_match: str):
        return SimpleNamespace(headers={"If-None-Match": if_none_match})

    def test_etag_list_preserves_commas_inside_opaque_tags(self):
        request = self._request('"unrelated", W/"current,value"')

        self.assertTrue(check_etag_match(request, '"current,value"'))

    def test_etag_comparison_is_weak_and_accepts_standalone_wildcard(self):
        self.assertTrue(check_etag_match(self._request('W/"current"'), '"current"'))
        self.assertTrue(check_etag_match(self._request(" * "), 'W/"current"'))

    def test_malformed_etag_lists_do_not_match(self):
        malformed_values = (
            '"current",',
            '*, "current"',
            '"current" garbage',
            "W/ current",
        )

        for value in malformed_values:
            with self.subTest(value=value):
                self.assertFalse(check_etag_match(self._request(value), '"current"'))

    def test_cache_durations_require_current_non_negative_integer_shape(self):
        setters = (
            CacheControl.max_age,
            CacheControl.s_maxage,
            CacheControl.stale_while_revalidate,
            CacheControl.stale_if_error,
        )

        for setter in setters:
            for value in (True, -1, 1.5, "1", None):
                with self.subTest(setter=setter.__name__, value=value):
                    with self.assertRaises(ValueError):
                        setter(CacheControl(), value)

        self.assertEqual(
            CacheControl().public().max_age(0).s_maxage(30).build(),
            "public, max-age=0, s-maxage=30",
        )


if __name__ == "__main__":
    unittest.main()
