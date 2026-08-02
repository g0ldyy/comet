import unittest

from comet.discovery.adapters.torrent.torrentio import TorrentioScraper
from comet.discovery.torrent_models import ScrapeRequest


class _Response:
    status = 200

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
    async def test_valid_streams_preserve_metadata(self):
        payload = {
            "streams": [
                {
                    "title": "First.Movie\n👤 20 💾 1.5 GB ⚙️ RARBG",
                    "infoHash": "A" * 40,
                    "sources": ["tracker:first"],
                },
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

    async def test_malformed_stream_fails_the_source_batch(self):
        scraper = TorrentioScraper(
            None,
            _Session({"streams": [{"infoHash": "B" * 40}]}),
            "https://torrentio.test",
        )
        request = ScrapeRequest(
            media_type="movie",
            media_id="tt123",
            media_only_id="tt123",
            title="Movie",
        )

        with self.assertRaisesRegex(ValueError, "Torrentio result"):
            await scraper.scrape(request)
