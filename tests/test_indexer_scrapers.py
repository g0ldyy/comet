import unittest
from unittest.mock import AsyncMock, patch

from comet.scrapers.jackett import JackettScraper
from comet.scrapers.models import ScrapeRequest
from comet.scrapers.prowlarr import ProwlarrScraper
from comet.scrapers.stremthru import StremthruScraper


REQUEST = ScrapeRequest(
    media_type="movie",
    media_id="tt123",
    media_only_id="tt123",
    title="Movie",
)


def _torrent(info_hash):
    return {
        "title": info_hash,
        "infoHash": info_hash,
        "fileIndex": None,
        "seeders": None,
        "size": None,
        "tracker": "indexer",
        "sources": [],
    }


class _StremthruResponse:
    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def text(self):
        return self.body


class _StremthruSession:
    def __init__(self, body):
        self.body = body

    def get(self, _):
        return _StremthruResponse(self.body)


class IndexerScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_indexers_search_every_localized_episode_title(self):
        request = ScrapeRequest(
            media_type="series",
            media_id="tt123:1:2",
            media_only_id="tt123",
            title="English",
            season=1,
            episode=2,
            search_titles=("English", "Italiano"),
        )
        expected_queries = {
            "English",
            "English S01",
            "English S01E02",
            "Italiano",
            "Italiano S01",
            "Italiano S01E02",
        }

        jackett = JackettScraper(None, None, "https://jackett.test")
        jackett.fetch_jackett_results = AsyncMock(return_value=[])
        with patch("comet.scrapers.jackett.settings.JACKETT_INDEXERS", ["indexer"]):
            await jackett.scrape(request)
        self.assertEqual(
            {call.args[1] for call in jackett.fetch_jackett_results.await_args_list},
            expected_queries,
        )

        prowlarr = ProwlarrScraper(None, None, "https://prowlarr.test")
        prowlarr._fetch_search_results = AsyncMock(return_value=[])
        with patch("comet.scrapers.prowlarr.settings.PROWLARR_INDEXERS", ["1"]):
            await prowlarr.scrape(request)
        self.assertEqual(
            {call.args[0] for call in prowlarr._fetch_search_results.await_args_list},
            expected_queries,
        )

    async def test_jackett_isolates_malformed_and_failed_results(self):
        results = [
            {"Details": "first", "token": "first"},
            None,
            {"token": "missing-details"},
            {"Details": "failed", "token": "failed"},
            {"Details": "second", "token": "second"},
        ]

        async def process(result, media_id, season):
            del media_id, season
            if result["token"] == "failed":
                raise RuntimeError("bad torrent payload")
            return [None, _torrent(result["token"])]

        scraper = JackettScraper(None, None, "https://jackett.test")
        scraper.fetch_jackett_results = AsyncMock(return_value=results)
        scraper.process_torrent = AsyncMock(side_effect=process)
        with patch("comet.scrapers.jackett.settings.JACKETT_INDEXERS", ["indexer"]):
            torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["infoHash"] for torrent in torrents], ["first", "second"]
        )

    async def test_prowlarr_isolates_malformed_search_results(self):
        results = [
            {"infoUrl": "first", "token": "first"},
            None,
            {"token": "missing-info-url"},
            {"infoUrl": "second", "token": "second"},
        ]

        async def process(result, media_id, season):
            del media_id, season
            return [None, _torrent(result["token"])]

        scraper = ProwlarrScraper(None, None, "https://prowlarr.test")
        scraper._fetch_search_results = AsyncMock(return_value=results)
        scraper.process_torrent = AsyncMock(side_effect=process)
        with patch("comet.scrapers.prowlarr.settings.PROWLARR_INDEXERS", ["1"]):
            torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["infoHash"] for torrent in torrents], ["first", "second"]
        )

    async def test_stremthru_discards_invalid_magnets_before_filtering(self):
        xml = """
        <rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
          <channel>
            <item>
              <title>Invalid Magnet</title>
              <torznab:attr name="size" value="1000" />
              <torznab:attr name="infohash" value="1111111111111111111111111111111111111111" />
            </item>
            <item>
              <title>Obsession.2026.1080p.WEB-DL.x264</title>
              <torznab:attr name="size" value="2000" />
              <torznab:attr name="infohash" value="2222222222222222222222222222222222222222" />
              <torznab:attr name="indexername" value="Knaben" />
            </item>
            <item>
              <title>Obsession.2026.720p.WEB-DL.x264</title>
              <torznab:attr name="size" value="1000" />
              <torznab:attr name="infohash" value="3333333333333333333333333333333333333333" />
            </item>
          </channel>
        </rss>
        """
        scraper = StremthruScraper(None, _StremthruSession(xml), "https://test")

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [(torrent["infoHash"], torrent["tracker"]) for torrent in torrents],
            [("2" * 40, "StremThru|Knaben"), ("3" * 40, "StremThru")],
        )
