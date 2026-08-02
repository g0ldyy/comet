import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.core.sources import TransportKind
from comet.discovery.adapters.newznab import (
    NewznabAccount,
    NewznabAdapter,
    NewznabError,
    NewznabFeedItem,
    _parse_caps,
    _query_params,
    _status_error,
    map_newznab_nzb_item,
    newznab_account_from_options,
)
from comet.discovery.models import DiscoveryContext, MediaQuery

CAPS = b"""<?xml version="1.0"?>
<caps>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes"
      supportedParams="q,season,ep,imdbid,tvdbid,kitsu_id"/>
    <movie-search available="yes" supportedParams="q,imdbid,tmdbid,year"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"/>
    <category id="5000" name="TV"/>
  </categories>
</caps>"""

RESULTS = b"""<?xml version="1.0"?>
<rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
  <channel>
    <newznab:response offset="0" total="1"/>
    <item>
      <title><![CDATA[Example.S02E03.2026.1080p]]></title>
      <guid isPermaLink="true">https://indexer.example/get?id=opaque-guid&amp;apikey=secret</guid>
      <pubDate>Mon, 27 Jul 2026 12:00:00 +0000</pubDate>
      <enclosure url="https://indexer.example/get?id=opaque-guid&amp;apikey=secret"
        length="1234" type="application/x-nzb"/>
      <newznab:attr name="size" value="1234"/>
    </item>
  </channel>
</rss>"""


NON_ASCII_DIGIT_CAPS = (
    '<?xml version="1.0"?><caps><searching>'
    '<search available="yes" supportedParams="q"/>'
    '<tv-search available="yes" supportedParams="q,season,ep"/>'
    '<movie-search available="yes" supportedParams="q"/>'
    "</searching><categories>"
    '<category id="\u00b2" name="TV"/>'
    '<category id="\u0663" name="Anime"/>'
    '<category id="5000" name="Real"/>'
    "</categories></caps>"
).encode()


class _Response:
    def __init__(self, body: bytes, status: int = 200, on_read=None, headers=None):
        self.body = body
        self.status = status
        self.on_read = on_read
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def iter_chunked(self, size):
        if self.on_read is not None:
            self.on_read()
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class _Session:
    def __init__(self, caps=CAPS, results=RESULTS):
        self.caps = caps
        self.results = results
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.caps if kwargs["params"]["t"] == "caps" else self.results)


class _PagedSession(_Session):
    def __init__(self, pages, *, cancel_on_offset=None):
        super().__init__()
        self.pages = pages
        self.cancel_on_offset = cancel_on_offset

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        params = kwargs["params"]
        if params["t"] == "caps":
            return _Response(self.caps)
        offset = int(params["offset"])
        return _Response(
            self.pages[offset],
            on_read=(
                self.cancel_on_offset.set
                if self.cancel_on_offset is not None and offset == 0
                else None
            ),
        )


class NewznabDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_preserves_an_upstream_authentication_failure(self):
        adapter = NewznabAdapter(
            _Session(),
            NewznabAccount(
                "https://indexer.example/api",
                "invalid-key",
                "source",
            ),
        )
        adapter._caps_for_search = AsyncMock(
            side_effect=NewznabError("api_key_invalid", auth_failed=True)
        )

        status = await adapter.validate_config()

        self.assertEqual(status.code, "api_key_invalid")
        self.assertTrue(status.auth_failed)

    async def test_rps_limit_waits_for_the_next_window(self):
        governor = AsyncMock()
        governor.acquire_window.side_effect = (None, object(), object())
        adapter = NewznabAdapter(
            _Session(),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "source",
                user_agent_mode="custom",
            ),
            governor=governor,
            governor_scope=b"a" * 32,
        )

        with (
            patch(
                "comet.discovery.adapters.newznab.asyncio.sleep", AsyncMock()
            ) as sleep,
            patch("comet.discovery.adapters.newznab.time.time", return_value=10.25),
        ):
            await adapter._request({"t": "caps"}, maximum=256 * 1024)

        sleep.assert_awaited_once_with(0.75)
        self.assertEqual(
            [call.args[1] for call in governor.acquire_window.await_args_list],
            ["newznab_rps", "newznab_rps", "newznab_query_daily"],
        )

    async def test_grab_replays_only_opaque_id_with_distinct_user_agent(self):
        nzb = b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"></nzb>'
        session = _Session(results=nzb)
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
                query_user_agent="Query-UA",
                grab_user_agent="Grab-UA",
            ),
        )

        document = await adapter.grab("opaque-guid")

        self.assertEqual(document, nzb)
        params = session.calls[0][1]["params"]
        self.assertEqual(
            params,
            {"t": "get", "id": "opaque-guid", "apikey": "secret"},
        )
        self.assertEqual(session.calls[0][1]["headers"]["User-Agent"], "Grab-UA")
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        with self.assertRaises(ValueError):
            await adapter.grab("https://indexer.example/signed?apikey=secret")
        with self.assertRaises(ValueError):
            await adapter.grab("HTTPS://indexer.example/signed?apikey=secret")

    async def test_grab_leaves_content_validation_to_the_nzb_broker(self):
        session = _Session(results=b"opaque document")
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        self.assertEqual(await adapter.grab("opaque-guid"), b"opaque document")

    async def test_grab_follows_bounded_validated_redirect_without_forwarding_api_key(
        self,
    ):
        nzb = b'<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"/>'
        session = _Session()
        responses = iter(
            (
                _Response(
                    b"",
                    status=302,
                    headers={"Location": "https://cdn.example/release?token=signed"},
                ),
                _Response(nzb),
            )
        )

        def redirected(_url, **kwargs):
            session.calls.append((_url, kwargs))
            return next(responses)

        session.get = redirected
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
                grab_user_agent="Grab-UA",
            ),
        )

        with patch(
            "comet.discovery.adapters.newznab.validate_http_url",
            AsyncMock(),
        ) as validate:
            validate.return_value.url = "https://cdn.example/release?token=signed"
            document = await adapter.grab("opaque-guid")

        self.assertEqual(document, nzb)
        self.assertEqual(
            session.calls[0][1]["params"],
            {"t": "get", "id": "opaque-guid", "apikey": "secret"},
        )
        validate.assert_awaited_once_with(
            "https://cdn.example/release?token=signed",
            allowed_private_origins=frozenset({"https://indexer.example:443"}),
        )
        self.assertEqual(
            session.calls[1],
            (
                "https://cdn.example/release?token=signed",
                {
                    "headers": {
                        "Accept": "application/xml, text/xml, application/rss+xml",
                        "User-Agent": "Grab-UA",
                    },
                    "allow_redirects": False,
                },
            ),
        )
        self.assertNotIn("secret", repr(session.calls[1]))

    async def test_caps_aware_tv_search_maps_only_opaque_replay_identity(self):
        session = _Session()
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )
        query = MediaQuery(
            "tt1234567",
            "series",
            season=2,
            episode=3,
            title_aliases=("Example",),
            search_scope="episode",
        )

        result = await adapter.search(
            query,
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(result.coverage, frozenset({"usenet"}))
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(candidate.transport, TransportKind.USENET)
        self.assertEqual(candidate.size, 1234)
        self.assertEqual(candidate.locators[0].remote_guid, "opaque-guid")
        self.assertNotIn("secret", candidate.locators[0].remote_guid)
        self.assertEqual(candidate.identities, ())
        search_params = session.calls[1][1]["params"]
        self.assertEqual(search_params["t"], "tvsearch")
        self.assertEqual(search_params["imdbid"], "1234567")
        self.assertEqual(search_params["season"], "2")
        self.assertEqual(search_params["ep"], "3")

        await adapter.search(
            query,
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )
        self.assertEqual(
            sum(call[1]["params"]["t"] == "caps" for call in session.calls),
            1,
        )
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertFalse(session.calls[1][1]["allow_redirects"])

    async def test_pagination_counts_feed_rows_not_only_valid_candidates(self):
        first = RESULTS.replace(b'total="1"', b'total="2"').replace(
            b"https://indexer.example/get?id=opaque-guid&amp;apikey=secret",
            b"https://indexer.example/signed/path?apikey=secret",
        )
        second = RESULTS.replace(b'total="1"', b'total="2"').replace(
            b"opaque-guid",
            b"opaque-guid-2",
        )
        session = _PagedSession({0: first, 1: second})
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
                max_results=2,
                page_size=1,
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].locators[0].remote_guid,
            "opaque-guid-2",
        )
        self.assertEqual(
            [
                call[1]["params"].get("offset")
                for call in session.calls
                if call[1]["params"]["t"] != "caps"
            ],
            ["0", "1"],
        )
        self.assertEqual(result.coverage, frozenset({"usenet"}))

    async def test_pagination_advances_by_returned_rows_after_a_short_page(self):
        first = RESULTS.replace(b'total="1"', b'total="2"')
        second = first.replace(b"opaque-guid", b"opaque-guid-2")
        session = _PagedSession({0: first, 1: second})
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
                max_results=2,
                page_size=2,
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(
            [
                call[1]["params"].get("offset")
                for call in session.calls
                if call[1]["params"]["t"] != "caps"
            ],
            ["0", "1"],
        )
        self.assertEqual(result.coverage, frozenset({"usenet"}))

    async def test_cancelled_partial_page_is_not_recorded_as_complete_coverage(self):
        cancellation = asyncio.Event()
        first = RESULTS.replace(b'total="1"', b'total="2"')
        session = _PagedSession({0: first}, cancel_on_offset=cancellation)
        adapter = NewznabAdapter(
            session,
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
                max_results=2,
                page_size=1,
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(
                frozenset({"usenet"}),
                b"a" * 32,
                cancellation=cancellation,
            ),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.coverage, frozenset())
        self.assertEqual(len(session.calls), 2)

    async def test_signed_url_without_replay_id_is_not_persisted(self):
        results = RESULTS.replace(
            b"https://indexer.example/get?id=opaque-guid&amp;apikey=secret",
            b"https://indexer.example/signed/path?apikey=secret",
        )
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery(
                "tt1234567",
                "series",
                title_aliases=("Example",),
            ),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(result.candidates, ())

    async def test_unusable_item_does_not_discard_valid_sibling(self):
        unusable = (
            b"<item><title></title>"
            b'<newznab:attr name="guid" value="unusable-guid"/>'
            b"</item>"
        )
        results = RESULTS.replace(b'total="1"', b'total="2"').replace(
            b"<item>",
            unusable + b"<item>",
            1,
        )
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].locators[0].remote_guid,
            "opaque-guid",
        )

    async def test_future_enclosure_type_can_supply_replay_identity(self):
        results = RESULTS.replace(
            (
                b'<guid isPermaLink="true">'
                b"https://indexer.example/get?id=opaque-guid&amp;apikey=secret"
                b"</guid>"
            ),
            (
                b'<guid isPermaLink="true">'
                b"https://indexer.example/signed?apikey=secret"
                b"</guid>"
            ),
        ).replace(b'application/x-nzb"', b'application/vnd.future-nzb"')
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(
            result.candidates[0].locators[0].remote_guid,
            "opaque-guid",
        )

    async def test_attribute_url_can_supply_replay_identity_without_persisting_secret(
        self,
    ):
        results = RESULTS.replace(
            b'<newznab:attr name="size" value="1234"/>',
            (
                b'<newznab:attr name="size" value="1234"/>'
                b'<newznab:attr name="guid" '
                b'value="https://indexer.example/get?id=attribute-guid&amp;apikey=secret"/>'
            ),
        ).replace(b"opaque-guid", b"signed-path")
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        remote_guid = result.candidates[0].locators[0].remote_guid
        self.assertEqual(remote_guid, "attribute-guid")
        self.assertNotIn("secret", remote_guid)

    async def test_permalink_identity_can_be_verified_by_the_download_path(self):
        results = RESULTS.replace(
            (
                b'<guid isPermaLink="true">'
                b"https://indexer.example/get?id=opaque-guid&amp;apikey=secret"
                b"</guid>"
            ),
            (
                b'<guid isPermaLink="true">'
                b"https://indexer.example/details/opaque-guid"
                b"</guid>"
            ),
        ).replace(
            b"https://indexer.example/get?id=opaque-guid&amp;apikey=secret",
            b"https://indexer.example/getnzb/opaque-guid-signed-token",
        )
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        remote_guid = result.candidates[0].locators[0].remote_guid
        self.assertEqual(remote_guid, "opaque-guid")
        self.assertNotIn("signed-token", remote_guid)

    def test_explicit_internal_replay_identity_remains_strict(self):
        for remote_id in (
            "https://indexer.example/signed",
            " https://indexer.example/signed",
            "x" * 1_025,
        ):
            with (
                self.subTest(remote_id=remote_id[:40]),
                self.assertRaisesRegex(ValueError, "replay identifier"),
            ):
                map_newznab_nzb_item(
                    NewznabFeedItem({"title": "Example"}, {}, ()),
                    MediaQuery("tt1234567", "series"),
                    configuration_id="source",
                    label="Newznab",
                    context=DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
                    remote_id=remote_id,
                )

    async def test_result_titles_are_not_normalized(self):
        results = RESULTS.replace(
            b"Example.S02E03.2026.1080p",
            b"  Example.S02E03.2026.1080p  ",
        )
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(
            result.candidates[0].title,
            "  Example.S02E03.2026.1080p  ",
        )

    async def test_unbounded_feed_numbers_use_bounded_enclosure_without_losing_item(
        self,
    ):
        huge = b"9" * 10_000
        results = RESULTS.replace(b'total="1"', b'total="' + huge + b'"')
        results = results.replace(
            b'<newznab:attr name="size" value="1234"/>',
            b'<newznab:attr name="size" value="' + huge + b'"/>',
        )
        adapter = NewznabAdapter(
            _Session(results=results),
            NewznabAccount(
                "https://indexer.example/api",
                "secret",
                "11111111-1111-4111-8111-111111111111",
            ),
        )

        result = await adapter.search(
            MediaQuery("tt1234567", "series", title_aliases=("Example",)),
            DiscoveryContext(frozenset({"usenet"}), b"a" * 32),
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].size, 1234)
        self.assertEqual(result.coverage, frozenset({"usenet"}))

    def test_daily_and_season_pack_queries_do_not_guess_episode_shapes(self):
        caps = _parse_caps(CAPS)
        daily = _query_params(
            MediaQuery(
                "tt1234567",
                "series",
                air_date="2026-07-27",
                title_aliases=("Example",),
                search_scope="daily_episode",
            ),
            caps,
        )
        season_pack = _query_params(
            MediaQuery(
                "tt1234567",
                "series",
                season=2,
                episode=3,
                title_aliases=("Example",),
                search_scope="season_pack",
            ),
            caps,
        )

        self.assertEqual((daily["season"], daily["ep"]), ("2026", "07/27"))
        self.assertEqual(season_pack["season"], "2")
        self.assertNotIn("ep", season_pack)

    def test_query_always_scopes_results_to_the_media_category(self):
        caps = _parse_caps(CAPS.replace(b'id="2000"', b'id="2070"'))

        params = _query_params(
            MediaQuery(
                "tt1234567",
                "movie",
                title_aliases=("Example",),
            ),
            caps,
        )

        self.assertEqual(params["cat"], "2000")

    async def test_stealth_mode_uses_browser_impersonation_without_overriding_its_user_agent(
        self,
    ):
        response = AsyncMock()
        response.status = 200
        response.headers = {}
        response.read.return_value = CAPS
        context = AsyncMock()
        context.__aenter__.return_value = response
        stealth_session = unittest.mock.Mock()
        stealth_session.get.return_value = context

        with patch(
            "comet.discovery.adapters.newznab.network_manager.get_client",
            return_value=stealth_session,
        ) as get_client:
            adapter = NewznabAdapter(
                _Session(),
                newznab_account_from_options(
                    {
                        "apiKey": "secret",
                        "endpoint": "https://indexer.example/api",
                        "userAgentMode": "stealth",
                    },
                    "source",
                ),
            )
            await adapter.validate_config()

        get_client.assert_called_once_with(
            "newznab",
            impersonate="chrome",
            discard_cookies=True,
            proxy_ethos="always",
            proxy_setting="USER_PROVIDED_PROXY_URL",
        )
        request = stealth_session.get.call_args
        self.assertNotIn("User-Agent", request.kwargs["headers"])
        self.assertEqual(request.kwargs["headers"]["Origin"], "https://indexer.example")
        self.assertEqual(
            request.kwargs["headers"]["Referer"], "https://indexer.example/"
        )
        self.assertEqual(request.kwargs["maximum_body_bytes"], 256 * 1024)

    async def test_stealth_grab_keeps_browser_impersonation_across_redirects(self):
        redirect_response = AsyncMock()
        redirect_response.status = 302
        redirect_response.headers = {"Location": "https://cdn.example/release.nzb"}
        redirect_context = AsyncMock()
        redirect_context.__aenter__.return_value = redirect_response
        nzb_response = AsyncMock()
        nzb_response.status = 200
        nzb_response.headers = {}
        nzb_response.read.return_value = b"<nzb/>"
        nzb_context = AsyncMock()
        nzb_context.__aenter__.return_value = nzb_response
        stealth_session = unittest.mock.Mock()
        stealth_session.get.side_effect = (redirect_context, nzb_context)

        with (
            patch(
                "comet.discovery.adapters.newznab.network_manager.get_client",
                return_value=stealth_session,
            ),
            patch(
                "comet.discovery.adapters.newznab.validate_http_url",
                AsyncMock(),
            ) as validate,
        ):
            validate.return_value.url = "https://cdn.example/release.nzb"
            adapter = NewznabAdapter(
                _Session(),
                newznab_account_from_options(
                    {
                        "apiKey": "secret",
                        "endpoint": "https://indexer.example/api",
                    },
                    "source",
                ),
            )
            document = await adapter.grab("opaque-guid")

        self.assertEqual(document, b"<nzb/>")
        self.assertEqual(stealth_session.get.call_count, 2)
        first, second = stealth_session.get.call_args_list
        self.assertNotIn("User-Agent", first.kwargs["headers"])
        self.assertNotIn("User-Agent", second.kwargs["headers"])
        self.assertEqual(first.kwargs["maximum_body_bytes"], 150 * 1024 * 1024)
        self.assertEqual(second.kwargs["maximum_body_bytes"], 150 * 1024 * 1024)
        self.assertNotIn("params", second.kwargs)

    def test_options_default_to_stealth(self):
        stealth = newznab_account_from_options(
            {"apiKey": "secret", "endpoint": "https://indexer.example/api"},
            "source",
        )
        explicit = newznab_account_from_options(
            {
                "apiKey": "secret",
                "endpoint": "https://indexer.example/api",
                "userAgentMode": "custom",
                "queryUserAgent": "Query-UA",
                "grabUserAgent": "Grab-UA",
            },
            "source",
        )

        self.assertEqual(stealth.user_agent_mode, "stealth")
        self.assertEqual(explicit.user_agent_mode, "custom")

    def test_query_text_is_trimmed_and_blank_only_searches_are_rejected(self):
        caps = _parse_caps(CAPS)

        params = _query_params(
            MediaQuery(
                "custom:123",
                "series",
                title_aliases=("  Example  ",),
            ),
            caps,
        )

        self.assertEqual(params["q"], "Example")
        with self.assertRaisesRegex(NewznabError, "provider_query_unsupported"):
            _query_params(
                MediaQuery(
                    "custom:123",
                    "series",
                    title_aliases=("   ",),
                ),
                caps,
            )

    def test_account_options_have_one_unpublished_credential_schema(self):
        with self.assertRaises(ValueError):
            newznab_account_from_options(
                {
                    "endpoint": "https://indexer.example/api",
                    "token": "obsolete",
                },
                "source",
            )

    def test_caps_ignore_category_ids_that_are_not_ascii_digits(self):
        """str.isdigit() accepts digits int() then rejects, so caps must screen for ASCII."""
        caps = _parse_caps(NON_ASCII_DIGIT_CAPS)

        self.assertEqual(caps.categories, frozenset({5000}))

    def test_caps_treat_unknown_availability_values_as_available(self):
        caps = _parse_caps(CAPS.replace(b'available="yes"', b'available="future"', 1))

        self.assertIn("search", caps.operations)

        huge = b"9" * 10_000
        caps = _parse_caps(CAPS.replace(b'id="5000"', b'id="' + huge + b'"'))
        self.assertEqual(caps.categories, frozenset({2000}))

        caps = _parse_caps(CAPS.replace(b"tv-search", b"tv_search"))
        self.assertIn("tvsearch", caps.operations)

    def test_newznab_accepts_inert_doctype_but_rejects_entity_declarations(self):
        inert_doctype = CAPS.replace(
            b"?>",
            b'?><!DOCTYPE caps SYSTEM "caps.dtd">',
            1,
        )
        self.assertEqual(_parse_caps(inert_doctype), _parse_caps(CAPS))

        with self.assertRaisesRegex(NewznabError, "api_key_invalid"):
            _parse_caps(b'<error code="101" description="Incorrect key"/>')
        with self.assertRaisesRegex(NewznabError, "provider_response_invalid"):
            _parse_caps(b'<!DOCTYPE caps [<!ENTITY x "boom">]><caps>&x;</caps>')

    def test_redirect_statuses_keep_actionable_provider_codes(self):
        self.assertEqual(_status_error(429).code, "provider_limit_exhausted")
        self.assertEqual(_status_error(429, retry_after="50309").retry_after, 300)
        self.assertEqual(_status_error(503).code, "provider_unavailable")
        self.assertEqual(
            _status_error(None, fallback="provider_redirect_invalid").code,
            "provider_redirect_invalid",
        )

    def test_endpoint_rejects_an_invalid_port_at_configuration_boundary(self):
        for endpoint in (
            "https://indexer.example:0/api",
            "https://indexer.example:99999/api",
            "https://indexer.example/api\n",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaisesRegex(ValueError, "endpoint is invalid"),
            ):
                NewznabAccount(endpoint, "secret", "source")
