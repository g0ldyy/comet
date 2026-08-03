import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from comet.services import anime
from comet.services.anime import AnimeMapper


class AnimeMapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_cached_entry_is_not_treated_as_no_aliases(self):
        mapper = AnimeMapper()
        mapper.loaded = True

        with patch(
            "comet.services.anime.database.fetch_one",
            return_value={"data_json": "not-json"},
        ):
            with self.assertRaisesRegex(ValueError, "anime JSON"):
                await mapper.get_aliases("tt123")

    async def test_oversized_cached_entry_is_not_treated_as_no_aliases(self):
        mapper = AnimeMapper()
        mapper.loaded = True

        with patch(
            "comet.services.anime.database.fetch_one",
            return_value={"data_json": "x" * (anime._MAX_ENTRY_JSON_BYTES + 1)},
        ):
            with self.assertRaisesRegex(ValueError, "cached anime entry"):
                await mapper.get_aliases("tt123")

    async def test_aliases_keep_ordered_unique_current_strings(self):
        mapper = AnimeMapper()
        mapper.loaded = True
        payload = b'{"title":"Main","synonyms":["Alt","Main","Alt"]}'

        with patch(
            "comet.services.anime.database.fetch_one",
            return_value={"data_json": payload},
        ):
            aliases = await mapper.get_aliases("tt123")

        self.assertEqual(aliases, {"original": ["Main"], "ez": ["Alt"]})

    async def test_optional_aliases_keep_only_bounded_usable_titles(self):
        mapper = AnimeMapper()
        mapper.loaded = True
        cases = (
            (
                {"title": "Main", "synonyms": ["Valid", "unsafe\x00alias"]},
                {"original": ["Main"], "ez": ["Valid"]},
            ),
            (
                {
                    "title": "Main",
                    "synonyms": [f"Alias {index}" for index in range(65)],
                },
                {
                    "original": ["Main"],
                    "ez": [f"Alias {index}" for index in range(64)],
                },
            ),
        )
        for payload, expected in cases:
            with (
                self.subTest(payload=payload),
                patch(
                    "comet.services.anime.database.fetch_one",
                    return_value={"data_json": anime.orjson.dumps(payload)},
                ),
            ):
                self.assertEqual(await mapper.get_aliases("tt123"), expected)

    def test_unloaded_mapping_does_not_classify_everything_as_anime(self):
        mapper = AnimeMapper()
        self.assertFalse(mapper.is_anime_content("tt1234567", "tt1234567"))

    def test_malformed_kitsu_identifier_is_rejected(self):
        self.assertEqual(AnimeMapper._parse_media_id("kitsu"), (None, None))
        self.assertEqual(AnimeMapper._parse_media_id("kitsu:"), (None, None))

    async def test_stop_cancels_and_drains_background_refresh(self):
        mapper = AnimeMapper()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def refresh():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        mapper._refresh_task = asyncio.create_task(refresh())
        mapper._refresh_task.add_done_callback(mapper._handle_refresh_task_done)
        await started.wait()

        await mapper.stop()

        self.assertTrue(cancelled.is_set())
        self.assertIsNone(mapper._refresh_task)

    async def test_refresh_completion_clears_task_reference(self):
        mapper = AnimeMapper()

        async def refresh():
            raise RuntimeError("unexpected refresh failure")

        task = asyncio.create_task(refresh())
        mapper._refresh_task = task
        task.add_done_callback(mapper._handle_refresh_task_done)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertIsNone(mapper._refresh_task)
        self.assertIsInstance(task.exception(), RuntimeError)

    async def test_remote_mapping_rolls_back_if_overrides_fail(self):
        mapper = AnimeMapper()

        class Transaction:
            def __init__(self):
                self.exit_error = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, error_type, error, traceback):
                self.exit_error = error

        transaction = Transaction()
        mapping = AsyncMock(return_value=2)
        overrides = AsyncMock(side_effect=RuntimeError("override write failed"))

        with (
            patch(
                "comet.services.anime.database.transaction", return_value=transaction
            ),
            patch.object(mapper, "_persist_mapping", mapping),
            patch.object(mapper, "_persist_provider_overrides", overrides),
            self.assertRaisesRegex(RuntimeError, "override write failed"),
        ):
            await mapper._persist_remote_mapping([], [], [])

        mapping.assert_awaited_once_with([], [])
        overrides.assert_awaited_once_with([])
        self.assertIsInstance(transaction.exit_error, RuntimeError)

    async def test_mapping_persistence_keeps_usable_external_neighbors(self):
        mapper = AnimeMapper()

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        persisted_rows = []

        async def capture_rows(_query, rows):
            persisted_rows.extend(dict(row) for row in rows)

        execute_many = AsyncMock(side_effect=capture_rows)
        with (
            patch(
                "comet.services.anime.database.transaction",
                return_value=Transaction(),
            ),
            patch(
                "comet.services.anime.database.execute",
                new=AsyncMock(),
            ),
            patch(
                "comet.services.anime.database.execute_many",
                new=execute_many,
            ),
        ):
            count = await mapper._persist_mapping(
                [
                    None,
                    {
                        "title": "Entry without usable sources",
                        "sources": "not-a-list",
                    },
                    {
                        "title": "Main",
                        "sources": ["https://anilist.co/anime/123/title"],
                    },
                ],
                [
                    {
                        "anilist_id": 123,
                        "imdb_id": ["", "tt1234567"],
                    }
                ],
            )

        self.assertEqual(count, 2)
        self.assertIn(
            {"provider": "anilist", "provider_id": "123", "entry_id": 3},
            persisted_rows,
        )
        self.assertIn(
            {"provider": "imdb", "provider_id": "tt1234567", "entry_id": 3},
            persisted_rows,
        )

    async def test_kitsu_persistence_keeps_zero_sentinel_and_skips_unmapped_rows(self):
        mapper = AnimeMapper()

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        persisted_rows = []

        async def capture_rows(_query, rows):
            persisted_rows.extend(dict(row) for row in rows)

        execute_many = AsyncMock(side_effect=capture_rows)
        with (
            patch(
                "comet.services.anime.database.transaction",
                return_value=Transaction(),
            ),
            patch(
                "comet.services.anime.database.execute",
                new=AsyncMock(),
            ),
            patch(
                "comet.services.anime.database.execute_many",
                new=execute_many,
            ),
        ):
            count = await mapper._persist_provider_overrides(
                [
                    {"kitsu_id": 1},
                    {
                        "kitsu_id": 2,
                        "imdb_id": "tt1234567",
                        "fromSeason": 0,
                        "fromEpisode": 56,
                    },
                ]
            )

        self.assertEqual(count, 1)
        execute_many.assert_awaited_once()
        self.assertEqual(
            persisted_rows,
            [
                {
                    "source_id": "2",
                    "target_id": "tt1234567",
                    "from_season": 0,
                    "from_episode": 56,
                }
            ],
        )

    async def test_remote_refresh_uses_bounded_pinned_fetches(self):
        mapper = AnimeMapper()
        payloads = {
            mapper._aod_url: b'{"data":[]}',
            mapper._fribb_url: b"[]",
            mapper._kitsu_imdb_url: b"[]",
        }

        @asynccontextmanager
        async def refresh_lock():
            yield

        with (
            patch.object(
                mapper,
                "_load_from_database",
                new=AsyncMock(return_value=False),
            ),
            patch.object(
                mapper,
                "_persist_remote_mapping",
                new=AsyncMock(return_value=0),
            ) as persist,
            patch.object(
                mapper,
                "_load_mapping_caches",
                new=AsyncMock(),
            ),
            patch(
                "comet.services.anime._anime_refresh_lock",
                new=refresh_lock,
            ),
            patch(
                "comet.services.anime.fetch_http_bytes",
                new=AsyncMock(side_effect=lambda url, **_kwargs: payloads[url]),
            ) as fetch,
            patch("comet.services.anime.log.info") as info,
        ):
            self.assertTrue(await mapper._refresh_from_remote())

        persist.assert_awaited_once_with([], [], [])
        self.assertEqual(fetch.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in info.call_args_list],
            [
                "anime.download.started",
                "anime.download.completed",
                "anime.refresh.completed",
            ],
        )
        self.assertEqual(info.call_args_list[1].kwargs["response_bytes"], 15)
        self.assertEqual(info.call_args_list[2].kwargs["item_count"], 0)
        self.assertEqual(info.call_args_list[2].kwargs["outcome"], "ok")
        expected_limits = {
            mapper._aod_url: anime._AOD_MAX_BYTES,
            mapper._fribb_url: anime._FRIBB_MAX_BYTES,
            mapper._kitsu_imdb_url: anime._KITSU_MAX_BYTES,
        }
        for call in fetch.await_args_list:
            self.assertEqual(call.kwargs["max_bytes"], expected_limits[call.args[0]])
            self.assertEqual(call.kwargs["headers"], {"Accept": "application/json"})
            self.assertEqual(call.kwargs["redirects"], 3)

    async def test_remote_refresh_cancels_sibling_fetches_after_failure(self):
        mapper = AnimeMapper()
        siblings_started = 0
        both_siblings_started = asyncio.Event()
        cancelled = 0

        async def fetch(url, **_kwargs):
            nonlocal siblings_started, cancelled
            if url == mapper._aod_url:
                await both_siblings_started.wait()
                raise anime.OutboundUrlError("source failed")
            siblings_started += 1
            if siblings_started == 2:
                both_siblings_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled += 1

        @asynccontextmanager
        async def refresh_lock():
            yield

        with (
            patch.object(
                mapper,
                "_load_from_database",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "comet.services.anime._anime_refresh_lock",
                new=refresh_lock,
            ),
            patch("comet.services.anime.fetch_http_bytes", new=fetch),
            patch("comet.services.anime.log.warning") as warning,
        ):
            self.assertFalse(await mapper._refresh_from_remote())

        self.assertEqual(cancelled, 2)
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[0], "anime.download.failed")
        self.assertEqual(warning.call_args.kwargs["failure_reason"], "network_error")
        self.assertEqual(warning.call_args.kwargs["outcome"], "failed")
        self.assertIsInstance(warning.call_args.kwargs["exc"], BaseExceptionGroup)

    def test_remote_json_uses_normal_key_semantics_and_identities_remain_scoped(self):
        self.assertEqual(
            anime._decode_json(b'{"data":[1],"data":[]}'),
            {"data": []},
        )
        self.assertEqual(
            anime._provider_identity("https://anilist.co/anime/123/title"),
            ("anilist", "123"),
        )
        self.assertIsNone(
            anime._provider_identity("https://anilist.co.evil.example/anime/123")
        )
        self.assertEqual(
            anime._provider_identity("https://user:secret@anilist.co/anime/123"),
            ("anilist", "123"),
        )
        self.assertEqual(
            anime._provider_identity("https://media.anilist.example/anime/456"),
            ("anilist", "456"),
        )
        self.assertEqual(anime._coordinate(0), 0)
        self.assertEqual(anime._coordinate(10_001), 10_001)
        self.assertEqual(anime._imdb_id("tt1234567"), "tt1234567")
        self.assertEqual(
            anime._imdb_id("tt12345678901234567890"),
            "tt12345678901234567890",
        )
        self.assertIsNone(anime._imdb_id("https://imdb.example/title/tt1234567"))

    async def test_database_cache_failures_are_not_remote_cache_misses(self):
        mapper = AnimeMapper()
        with (
            patch(
                "comet.services.anime.database.fetch_val",
                new=AsyncMock(return_value=1),
            ),
            patch.object(
                mapper,
                "_load_mapping_caches",
                new=AsyncMock(side_effect=RuntimeError("cache corruption")),
            ),
            self.assertRaisesRegex(RuntimeError, "cache corruption"),
        ):
            await mapper._load_from_database(schedule_refresh=False)

    async def test_unexpected_download_failure_surfaces(self):
        mapper = AnimeMapper()

        @asynccontextmanager
        async def refresh_lock():
            yield

        with (
            patch.object(
                mapper,
                "_load_from_database",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "comet.services.anime._anime_refresh_lock",
                new=refresh_lock,
            ),
            patch(
                "comet.services.anime.fetch_http_bytes",
                new=AsyncMock(side_effect=RuntimeError("download bug")),
            ),
            self.assertRaises(ExceptionGroup) as raised,
        ):
            await mapper._refresh_from_remote()

        self.assertIsInstance(raised.exception.exceptions[0], RuntimeError)

    async def test_kitsu_cache_load_is_atomic_on_invalid_row(self):
        mapper = AnimeMapper()
        mapper.anime_imdb_ids = {"tt-old"}
        mapper._kitsu_mapping_cache = {"old": {"imdb_id": "tt-old"}}
        mapper._imdb_kitsu_mapping_cache = {"tt-old": ["old"]}
        kitsu_rows = [
            {
                "source_id": "123",
                "target_id": "tt1234567",
                "from_season": 2,
                "from_episode": None,
            },
            {"source_id": "broken"},
        ]

        with (
            patch(
                "comet.services.anime.database.fetch_all",
                side_effect=[[{"provider_id": "tt1234567"}], kitsu_rows],
            ),
            self.assertRaises(KeyError),
        ):
            await mapper._load_mapping_caches()

        self.assertEqual(mapper.anime_imdb_ids, {"tt-old"})
        self.assertEqual(
            mapper._kitsu_mapping_cache,
            {"old": {"imdb_id": "tt-old"}},
        )
        self.assertEqual(mapper._imdb_kitsu_mapping_cache, {"tt-old": ["old"]})

    async def test_provider_id_load_is_atomic_on_invalid_row(self):
        mapper = AnimeMapper()
        mapper.anime_imdb_ids = {"tt-old"}
        rows = [{"provider_id": "tt1234567"}, {}]

        with (
            patch("comet.services.anime.database.fetch_all", return_value=rows),
            self.assertRaises(KeyError),
        ):
            await mapper._load_mapping_caches()

        self.assertEqual(mapper.anime_imdb_ids, {"tt-old"})
