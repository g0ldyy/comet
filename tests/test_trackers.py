import unittest

from comet.api.endpoints.stream import _tracker_sources
from comet.services.trackers import normalize_trackers


class NormalizeTrackersTests(unittest.TestCase):
    def test_strips_prefix_and_deduplicates_in_order(self):
        self.assertEqual(
            normalize_trackers(
                [
                    " udp://a.example:1337/announce ",
                    "tracker:udp://a.example:1337/announce",
                    "tracker:https://b.example/announce",
                    "http://c.example/announce",
                ]
            ),
            [
                "udp://a.example:1337/announce",
                "https://b.example/announce",
                "http://c.example/announce",
            ],
        )

    def test_drops_dht_and_unusable_entries(self):
        self.assertEqual(
            normalize_trackers(
                [
                    "dht:" + "a" * 40,
                    "",
                    "garbage",
                    "wss://tracker.example/ws",
                    "udp://",
                    "udp://[::1",
                ]
            ),
            [],
        )


class StremioTrackerSourcesTests(unittest.TestCase):
    def test_prefixes_trackers_for_stremio(self):
        self.assertEqual(
            _tracker_sources(
                ("udp://a.example:1/announce", "tracker:udp://a.example:1/announce")
            ),
            ("tracker:udp://a.example:1/announce",),
        )


if __name__ == "__main__":
    unittest.main()
