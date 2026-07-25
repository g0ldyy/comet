import unittest

from comet.metadata.tmdb import (
    TMDBApi,
    _extract_all_title_aliases,
    _extract_title_aliases,
    _extract_tmdb_id,
    _extract_upcoming_release_date,
)


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, headers):
        self.requests.append((url, headers))
        return self.responses.pop(0)


class TmdbMetadataTests(unittest.TestCase):
    def test_tmdb_id_extractor_isolates_malformed_results(self):
        payload = {
            "movie_results": [None, {"id": True}, {"id": "12"}],
            "tv_results": [{}, {"id": 456}],
        }

        self.assertEqual(_extract_tmdb_id(payload), "456")
        self.assertEqual(_extract_tmdb_id(payload, "series"), "456")
        self.assertIsNone(_extract_tmdb_id(payload, "movie"))
        self.assertIsNone(_extract_tmdb_id([]))

    def test_title_aliases_are_normalized_and_deduplicated_in_provider_order(self):
        payload = {
            "titles": [
                {"title": " First ", "iso_3166_1": "US"},
                None,
                {"title": "Second", "iso_3166_1": "us"},
                {"title": "First", "iso_3166_1": "US"},
                {"title": "Fallback", "iso_3166_1": "United States"},
                {"title": "Non-ASCII", "iso_3166_1": "ÉÉ"},
                {"title": " ", "iso_3166_1": "GB"},
                {"title": 123, "iso_3166_1": "GB"},
            ]
        }

        self.assertEqual(
            _extract_title_aliases(payload, "titles"),
            {
                "us": ["First", "Second"],
                "ez": ["Fallback", "Non-ASCII"],
            },
        )
        self.assertEqual(_extract_title_aliases({"titles": {}}, "titles"), {})

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
                    None,
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

    def test_release_date_extractor_keeps_valid_current_entries(self):
        payload = {
            "results": [
                None,
                {"release_dates": "invalid"},
                {
                    "release_dates": [
                        {"type": 4, "release_date": ["invalid"]},
                        {"type": 3, "release_date": "2025-01-01"},
                        {"type": 5, "release_date": "invalid"},
                        {"type": 5, "release_date": "2026-07-22T00:00:00Z"},
                        {"type": 4, "release_date": "2026-06-01T00:00:00Z"},
                    ]
                },
            ]
        }

        self.assertEqual(_extract_upcoming_release_date(payload), "2026-06-01")
        self.assertIsNone(_extract_upcoming_release_date({"results": {}}))


class TmdbApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_type_lookup_ignores_malformed_find_results(self):
        session = _Session(
            _Response(
                200,
                {
                    "movie_results": [{"id": True}, {"id": "invalid"}],
                    "tv_results": [{"id": 456}],
                },
            )
        )

        media_type = await TMDBApi(session).get_media_type_from_imdb("tt6468322")

        self.assertEqual(media_type, "series")

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
        self.assertTrue(
            session.requests[1][0].endswith(
                "tv/456?append_to_response=alternative_titles,translations"
            )
        )

    async def test_title_alias_lookup_reports_provider_failure(self):
        session = _Session(_Response(503, {"status_message": "unavailable"}))

        aliases = await TMDBApi(session).get_title_aliases("movie", "tt0133093")

        self.assertIsNone(aliases)
