import unittest

from comet.scrapers.models import ScrapeRequest
from comet.scrapers.torrentio import TorrentioScraper


class _Response:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url):
        return _Response(self.payload)


class TorrentioScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_stream_does_not_discard_valid_peers(self):
        payload = {
            "streams": [
                {
                    "title": "First.Movie\n👤 20 💾 1.5 GB ⚙️ RARBG",
                    "infoHash": "A" * 40,
                    "sources": ["tracker:first"],
                },
                {"infoHash": "B" * 40},
                {
                    "title": "Second.Movie\n💾 700 MB",
                    "infoHash": "C" * 40,
                    "sources": [],
                },
            ]
        }
        scraper = TorrentioScraper(None, _Session(payload), "https://torrentio.test")
        request = ScrapeRequest(
            media_type="movie",
            media_id="tt123",
            media_only_id="tt123",
            title="Movie",
        )

        torrents = await scraper.scrape(request)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual(
            [torrent["infoHash"] for torrent in torrents], ["a" * 40, "c" * 40]
        )
