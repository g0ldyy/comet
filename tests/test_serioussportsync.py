import unittest
from unittest.mock import patch

from comet.metadata.serioussportsync import (
    SeriousSportSyncResolverError,
    build_event_search_titles,
    is_serioussportsync_event_id,
    resolve_serioussportsync_event,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.requested_url = None

    def get(self, url):
        self.requested_url = url
        return _Response(self.payload)


class SeriousSportSyncTests(unittest.IsolatedAsyncioTestCase):
    def test_recognises_only_movie_event_ids(self):
        self.assertTrue(is_serioussportsync_event_id("movie", "nba:2371750"))
        self.assertFalse(is_serioussportsync_event_id("series", "nba:2371750"))
        self.assertFalse(is_serioussportsync_event_id("movie", "tt1234567"))
        self.assertFalse(is_serioussportsync_event_id("movie", "kitsu:123"))

    def test_builds_separator_free_fixture_queries(self):
        self.assertEqual(
            build_event_search_titles(
                "Dallas Mavericks vs Chicago Bulls",
                ["Chicago Bulls @ Dallas Mavericks"],
            ),
            (
                "Dallas Mavericks vs Chicago Bulls",
                "Dallas Mavericks Chicago Bulls",
                "Chicago Bulls @ Dallas Mavericks",
                "Chicago Bulls Dallas Mavericks",
            ),
        )

    def test_builds_base_query_for_split_day_events(self):
        self.assertEqual(
            build_event_search_titles(
                "SummerSlam Sunday",
                ["WWE SummerSlam Sunday", "WWE SummerSlam Sunday 2025"],
            ),
            (
                "SummerSlam Sunday",
                "SummerSlam",
                "WWE SummerSlam Sunday",
                "WWE SummerSlam",
                "WWE SummerSlam Sunday 2025",
            ),
        )

    async def test_resolves_token_scoped_search_context(self):
        session = _Session(
            {
                "title": "Dallas Mavericks vs Chicago Bulls",
                "searchTitles": ["NBA Dallas Mavericks Chicago Bulls"],
                "date": "2026-04-12",
                "year": 2026,
                "country": "US",
            }
        )
        with patch(
            "comet.metadata.serioussportsync.settings.SERIOUSSPORTSYNC_ALLOWED_HOSTS",
            "serioussportsync:7000",
        ):
            event = await resolve_serioussportsync_event(
                session,
                "http://serioussportsync:7000/user-token/manifest.json",
                "movie",
                "nba:2371750",
            )

        self.assertEqual(
            session.requested_url,
            "http://serioussportsync:7000/user-token/search-context/movie/"
            "nba%3A2371750.json",
        )
        self.assertEqual(event.date, "2026-04-12")
        self.assertIn("Dallas Mavericks Chicago Bulls", event.search_titles)

    async def test_rejects_manifest_hosts_not_allowed_by_operator(self):
        session = _Session({})
        with patch(
            "comet.metadata.serioussportsync.settings.SERIOUSSPORTSYNC_ALLOWED_HOSTS",
            "serioussportsync:7000",
        ):
            with self.assertRaises(SeriousSportSyncResolverError):
                await resolve_serioussportsync_event(
                    session,
                    "http://127.0.0.1:7000/token/manifest.json",
                    "movie",
                    "nba:2371750",
                )


if __name__ == "__main__":
    unittest.main()
