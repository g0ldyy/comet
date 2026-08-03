import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.core.capabilities import CapabilityPlanner
from comet.core.sources import LocatorKind, TransportKind
from comet.discovery.adapters.animetosho import (
    AnimeToshoAdapter,
    AnimeToshoConfiguration,
    _nzb_token,
    _nzb_url,
)
from comet.discovery.adapters.newznab import parse_newznab_feed
from comet.discovery.adapters.torrent.animetosho import AnimeToshoScraper
from comet.discovery.models import DiscoveryContext, MediaQuery
from comet.playback.base import Readiness
from comet.usenet.access import NativeAccessAuthorizer

CAPS = b"""<?xml version="1.0"?>
<caps>
  <searching><search available="yes" supportedParams="q"/></searching>
  <categories><category id="5070" name="Anime"/></categories>
</caps>"""

FEED = b"""<?xml version="1.0"?>
<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"
     xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <newznab:response offset="0" total="1"/>
    <item>
      <title>Example.S02E03.1080p</title>
      <guid isPermaLink="true">https://animetosho.org/view/a123</guid>
      <pubDate>Mon, 27 Jul 2026 12:00:00 +0000</pubDate>
      <enclosure
        url="https://storage.animetosho.org/torrent/aaaaaaaa/file.torrent"
        type="application/x-bittorrent" length="0"/>
      <enclosure
        url="https://storage.animetosho.org/nzbs/0123abcd/Example.S02E03.nzb"
        type="application/x-nzb" length="0"/>
      <newznab:attr name="size" value="1234"/>
      <torznab:attr name="infohash"
        value="0123456789abcdef0123456789abcdef01234567"/>
      <torznab:attr name="magneturl"
        value="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&amp;tr=udp%3A%2F%2Ftracker.example%3A80"/>
      <torznab:attr name="seeders" value="42"/>
    </item>
  </channel>
</rss>"""

SAFE_DOCTYPE = (
    b'<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" '
    b'"http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">'
)
NZB = b'<?xml version="1.0"?>' + SAFE_DOCTYPE + b"<nzb></nzb>"


class _Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def read(self):
        return self.body

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class _Session:
    def __init__(self, feed=FEED, nzb=NZB, feed_status=200):
        self.feed = feed
        self.nzb = nzb
        self.feed_status = feed_status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        params = kwargs.get("params") or {}
        if params.get("t") == "caps":
            return _Response(CAPS)
        return _Response(
            self.feed if "feed.animetosho.org" in url else self.nzb,
            self.feed_status if "feed.animetosho.org" in url else 200,
        )


class AnimeToshoDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_transport_refreshes_share_one_feed_parse(self):
        session = _Session()
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )
        query = MediaQuery(
            "kitsu:123",
            "series",
            title_aliases=("Example",),
        )

        torrent, usenet = await asyncio.gather(
            adapter.search(
                query,
                DiscoveryContext(frozenset({"bittorrent"}), b"a" * 32),
            ),
            adapter.search(
                query,
                DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
            ),
        )

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(torrent.candidates[0].transport, TransportKind.BITTORRENT)
        self.assertEqual(usenet.candidates[0].transport, TransportKind.USENET)

    async def test_different_searches_are_not_serialized_by_singleflight(self):
        adapter = AnimeToshoAdapter(
            _Session(),
            AnimeToshoConfiguration("source"),
        )
        active = 0
        peak = 0

        async def request(_params):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return FEED

        adapter._request = request
        context = DiscoveryContext(frozenset({"usenet"}), b"a" * 32)

        await asyncio.gather(
            adapter.search(
                MediaQuery("kitsu:123", "series", title_aliases=("First",)),
                context,
            ),
            adapter.search(
                MediaQuery("kitsu:456", "series", title_aliases=("Second",)),
                context,
            ),
        )

        self.assertEqual(peak, 2)

    async def test_one_shared_feed_emits_torrent_and_replayable_nzb_candidates(self):
        session = _Session()
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        result = await adapter.search(
            MediaQuery(
                "kitsu:123",
                "series",
                season=2,
                episode=3,
                title_aliases=("Example",),
            ),
            DiscoveryContext(
                frozenset({"bittorrent", "usenet"}),
                b"a" * 32,
            ),
        )

        self.assertEqual(result.coverage, frozenset({"bittorrent", "usenet"}))
        self.assertEqual(
            [candidate.transport for candidate in result.candidates],
            [TransportKind.BITTORRENT, TransportKind.USENET],
        )
        torrent, usenet = result.candidates
        self.assertEqual(torrent.locators[0].kind, LocatorKind.TORRENT)
        self.assertEqual(
            torrent.locators[0].info_hash,
            "0123456789abcdef0123456789abcdef01234567",
        )
        self.assertEqual(torrent.transport_stats, {"seeders": 42})
        self.assertEqual(usenet.locators[0].kind, LocatorKind.REAL_NZB)
        self.assertTrue(usenet.locators[0].remote_guid.startswith("atn1:"))
        self.assertNotIn("https:", usenet.locators[0].remote_guid)
        self.assertEqual(
            usenet.locators[0].adapter_configuration_id,
            "source",
        )
        self.assertEqual(
            usenet.locators[0].policy.owner_configuration_partition,
            b"a" * 32,
        )
        self.assertEqual(session.calls[0][1]["params"]["q"], "Example")

    async def test_grab_fetches_the_provider_url_without_rewriting_its_document(self):
        session = _Session()
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )
        token = _nzb_token(
            "https://storage.animetosho.org/nzbs/0123abcd/Example%20S02E03.nzb"
        )

        with patch(
            "comet.discovery.adapters.animetosho.fetch_http_bytes",
            new=AsyncMock(return_value=NZB),
        ) as fetch:
            document = await adapter.grab(token)

        self.assertEqual(document, NZB)
        self.assertEqual(
            fetch.await_args.args[0],
            "https://storage.animetosho.org/nzbs/0123abcd/Example%20S02E03.nzb",
        )

    async def test_validation_reuses_the_shared_newznab_caps_parser(self):
        adapter = AnimeToshoAdapter(
            _Session(),
            AnimeToshoConfiguration("source"),
        )

        status = await adapter.validate_config()

        self.assertEqual(status.readiness, Readiness.READY)

    async def test_provider_can_move_its_nzb_to_another_public_host(self):
        session = _Session(
            feed=FEED.replace(
                b"https://storage.animetosho.org/nzbs/0123abcd/Example.S02E03.nzb",
                b"https://foreign.example/nzbs/0123abcd/Example.S02E03.nzb",
            )
        )
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        result = await adapter.search(
            MediaQuery(
                "kitsu:123",
                "series",
                title_aliases=("Example",),
            ),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            _nzb_url(result.candidates[0].locators[0].remote_guid),
            "https://foreign.example/nzbs/0123abcd/Example.S02E03.nzb",
        )
        self.assertEqual(result.coverage, frozenset({"usenet"}))

    async def test_nzb_identity_does_not_depend_on_the_url_suffix(self):
        session = _Session(
            feed=FEED.replace(
                b"Example.S02E03.nzb",
                b"opaque.torrent",
            )
        )
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        result = await adapter.search(
            MediaQuery(
                "kitsu:123",
                "series",
                title_aliases=("Example",),
            ),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertTrue(
            _nzb_url(result.candidates[0].locators[0].remote_guid).endswith(
                "/opaque.torrent"
            )
        )

    async def test_expired_search_does_not_publish_complete_coverage(self):
        session = _Session()
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        result = await adapter.search(
            MediaQuery(
                "kitsu:123",
                "series",
                title_aliases=("Example",),
            ),
            DiscoveryContext(
                frozenset({"bittorrent", "usenet"}),
                b"a" * 32,
                hard_deadline=0,
            ),
        )

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.coverage, frozenset())
        self.assertEqual(session.calls, [])

    async def test_unbounded_seed_count_is_ignored_without_losing_the_release(self):
        session = _Session(
            feed=FEED.replace(
                b'value="42"',
                b'value="999999999999999999999999999999999999999999"',
            )
        )
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        result = await adapter.search(
            MediaQuery(
                "kitsu:123",
                "series",
                title_aliases=("Example",),
            ),
            DiscoveryContext(frozenset({"bittorrent"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].transport_stats, {"seeders": None})

    async def test_malformed_torrent_item_does_not_discard_valid_feed_items(self):
        malformed = b"""
    <item>
      <title>Malformed</title>
      <torznab:attr name="infohash" value="invalid"/>
    </item>"""
        session = _Session(
            feed=FEED.replace(b' total="1"', b' total="2"').replace(
                b"    <item>",
                malformed + b"\n    <item>",
                1,
            )
        )
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        result = await adapter.search(
            MediaQuery(
                "kitsu:123",
                "series",
                title_aliases=("Example",),
            ),
            DiscoveryContext(frozenset({"bittorrent"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].locators[0].info_hash,
            "0123456789abcdef0123456789abcdef01234567",
        )

    async def test_search_uses_normalized_metadata_title_without_a_second_limit(self):
        title = "x" * 513
        session = _Session()
        adapter = AnimeToshoAdapter(
            session,
            AnimeToshoConfiguration("source"),
        )

        await adapter.search(
            MediaQuery("kitsu:123", "series", title_aliases=(title,)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(session.calls[0][1]["params"]["q"], title)

    async def test_legacy_torrent_outage_is_not_reported_as_empty_success(self):
        adapter = AnimeToshoScraper(
            None,
            _Session(feed_status=503),
        )

        with self.assertRaises(RuntimeError):
            await adapter.search(
                MediaQuery(
                    "kitsu:123",
                    "series",
                    title_aliases=("Example",),
                    title="Example",
                ),
                DiscoveryContext(frozenset({"bittorrent"})),
            )

    async def test_legacy_torrent_pagination_has_a_fixed_request_ceiling(self):
        session = _Session(feed=FEED.replace(b'total="1"', b'total="999999999"'))
        adapter = AnimeToshoScraper(None, session)

        results = await adapter._scrape_query(
            "Example",
            asyncio.Semaphore(10),
        )

        self.assertEqual(len(results), 7)
        self.assertEqual(len(session.calls), 7)
        self.assertIn("offset=900&limit=100", session.calls[-1][0])


class AnimeToshoCodecTests(unittest.TestCase):
    def test_legacy_torrent_mapper_reuses_the_same_parsed_feed_items(self):
        items, _total = parse_newznab_feed(FEED)

        torrents = AnimeToshoScraper.parse_items(None, items)

        self.assertEqual(len(torrents), 1)
        self.assertEqual(
            torrents[0]["infoHash"],
            "0123456789abcdef0123456789abcdef01234567",
        )
        self.assertEqual(torrents[0]["seeders"], 42)

    def test_server_managed_source_is_usenet_only(self):
        torrent_provider = {
            "configurationId": "11111111-1111-4111-8111-111111111111",
            "displayName": "Torrent",
            "kind": "direct_torrent",
            "enabled": True,
            "options": {},
        }
        usenet_provider = {
            "configurationId": "22222222-2222-4222-8222-222222222222",
            "displayName": "NNTP",
            "kind": "stremio_nntp",
            "enabled": True,
            "options": {},
        }
        source = {
            "configurationId": "33333333-3333-4333-8333-333333333333",
            "kind": "animetosho",
            "enabled": True,
            "options": {},
        }
        planner = CapabilityPlanner(
            usenet_offered=True,
            native_authorizer=NativeAccessAuthorizer(None),
        )
        config = {
            "schemaVersion": 2,
            "enabledTransports": ["bittorrent", "usenet"],
            "playbackProviders": [torrent_provider, usenet_provider],
            "discoverySources": [source],
        }

        plan = planner.build(config)

        self.assertEqual(
            plan.branches_for(source["configurationId"]),
            frozenset({TransportKind.USENET}),
        )

    def test_storage_reference_codec_preserves_provider_urls(self):
        url = "https://storage.animetosho.org/nzbs/0123abcd/Example%20S02E03.nzb"
        token = _nzb_token(url)

        self.assertEqual(_nzb_url(token), url)
        foreign = "https://foreign.example/nzbs/0123abcd/Example.S02E03.nzb"
        self.assertEqual(
            _nzb_url(_nzb_token(foreign)),
            foreign,
        )
        with self.assertRaises(ValueError):
            _nzb_url(token + "=")

    def test_storage_reference_rejects_urls_that_cannot_be_persisted(self):
        self.assertIsNone(_nzb_token("https://example.com/" + "x" * 1_000))
