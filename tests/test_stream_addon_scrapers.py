import unittest
from unittest.mock import AsyncMock, patch

from comet.discovery.adapters.torrent.aiostreams import AiostreamsScraper
from comet.discovery.adapters.torrent.comet import CometScraper
from comet.discovery.adapters.torrent.debridio import DebridioScraper
from comet.discovery.adapters.torrent.jackettio import JackettioScraper
from comet.discovery.adapters.torrent.mediafusion import MediaFusionScraper
from comet.discovery.adapters.torrent.peerflix import PeerflixScraper
from comet.discovery.adapters.torrent.seadex import SeaDexScraper
from comet.discovery.adapters.torrent.torrentsdb import TorrentsDBScraper
from comet.discovery.torrent_models import ScrapeRequest


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return _Response(self.payload, self.status)


REQUEST = ScrapeRequest(
    media_type="movie",
    media_id="tt123",
    media_only_id="tt123",
    title="Movie",
)


class StreamAddonScraperTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_payload_failures_propagate(self):
        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            await PeerflixScraper(None, _Session({}, status=404)).scrape(REQUEST)
        with self.assertRaises(TypeError):
            await CometScraper(None, _Session([]), "https://comet.test").scrape(REQUEST)

    def test_stream_parsers_expose_missing_consumed_fields(self):
        cases = (
            (MediaFusionScraper._parse_stream, {"infoHash": "a" * 40}),
            (AiostreamsScraper._parse_stream, {"infoHash": "a" * 40}),
            (TorrentsDBScraper._parse_stream, None),
            (JackettioScraper._parse_stream, {"title": "Movie"}),
            (DebridioScraper._parse_stream, {"title": "Movie", "url": None}),
        )
        for parser, stream in cases:
            with (
                self.subTest(parser=parser.__qualname__),
                self.assertRaises((AttributeError, KeyError, TypeError)),
            ):
                parser(stream)

    async def test_mediafusion_parses_valid_streams(self):
        payload = {
            "streams": [
                {
                    "description": "📂 First.Movie/\n👤 12\n🔗 RARBG",
                    "infoHash": "A" * 40,
                    "behaviorHints": {"videoSize": 1_000},
                    "sources": [],
                },
                {
                    "description": "📂 Second.Movie/\n👤 3\n🔗 YTS",
                    "infoHash": "C" * 40,
                    "behaviorHints": {"videoSize": 2_000},
                    "sources": ["tracker:second"],
                },
            ]
        }
        session = _Session(payload)
        scraper = MediaFusionScraper(None, session, "https://mf.test", "secret")

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual([torrent["seeders"] for torrent in torrents], [12, 3])
        self.assertIn("encoded_user_data", session.requests[0][1]["headers"])

    async def test_aiostreams_parses_valid_streams(self):
        payload = {
            "data": {
                "results": [
                    {
                        "filename": "First.Movie",
                        "infoHash": "a" * 40,
                        "size": 1_000,
                        "sources": [],
                    },
                    {
                        "filename": "Second.Movie",
                        "infoHash": "c" * 40,
                        "size": 2_000,
                        "indexer": "Usenet",
                        "sources": ["tracker:second"],
                    },
                ]
            }
        }
        session = _Session(payload)
        scraper = AiostreamsScraper(
            None,
            session,
            "https://aio.test",
            "user:password",
        )

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual(
            [torrent["tracker"] for torrent in torrents],
            ["AIOStreams", "AIOStreams|Usenet"],
        )
        self.assertEqual(
            session.requests[0][1]["headers"]["Authorization"],
            "Basic dXNlcjpwYXNzd29yZA==",
        )

    async def test_torrentsdb_parses_valid_streams(self):
        payload = {
            "streams": [
                {
                    "title": "First.Movie\n👤 12 💾 1 GB ⚙️ RARBG",
                    "infoHash": "A" * 40,
                    "sources": [],
                },
                {
                    "title": "Second.Movie\n💾 2 GB",
                    "infoHash": "C" * 40,
                    "sources": ["tracker:second"],
                },
            ]
        }
        scraper = TorrentsDBScraper(None, _Session(payload))

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual([torrent["seeders"] for torrent in torrents], [12, None])

    async def test_peerflix_keeps_streams_without_optional_metadata(self):
        payload = {
            "streams": [
                {
                    "description": "First.Movie\n🌐RARBG",
                    "infoHash": "A" * 40,
                    "fileIdx": 1,
                    "sources": [],
                },
                {"description": "Broken.Movie", "infoHash": "B" * 40},
                {
                    "description": "Second.Movie",
                    "infoHash": "C" * 40,
                    "fileIdx": 2,
                    "sources": ["tracker:second"],
                },
            ]
        }
        scraper = PeerflixScraper(None, _Session(payload))

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents],
            ["First.Movie", "Broken.Movie", "Second.Movie"],
        )
        self.assertEqual([torrent["fileIndex"] for torrent in torrents], [1, None, 2])

    async def test_comet_keeps_unparseable_optional_metadata(self):
        payload = {
            "streams": [
                {
                    "description": "📄 First.Movie\n👤 12 seeders\n🔎 RARBG",
                    "infoHash": "A" * 40,
                    "behaviorHints": {"videoSize": 1_000},
                    "sources": [],
                },
                {
                    "description": "📄 Broken.Movie\n👤 unknown seeders",
                    "infoHash": "B" * 40,
                    "behaviorHints": {},
                },
                {
                    "description": "📄 Second.Movie",
                    "infoHash": "C" * 40,
                    "behaviorHints": {"videoSize": 2_000},
                    "sources": ["tracker:second"],
                },
            ]
        }
        scraper = CometScraper(None, _Session(payload), "https://comet.test")

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents],
            ["First.Movie", "Broken.Movie", "Second.Movie"],
        )
        self.assertEqual([torrent["seeders"] for torrent in torrents], [12, None, None])

    async def test_jackettio_parses_valid_streams(self):
        payload = {
            "streams": [
                {
                    "title": "First.Movie\n💾 1 GB 👥 12 ⚙️ RARBG",
                    "infoHash": "a" * 40,
                },
                {"title": "Second.Movie", "infoHash": "c" * 40},
            ]
        }
        scraper = JackettioScraper(None, _Session(payload), "https://jackettio.test")

        torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual([torrent["seeders"] for torrent in torrents], [12, None])

    async def test_seadex_skips_only_explicitly_redacted_torrents(self):
        payload = {
            "items": [
                {"expand": {"trs": [{"infoHash": "<redacted>"}]}},
                {
                    "expand": {
                        "trs": [
                            {
                                "infoHash": "a" * 40,
                                "files": [
                                    {"name": "First.Movie", "length": 1_000},
                                ],
                            },
                            {
                                "infoHash": "c" * 40,
                                "files": [{"name": "Second.Movie", "length": 2_000}],
                            },
                        ]
                    }
                },
            ]
        }
        scraper = SeaDexScraper(None, _Session(payload))
        with (
            patch(
                "comet.discovery.adapters.torrent.seadex.anime_mapper.is_loaded",
                return_value=True,
            ),
            patch(
                "comet.discovery.adapters.torrent.seadex.anime_mapper.get_anilist_id",
                new=AsyncMock(return_value=123),
            ),
        ):
            torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual([torrent["fileIndex"] for torrent in torrents], [0, 0])

    async def test_seadex_exposes_malformed_results(self):
        scraper = SeaDexScraper(None, _Session({"items": [None]}))
        with (
            patch(
                "comet.discovery.adapters.torrent.seadex.anime_mapper.is_loaded",
                return_value=True,
            ),
            patch(
                "comet.discovery.adapters.torrent.seadex.anime_mapper.get_anilist_id",
                new=AsyncMock(return_value=123),
            ),
            self.assertRaises(TypeError),
        ):
            await scraper.scrape(REQUEST)

    async def test_debridio_parses_valid_streams(self):
        payload = {
            "streams": [
                {
                    "title": "First.Movie\n💾 1 GB 👤 12 ⚙️ RARBG",
                    "url": f"https://debrid.test/{'a' * 40}/play",
                },
                {
                    "title": "Second.Movie",
                    "url": f"https://debrid.test/{'c' * 40}/play",
                },
            ]
        }
        scraper = DebridioScraper(None, _Session(payload))
        with patch(
            "comet.discovery.adapters.torrent.debridio._debridio_config",
            return_value="config",
        ):
            torrents = await scraper.scrape(REQUEST)

        self.assertEqual(
            [torrent["title"] for torrent in torrents], ["First.Movie", "Second.Movie"]
        )
        self.assertEqual([torrent["seeders"] for torrent in torrents], [12, None])
