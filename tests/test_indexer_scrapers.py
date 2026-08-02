import asyncio
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, patch

from comet.discovery.adapters.torrent.bitmagnet import BitmagnetScraper
from comet.discovery.adapters.torrent.dmm import DMMScraper
from comet.discovery.adapters.torrent.jackett import JackettScraper
from comet.discovery.adapters.torrent.prowlarr import ProwlarrScraper
from comet.discovery.adapters.torrent.stremthru import StremthruScraper
from comet.discovery.torrent_models import ScrapeRequest
from comet.services.indexer_manager import (
    MAX_INDEXER_RESPONSE_BYTES,
    indexer_manager,
)

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
    status = 200

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


class _IndexerResponse:
    status = 200

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def read(self):
        return self.body


class _IndexerSession:
    def __init__(self, body):
        self.response = _IndexerResponse(body)
        self.kwargs = None

    def get(self, _url, **kwargs):
        self.kwargs = kwargs
        return self.response


class IndexerScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_indexer_scrapers_use_the_bounded_response_contract(self):
        jackett_session = _IndexerSession(b'{"Results":[]}')
        prowlarr_session = _IndexerSession(b"[]")

        self.assertEqual(
            await JackettScraper(
                None, jackett_session, "https://jackett.test"
            ).fetch_jackett_results("indexer", "query"),
            [],
        )
        with patch.object(indexer_manager, "active_prowlarr_config", ["1"]):
            self.assertEqual(
                await ProwlarrScraper(
                    None, prowlarr_session, "https://prowlarr.test"
                )._fetch_search_results("query"),
                [],
            )

        self.assertEqual(
            jackett_session.kwargs["maximum_body_bytes"],
            MAX_INDEXER_RESPONSE_BYTES,
        )
        self.assertEqual(
            prowlarr_session.kwargs["maximum_body_bytes"],
            MAX_INDEXER_RESPONSE_BYTES,
        )

    def test_bitmagnet_rejects_an_incomplete_result(self):
        root = ET.fromstring(
            """
            <rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
              <channel><item><title>Movie</title></item></channel>
            </rss>
            """
        )
        with self.assertRaisesRegex(ValueError, "Bitmagnet result"):
            BitmagnetScraper(None, None, "https://bitmagnet.test").parse_items(root)

    async def test_dmm_storage_failures_propagate(self):
        with (
            patch(
                "comet.discovery.adapters.torrent.dmm.settings.DMM_INGEST_ENABLED",
                True,
            ),
            patch(
                "comet.discovery.adapters.torrent.dmm.database.fetch_all",
                AsyncMock(side_effect=RuntimeError("database unavailable")),
            ),
            self.assertRaisesRegex(RuntimeError, "database unavailable"),
        ):
            await DMMScraper(None, None).scrape(REQUEST)

    async def test_indexer_downloads_allow_only_the_configured_private_origin(self):
        jackett_result = {
            "Title": "Torrent",
            "Seeders": None,
            "Size": 1,
            "Tracker": "indexer",
            "Link": "http://jackett.internal/download/1",
        }
        jackett = JackettScraper(None, object(), "http://jackett.internal:9117")
        with patch(
            "comet.discovery.adapters.torrent.jackett.download_torrent",
            new=AsyncMock(return_value=(None, None, None)),
        ) as download:
            self.assertEqual(
                await jackett.process_torrent(jackett_result, "tt123", None),
                [],
            )
        download.assert_awaited_once_with(
            jackett.session,
            jackett_result["Link"],
            allowed_private_origins=frozenset({"http://jackett.internal:9117"}),
        )

        prowlarr_result = {
            "title": "Torrent",
            "seeders": None,
            "size": 1,
            "indexer": "indexer",
            "downloadUrl": "http://prowlarr.internal/download/1",
        }
        prowlarr = ProwlarrScraper(None, object(), "http://prowlarr.internal:9696")
        with patch(
            "comet.discovery.adapters.torrent.prowlarr.download_torrent",
            new=AsyncMock(return_value=(None, None, None)),
        ) as download:
            self.assertEqual(
                await prowlarr.process_torrent(prowlarr_result, "tt123", None),
                [],
            )
        download.assert_awaited_once_with(
            prowlarr.session,
            prowlarr_result["downloadUrl"],
            allowed_private_origins=frozenset({"http://prowlarr.internal:9696"}),
        )

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
        with patch.object(indexer_manager, "active_jackett_config", ["indexer"]):
            await jackett.scrape(request)
        self.assertEqual(
            {call.args[1] for call in jackett.fetch_jackett_results.await_args_list},
            expected_queries,
        )

        prowlarr = ProwlarrScraper(None, None, "https://prowlarr.test")
        prowlarr._fetch_search_results = AsyncMock(return_value=[])
        with patch.object(indexer_manager, "active_prowlarr_config", ["1"]):
            await prowlarr.scrape(request)
        self.assertEqual(
            {call.args[0] for call in prowlarr._fetch_search_results.await_args_list},
            expected_queries,
        )

    async def test_jackett_processing_failure_fails_the_source(self):
        results = [
            {"token": "first"},
            {"token": "failed"},
        ]

        async def process(result, media_id, season):
            del media_id, season
            if result["token"] == "failed":
                raise RuntimeError("bad torrent payload")
            return [_torrent(result["token"])]

        scraper = JackettScraper(None, None, "https://jackett.test")
        scraper.fetch_jackett_results = AsyncMock(return_value=results)
        scraper.process_torrent = AsyncMock(side_effect=process)
        with patch.object(indexer_manager, "active_jackett_config", ["indexer"]):
            with self.assertRaisesRegex(RuntimeError, "bad torrent payload"):
                await scraper.scrape(REQUEST)

    async def test_prowlarr_does_not_require_an_unconsumed_info_url(self):
        results = [
            {"token": "first"},
            {"token": "second"},
        ]

        async def process(result, media_id, season):
            del media_id, season
            return [_torrent(result["token"])]

        scraper = ProwlarrScraper(None, None, "https://prowlarr.test")
        scraper._fetch_search_results = AsyncMock(return_value=results)
        scraper.process_torrent = AsyncMock(side_effect=process)
        with patch.object(indexer_manager, "active_prowlarr_config", ["1"]):
            torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["infoHash"] for torrent in torrents], ["first", "second"]
        )

    async def test_prowlarr_partial_query_failure_fails_the_source(self):
        request = REQUEST.model_copy(update={"search_titles": ("Working", "Failed")})

        async def search(query):
            if query == "Failed":
                raise RuntimeError("transport failed")
            return [{"infoUrl": "working"}]

        scraper = ProwlarrScraper(None, None, "https://prowlarr.test")
        scraper._fetch_search_results = AsyncMock(side_effect=search)
        scraper.process_torrent = AsyncMock(return_value=[_torrent("working")])
        with (
            patch.object(indexer_manager, "active_prowlarr_config", ["1"]),
            self.assertRaisesRegex(RuntimeError, "transport failed"),
        ):
            await scraper.scrape(request)

    async def test_indexer_request_and_result_processing_concurrency_is_bounded(self):
        active_fetches = 0
        peak_fetches = 0
        active_results = 0
        peak_results = 0

        async def fetch(indexer, query):
            nonlocal active_fetches, peak_fetches
            active_fetches += 1
            peak_fetches = max(peak_fetches, active_fetches)
            await asyncio.sleep(0)
            active_fetches -= 1
            return [
                {
                    "Details": f"{indexer}-{query}-{index}",
                    "token": f"{indexer}-{query}-{index}",
                }
                for index in range(2)
            ]

        async def process(result, media_id, season):
            nonlocal active_results, peak_results
            del media_id, season
            active_results += 1
            peak_results = max(peak_results, active_results)
            await asyncio.sleep(0)
            active_results -= 1
            return [_torrent(result["token"])]

        request = REQUEST.model_copy(
            update={"search_titles": tuple(f"Title {index}" for index in range(8))}
        )
        scraper = JackettScraper(None, None, "https://jackett.test")
        scraper.fetch_jackett_results = AsyncMock(side_effect=fetch)
        scraper.process_torrent = AsyncMock(side_effect=process)
        with patch.object(
            indexer_manager,
            "active_jackett_config",
            [f"indexer-{index}" for index in range(8)],
        ):
            torrents = await scraper.scrape(request)

        self.assertEqual(len(torrents), 128)
        self.assertEqual(peak_fetches, 16)
        self.assertEqual(peak_results, 16)

    async def test_indexers_stop_emitting_batches_at_aggregate_result_cap(self):
        oversized_response = [{}] * 10_000
        request = ScrapeRequest(
            media_type="series",
            media_id="tt123:1:2",
            media_only_id="tt123",
            title="Canonical",
            season=1,
            episode=2,
            search_titles=tuple(f"Title {index}" for index in range(8)),
        )

        jackett = JackettScraper(None, None, "https://jackett.test")
        jackett.fetch_jackett_results = AsyncMock(return_value=oversized_response)
        jackett.process_torrent = AsyncMock(return_value=[])
        with patch.object(
            indexer_manager,
            "active_jackett_config",
            [f"indexer-{index}" for index in range(64)],
        ):
            self.assertEqual(await jackett.scrape(request), [])
        self.assertEqual(jackett.fetch_jackett_results.await_count, 16)

        prowlarr = ProwlarrScraper(None, None, "https://prowlarr.test")
        prowlarr._fetch_search_results = AsyncMock(return_value=oversized_response)
        prowlarr.process_torrent = AsyncMock(return_value=[])
        with patch.object(indexer_manager, "active_prowlarr_config", ["1"]):
            self.assertEqual(await prowlarr.scrape(request), [])
        self.assertEqual(prowlarr._fetch_search_results.await_count, 16)

    async def test_stremthru_preserves_opaque_titles(self):
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
            [
                ("1" * 40, "StremThru"),
                ("2" * 40, "StremThru|Knaben"),
                ("3" * 40, "StremThru"),
            ],
        )
