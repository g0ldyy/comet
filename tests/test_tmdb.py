import unittest

import orjson

from comet.metadata.tmdb import (
    TMDBApi,
    _extract_all_title_aliases,
    _extract_title_aliases,
    _extract_upcoming_release_date,
)


class _Content:
    def __init__(self, payload):
        self.body = orjson.dumps(payload)
        self.consumed = False

    async def read(self, _size):
        if self.consumed:
            return b""
        self.consumed = True
        return self.body


class _Response:
    def __init__(self, status, payload):
        self.status = status
        body = orjson.dumps(payload)
        self.headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        self.content = _Content(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class TmdbMetadataTests(unittest.TestCase):
    def test_title_alias_entries_are_collected_in_provider_order(self):
        payload = {
            "titles": [
                {"title": " First ", "iso_3166_1": "US"},
                {"title": "Second", "iso_3166_1": "us"},
                {"title": "First", "iso_3166_1": "US"},
                {"title": "Fallback", "iso_3166_1": "United States"},
                {"title": "Non-ASCII", "iso_3166_1": "ÉÉ"},
            ]
        }

        self.assertEqual(
            _extract_title_aliases(payload, "titles"),
            {
                "us": ["First", "Second", "First"],
                "ez": ["Fallback", "Non-ASCII"],
            },
        )

    def test_optional_title_alias_entries_keep_only_usable_titles(self):
        self.assertEqual(
            _extract_title_aliases(
                {
                    "titles": [
                        {"title": "Valid", "iso_3166_1": "US"},
                        None,
                        {"title": "", "iso_3166_1": "FR"},
                    ]
                },
                "titles",
            ),
            {"us": ["Valid"]},
        )

    def test_original_translated_and_alternative_titles_are_merged(self):
        config = {
            "title": "title",
            "original_title": "original_title",
            "alias_results": "titles",
        }
        payload = {
            "original_title": " La vita davanti a sé ",
            "original_language": "it",
            "origin_country": ["IT"],
            "translations": {
                "translations": [
                    {
                        "iso_3166_1": "FR",
                        "iso_639_1": "fr",
                        "data": {"title": "La Vie devant soi"},
                    },
                ]
            },
            "alternative_titles": {
                "titles": [
                    {"iso_3166_1": "US", "title": "The Life Ahead"},
                ]
            },
        }

        self.assertEqual(
            _extract_all_title_aliases(payload, config),
            {
                "original:it": ["La vita davanti a sé"],
                "lang:fr": ["La Vie devant soi"],
                "us": ["The Life Ahead"],
            },
        )

    def test_empty_optional_translations_do_not_break_alias_lookup(self):
        config = {
            "title": "title",
            "original_title": "original_title",
            "alias_results": "titles",
        }
        payload = {
            "original_title": "Movie",
            "original_language": "en",
            "translations": {
                "translations": [
                    {"iso_639_1": "fr", "data": {"title": "Film"}},
                    {"iso_639_1": "en", "data": {"title": ""}},
                    {"iso_639_1": "de", "data": {"title": ""}},
                    None,
                ]
            },
            "alternative_titles": {"titles": []},
        }

        self.assertEqual(
            _extract_all_title_aliases(payload, config),
            {"original:en": ["Movie"], "lang:fr": ["Film"]},
        )

    def test_release_date_extractor_keeps_valid_current_entries(self):
        payload = {
            "results": [
                {
                    "release_dates": [
                        {"type": 3, "release_date": "2025-01-01"},
                        {"type": 5, "release_date": "invalid"},
                        {"type": 5, "release_date": "2026-07-22T00:00:00Z"},
                        {"type": 4, "release_date": "2026-06-01T00:00:00Z"},
                    ]
                },
            ]
        }

        self.assertEqual(_extract_upcoming_release_date(payload), "2026-06-01")


class TmdbApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_alias_lookup_uses_typed_find_result_and_tv_endpoint(self):
        session = _Session(
            _Response(
                200,
                {
                    "movie_results": [{"id": 123}],
                    "tv_results": [{"id": 456}],
                },
            ),
            _Response(
                200,
                {
                    "original_name": "La casa de papel",
                    "original_language": "es",
                    "origin_country": ["ES"],
                    "translations": {"translations": []},
                    "alternative_titles": {"results": []},
                },
            ),
        )

        aliases = await TMDBApi(session).get_title_aliases("series", "tt6468322")

        self.assertEqual(aliases, {"original:es": ["La casa de papel"]})
        self.assertTrue(session.requests[0][0].endswith("external_source=imdb_id"))
        self.assertFalse(session.requests[0][1]["allow_redirects"])
        self.assertEqual(
            session.requests[0][1]["headers"]["Accept-Encoding"],
            "identity",
        )
        self.assertTrue(
            session.requests[1][0].endswith(
                "tv/456?append_to_response=alternative_titles,translations"
            )
        )

    async def test_title_alias_lookup_reports_provider_failure(self):
        session = _Session(_Response(503, {"status_message": "unavailable"}))

        aliases = await TMDBApi(session).get_title_aliases("movie", "tt0133093")

        self.assertIsNone(aliases)

    async def test_watch_provider_result_reports_availability(self):
        empty_session = _Session(_Response(200, {"results": {}}))
        populated_session = _Session(_Response(200, {"results": {"FR": {}}}))

        self.assertFalse(await TMDBApi(empty_session).has_watch_providers("123"))
        self.assertTrue(await TMDBApi(populated_session).has_watch_providers("123"))
