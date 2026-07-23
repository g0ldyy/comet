import unittest

from comet.scrapers.models import ScrapeRequest
from comet.scrapers.zilean import ZileanScraper


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


class ZileanScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_closes_and_malformed_result_is_isolated(self):
        response = _Response(
            [
                {
                    "raw_title": "First.Movie.mkv",
                    "info_hash": "A" * 40,
                    "size": 100,
                },
                {"raw_title": "Broken.mkv", "info_hash": "B" * 40},
                {
                    "raw_title": "Second.Movie.mkv",
                    "info_hash": "C" * 40,
                    "size": "200",
                },
            ]
        )
        scraper = ZileanScraper(None, _Session(response), "https://zilean.test")
        request = ScrapeRequest(
            media_type="movie",
            media_id="tt123",
            media_only_id="tt123",
            title="Movie",
        )

        torrents = await scraper.scrape(request)

        self.assertTrue(response.exited)
        self.assertEqual(
            [torrent["infoHash"] for torrent in torrents],
            ["a" * 40, "c" * 40],
        )
        self.assertEqual([torrent["size"] for torrent in torrents], [100, 200])

    async def test_series_request_omits_only_missing_filters(self):
        cases = (
            (
                "tt11198330:1",
                None,
                {"query": "House of the Dragon", "season": 1},
            ),
            (
                "tt11198330:1:2",
                2,
                {"query": "House of the Dragon", "season": 1, "episode": 2},
            ),
        )

        for media_id, episode, expected_params in cases:
            with self.subTest(episode=episode):
                session = _Session(_Response([]))
                scraper = ZileanScraper(None, session, "https://zilean.test")
                request = ScrapeRequest(
                    media_type="series",
                    media_id=media_id,
                    media_only_id="tt11198330",
                    title="House of the Dragon",
                    season=1,
                    episode=episode,
                )

                await scraper.scrape(request)

                self.assertEqual(
                    session.requests,
                    [
                        (
                            "https://zilean.test/dmm/filtered",
                            {"params": expected_params},
                        )
                    ],
                )
