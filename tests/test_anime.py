import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from comet.services.anime import AnimeMapper


class AnimeMapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_cached_entry_degrades_to_no_aliases(self):
        mapper = AnimeMapper()
        mapper.loaded = True

        with patch(
            "comet.services.anime.database.fetch_one",
            return_value={"data_json": "not-json"},
        ):
            aliases = await mapper.get_aliases("tt123")

        self.assertEqual(aliases, {})

    async def test_aliases_keep_only_ordered_unique_current_strings(self):
        mapper = AnimeMapper()
        mapper.loaded = True
        payload = b'{"title":"Main","synonyms":["Alt",null,"Main",42,"Alt"]}'

        with patch(
            "comet.services.anime.database.fetch_one",
            return_value={"data_json": payload},
        ):
            aliases = await mapper.get_aliases("tt123")

        self.assertEqual(aliases, {"original": ["Main"], "ez": ["Alt"]})

    async def test_media_type_uses_persisted_anime_type(self):
        mapper = AnimeMapper()

        with patch.object(mapper, "_get_entry_data", return_value={"type": "Movie"}):
            self.assertEqual(await mapper.get_media_type("kitsu:1"), "movie")

        with patch.object(mapper, "_get_entry_data", return_value={"type": "TV"}):
            self.assertEqual(await mapper.get_media_type("kitsu:2"), "series")

        with patch.object(mapper, "_get_entry_data", return_value={"type": "UNKNOWN"}):
            self.assertIsNone(await mapper.get_media_type("kitsu:3"))

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

    async def test_refresh_completion_observes_unexpected_error(self):
        mapper = AnimeMapper()

        async def refresh():
            raise RuntimeError("unexpected refresh failure")

        with patch("comet.services.anime.logger.warning") as warning:
            mapper._refresh_task = asyncio.create_task(refresh())
            mapper._refresh_task.add_done_callback(mapper._handle_refresh_task_done)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        self.assertIsNone(mapper._refresh_task)
        warning.assert_called_once_with(
            "Anime mapping refresh task failed: unexpected refresh failure"
        )

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

    async def test_kitsu_cache_load_is_atomic_on_invalid_row(self):
        mapper = AnimeMapper()
        mapper.anime_imdb_ids = {"tt-old"}
        mapper._kitsu_mapping_cache = {"old": {"imdb_id": "tt-old"}}
        mapper._imdb_kitsu_mapping_cache = {"tt-old": ["old"]}
        kitsu_rows = [
            {
                "source_id": "new",
                "target_id": "tt-new",
                "from_season": 2,
                "from_episode": None,
            },
            {"source_id": "broken"},
        ]

        with (
            patch(
                "comet.services.anime.database.fetch_all",
                side_effect=[[{"provider_id": "tt-new"}], kitsu_rows],
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
        rows = [{"provider_id": "tt-new"}, {}]

        with (
            patch("comet.services.anime.database.fetch_all", return_value=rows),
            self.assertRaises(KeyError),
        ):
            await mapper._load_mapping_caches()

        self.assertEqual(mapper.anime_imdb_ids, {"tt-old"})
