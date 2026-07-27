import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson
from fastapi import BackgroundTasks
from RTN import ParsedData
from starlette.requests import Request

from comet.api.endpoints.torznab import (
    FEED_LIMIT,
    NEWZNAB_NAMESPACE,
    TORZNAB_NAMESPACE,
    CategoryConstraint,
    SearchTarget,
    TorznabProtocolError,
    build_magnet,
    category_constraint,
    find_recent_target,
    parse_torznab_query,
    resolve_search_target,
    serialize_caps,
    serialize_feed,
    torznab_api,
)
from comet.core.models import settings
from comet.metadata.imdb import resolve_imdb_title
from comet.services.media_search import MediaSearchResult, MediaSearchStatus


def _request(query: str = "", path: str = "/torznab/api") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("example.test", 443),
        }
    )


def _torrent(index: int, *, title: str | None = None, seeders: int = 0) -> dict:
    release_title = title if title is not None else f"Movie.Release.{index}"
    return {
        "title": release_title,
        "seeders": seeders,
        "size": index + 1,
        "tracker": "indexer",
        "sources": [
            "tracker:udp://tracker.example:80/announce",
            "udp://tracker.example:80/announce",
            "dht:node",
        ],
        "updatedAt": 1_700_000_000,
        "parsed": ParsedData(
            raw_title=release_title,
            resolution="1080p",
            year=2024,
            languages=["en"],
        ),
    }


def _result(count: int, *, media_type: str = "movie") -> MediaSearchResult:
    hashes = [f"{index:040x}" for index in range(count)]
    return MediaSearchResult(
        MediaSearchStatus.OK,
        metadata={"title": "Movie", "year": 2024},
        torrents={
            info_hash: _torrent(index) for index, info_hash in enumerate(hashes)
        },
        ranked_info_hashes=hashes,
        media_only_id="tt1234567",
        search_season=0 if media_type == "series" else None,
        search_episode=2 if media_type == "series" else None,
    )


class _Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def get(self, url):
        self.requests.append(url)
        return self.response


class TorznabPureTests(unittest.IsolatedAsyncioTestCase):
    def test_caps_are_parseable_and_honest(self):
        with patch.object(settings, "DISABLE_TORRENT_STREAMS", False):
            root = ET.fromstring(serialize_caps())

        self.assertEqual(root.tag, "caps")
        self.assertEqual(root.find("server").attrib["version"], "1.3")
        self.assertEqual(
            root.find("limits").attrib,
            {"max": str(FEED_LIMIT), "default": str(FEED_LIMIT)},
        )
        self.assertEqual(
            [category.attrib["id"] for category in root.findall("categories/category")],
            ["2000", "5000"],
        )
        self.assertTrue(
            all(
                node.attrib["available"] == "yes"
                for node in root.findall("searching/*")
            )
        )

        with patch.object(settings, "DISABLE_TORRENT_STREAMS", True):
            disabled = ET.fromstring(serialize_caps())
        self.assertTrue(
            all(
                node.attrib["available"] == "no"
                for node in disabled.findall("searching/*")
            )
        )

    def test_manual_parser_handles_case_repetition_and_scopes(self):
        parsed = parse_torznab_query(
            _request(
                "T=TVSEARCH&Q=Ignored&IMDBID=1234567"
                "&CAT=5000,5030&cat=5040&season=S0&ep=E2"
                "&offset=100&limit=999&O=XML"
            )
        )

        self.assertEqual(parsed.function, "tvsearch")
        self.assertEqual(parsed.imdb_id, "tt1234567")
        self.assertEqual(parsed.categories, (5000, 5030, 5040))
        self.assertEqual((parsed.season, parsed.episode), (0, 2))

        daily = parse_torznab_query(
            _request("t=tvsearch&q=Daily.Show&season=2026&ep=07/25")
        )
        self.assertEqual((daily.season, daily.episode), (2026, "07/25"))

    def test_parser_rejects_invalid_values_with_protocol_errors(self):
        invalid_queries = (
            "t=caps&o=json",
            "t=search&cat=-1",
            "t=search&season=one",
            "t=search&ep=E",
            "t=tvsearch&q=Show&season=2026&ep=13/40",
            "t=tvsearch&q=Show&season=13&ep=07/25",
            "t=search&year=24",
            "t=search&o=json",
        )
        for query in invalid_queries:
            with self.subTest(query=query), self.assertRaises(
                TorznabProtocolError
            ) as context:
                parse_torznab_query(_request(query))
            self.assertEqual(context.exception.code, 201)

    async def test_title_scope_and_daily_episode_resolve_canonical_ids(self):
        session = object()
        title_match = SimpleNamespace(
            imdb_id="tt7654321", media_type="series", year=2024
        )
        parsed = parse_torznab_query(
            _request("t=search&q=Show.Name.S01E002&cat=5000")
        )
        with patch(
            "comet.api.endpoints.torznab.resolve_imdb_title",
            new=AsyncMock(return_value=title_match),
        ) as resolver:
            target = await resolve_search_target(
                parsed, category_constraint(parsed.categories), session
            )

        self.assertEqual(target, SearchTarget("series", "tt7654321:1:2"))
        resolver.assert_awaited_once_with(
            session, "Show.Name", media_type="series", year=None
        )

        daily = parse_torznab_query(
            _request("t=tvsearch&imdbid=7654321&season=2026&ep=07/25")
        )
        with patch(
            "comet.api.endpoints.torznab.EpisodeIndexService.get_episode_by_air_date",
            new=AsyncMock(return_value=(3, 9)),
        ) as episode_lookup:
            target = await resolve_search_target(
                daily, CategoryConstraint(None, False), session
            )
        self.assertEqual(target, SearchTarget("series", "tt7654321:3:9"))
        episode_lookup.assert_awaited_once_with("tt7654321", "2026-07-25")

        prioritized = parse_torznab_query(
            _request(
                "t=search&imdbid=1234567&q=Different.Show.S01E02&cat=5000"
            )
        )
        target = await resolve_search_target(
            prioritized, category_constraint(prioritized.categories), session
        )
        self.assertEqual(target, SearchTarget("series", "tt1234567:1:2"))

    async def test_title_resolver_uses_one_request_and_filters_type_and_year(self):
        session = _Session(
            _Response(
                {
                    "d": [
                        {"id": "tt1111111", "qid": "movie", "y": 2024},
                        {"id": "bad", "qid": "tvSeries", "y": 2026},
                        {"id": "tt2222222", "qid": "tvSeries", "y": 2025},
                        {"id": "tt3333333", "qid": "tvSeries", "y": 2026},
                    ]
                }
            )
        )

        match = await resolve_imdb_title(
            session, "A title", media_type="series", year=2026
        )

        self.assertEqual(match.imdb_id, "tt3333333")
        self.assertEqual(match.media_type, "series")
        self.assertEqual(len(session.requests), 1)

        no_match_session = _Session(
            _Response({"d": [{"id": "tt1111111", "qid": "movie", "y": 2024}]})
        )
        self.assertIsNone(
            await resolve_imdb_title(
                no_match_session, "A title", media_type="series", year=2026
            )
        )

        nearby_session = _Session(
            _Response(
                {"d": [{"id": "tt2222222", "qid": "tvSeries", "y": 2025}]}
            )
        )
        nearby_match = await resolve_imdb_title(
            nearby_session, "A title", media_type="series", year=2026
        )
        self.assertEqual(nearby_match.imdb_id, "tt2222222")
        self.assertEqual(nearby_match.year, 2025)

    async def test_recent_selection_requires_real_rows_and_bounds_type_lookup(self):
        rows = [
            {"media_id": "kitsu:123"},
            {"media_id": "tt1111111"},
            {"media_id": "tt2222222"},
        ]
        with (
            patch(
                "comet.api.endpoints.torznab.database.fetch_all",
                new=AsyncMock(return_value=rows),
            ),
            patch(
                "comet.api.endpoints.torznab._candidate_torrent_row",
                new=AsyncMock(
                    side_effect=[
                        {"season": None, "episode": None},
                        {"season": None, "episode": None},
                    ]
                ),
            ),
            patch(
                "comet.api.endpoints.torznab.TMDBApi.get_media_type_from_imdb",
                new=AsyncMock(return_value=None),
            ) as media_type_lookup,
        ):
            target = await find_recent_target(
                CategoryConstraint(None, False), object()
            )

        self.assertIsNone(target)
        media_type_lookup.assert_awaited_once_with("tt1111111")

    def test_xml_serialization_sanitizes_and_emits_required_fields(self):
        result = _result(1)
        info_hash = result.ranked_info_hashes[0]
        result.torrents[info_hash] = _torrent(
            0, title="A & B < C\x00 😀", seeders=0
        )

        content, total = serialize_feed(
            result,
            "movie",
            "https://example.test/torznab/api?a=1&b=2",
            request_timestamp=1_700_000_001,
        )
        root = ET.fromstring(content)
        item = root.find("channel/item")
        attrs = {
            element.attrib["name"]: element.attrib["value"]
            for element in item.findall(f"{{{TORZNAB_NAMESPACE}}}attr")
        }

        self.assertEqual(total, 1)
        self.assertIn(b'xmlns:torznab="http://torznab.com/', content)
        self.assertIn(b'xmlns:newznab="http://www.newznab.com/', content)
        self.assertEqual(item.findtext("title"), "A & B < C 😀")
        self.assertIsNotNone(item.find("pubDate"))
        self.assertEqual(item.findtext("category"), "Movies")
        self.assertEqual(item.findtext("size"), "1")
        self.assertEqual(item.find("enclosure").attrib["length"], "1")
        self.assertEqual(attrs["infohash"], info_hash)
        self.assertEqual(attrs["imdb"], "1234567")
        self.assertEqual(attrs["seeders"], "0")
        self.assertIn("magnet:?xt=urn:btih:", attrs["magneturl"])
        self.assertEqual(attrs["magneturl"].count("tr="), 1)

    def test_magnet_encodes_title_and_normalizes_trackers(self):
        torrent = _torrent(0, title="A & B")
        magnet = build_magnet("a" * 40, "A & B", torrent)

        self.assertTrue(magnet.startswith(f"magnet:?xt=urn:btih:{'a' * 40}&"))
        self.assertIn("dn=A%20%26%20B", magnet)
        self.assertEqual(magnet.count("tr="), 1)
        self.assertNotIn("dht", magnet)

    def test_feed_serializes_every_ranked_result_in_one_page(self):
        result = _result(205)
        content, total = serialize_feed(
            result, "movie", "https://example.test/torznab/api"
        )
        root = ET.fromstring(content)
        response = root.find(f"channel/{{{NEWZNAB_NAMESPACE}}}response")
        hashes = [item.findtext("guid") for item in root.findall("channel/item")]

        self.assertEqual(total, 205)
        self.assertEqual(response.attrib, {"offset": "0", "total": "205"})
        self.assertEqual(hashes, result.ranked_info_hashes)


class TorznabRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_offset_and_limit_parameters_are_ignored(self):
        request = _request("t=movie&imdbid=1234567&offset=200&limit=1")
        with (
            patch.object(settings, "HTTP_CACHE_ENABLED", False),
            patch(
                "comet.api.endpoints.torznab.http_client_manager.get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch("comet.api.endpoints.torznab.config_check", return_value={}),
            patch(
                "comet.api.endpoints.torznab.search_media",
                new=AsyncMock(return_value=_result(205)),
            ),
        ):
            response = await torznab_api(request, BackgroundTasks())

        root = ET.fromstring(response.body)
        self.assertEqual(len(root.findall("channel/item")), 205)
        self.assertEqual(
            root.find(f"channel/{{{NEWZNAB_NAMESPACE}}}response").attrib,
            {"offset": "0", "total": "205"},
        )

    async def test_category_filter_returns_empty_before_search(self):
        request = _request("t=movie&imdbid=1234567&cat=5000")
        with patch(
            "comet.api.endpoints.torznab.search_media", new=AsyncMock()
        ) as search:
            response = await torznab_api(request, BackgroundTasks())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/rss+xml; charset=utf-8",
        )
        self.assertEqual(
            ET.fromstring(response.body)
            .find(f"channel/{{{NEWZNAB_NAMESPACE}}}response")
            .attrib["total"],
            "0",
        )
        search.assert_not_awaited()

    async def test_unknown_categories_return_empty_before_search(self):
        request = _request("t=search&q=Movie&cat=1000,7000")
        with patch(
            "comet.api.endpoints.torznab.search_media", new=AsyncMock()
        ) as search:
            response = await torznab_api(request, BackgroundTasks())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ET.fromstring(response.body).tag, "rss")
        search.assert_not_awaited()

    async def test_targeted_search_uses_exact_default_config_object(self):
        request = _request("t=movie&imdbid=1234567")
        config = object()
        result = _result(1)
        session = object()
        with (
            patch(
                "comet.api.endpoints.torznab.http_client_manager.get_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "comet.api.endpoints.torznab.config_check",
                return_value=config,
            ) as check,
            patch(
                "comet.api.endpoints.torznab.search_media",
                new=AsyncMock(return_value=result),
            ) as search,
        ):
            response = await torznab_api(request, BackgroundTasks())

        self.assertEqual(response.status_code, 200)
        check.assert_called_once_with(None, strict_b64config=True)
        self.assertIs(search.await_args.args[2], config)
        self.assertEqual(search.await_args.args[:2], ("movie", "tt1234567"))

    async def test_recent_probe_selects_one_target_and_calls_search_once(self):
        request = _request("t=search")
        target = SearchTarget("series", "tt1234567:1:2")
        with (
            patch(
                "comet.api.endpoints.torznab.http_client_manager.get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.torznab.find_recent_target",
                new=AsyncMock(return_value=target),
            ) as recent,
            patch(
                "comet.api.endpoints.torznab.config_check",
                return_value={},
            ),
            patch(
                "comet.api.endpoints.torznab.search_media",
                new=AsyncMock(return_value=_result(1, media_type="series")),
            ) as search,
        ):
            response = await torznab_api(request, BackgroundTasks())

        self.assertEqual(response.status_code, 200)
        recent.assert_awaited_once()
        search.assert_awaited_once()

    async def test_empty_recent_probe_is_an_honest_empty_feed(self):
        request = _request("t=search")
        with (
            patch(
                "comet.api.endpoints.torznab.http_client_manager.get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.torznab.find_recent_target",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "comet.api.endpoints.torznab.search_media",
                new=AsyncMock(),
            ) as search,
        ):
            response = await torznab_api(request, BackgroundTasks())

        root = ET.fromstring(response.body)
        self.assertEqual(
            root.find(f"channel/{{{NEWZNAB_NAMESPACE}}}response").attrib["total"],
            "0",
        )
        search.assert_not_awaited()

    async def test_errors_are_xml_http_200_and_do_not_leak_exceptions(self):
        invalid = await torznab_api(
            _request("t=tvsearch&q=Show&ep=2"), BackgroundTasks()
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(ET.fromstring(invalid.body).attrib["code"], "201")

        request = _request("t=movie&imdbid=1234567")
        with (
            patch.object(settings, "HTTP_CACHE_ENABLED", True),
            patch(
                "comet.api.endpoints.torznab.http_client_manager.get_session",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "comet.api.endpoints.torznab.config_check",
                return_value={},
            ),
            patch(
                "comet.api.endpoints.torznab.search_media",
                new=AsyncMock(side_effect=RuntimeError("private detail")),
            ),
        ):
            backend = await torznab_api(request, BackgroundTasks())

        root = ET.fromstring(backend.body)
        self.assertEqual(root.attrib["code"], "900")
        self.assertNotIn(b"private detail", backend.body)
        self.assertIn("no-store", backend.headers.get("cache-control", ""))

    async def test_global_disable_returns_unavailable_xml(self):
        with patch.object(settings, "DISABLE_TORRENT_STREAMS", True):
            response = await torznab_api(
                _request("t=search&q=Movie"), BackgroundTasks()
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ET.fromstring(response.body).attrib["code"], "203")

    def test_route_is_mounted_only_under_the_configured_prefix(self):
        from comet.api.app import app

        expected = f"{settings.STREMIO_API_PREFIX}/torznab/api"
        self.assertEqual(str(app.url_path_for("torznab_api")), expected)

    async def test_stream_adapter_preserves_shared_ranked_order(self):
        from comet.api.endpoints import stream as stream_endpoint
        from comet.api.endpoints import torznab as torznab_endpoint
        from comet.core.config_validation import config_check

        self.assertIs(stream_endpoint.search_media, torznab_endpoint.search_media)
        config = config_check(None, strict_b64config=True)
        result = _result(3)
        result.sort_mixed = True
        with (
            patch.object(settings, "HTTP_CACHE_ENABLED", False),
            patch.object(stream_endpoint, "config_check", return_value=config),
            patch.object(
                stream_endpoint,
                "search_media",
                new=AsyncMock(return_value=result),
            ) as search,
        ):
            response = await stream_endpoint.stream(
                _request(path="/stream/movie/tt1234567.json"),
                "movie",
                "tt1234567",
                BackgroundTasks(),
            )

        self.assertIs(search.await_args.args[2], config)
        payload = orjson.loads(response.body)
        self.assertEqual(
            [stream["infoHash"] for stream in payload["streams"]],
            result.ranked_info_hashes,
        )


if __name__ == "__main__":
    unittest.main()
