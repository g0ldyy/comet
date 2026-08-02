import asyncio
import sqlite3
import time
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory

from databases import Database

import comet.core.models  # noqa: F401
from comet.core.db_router import ReplicaAwareDatabase
from comet.core.event_store import EventFilters, EventStore, EventWrite
from comet.core.schema_migrations import run_schema_migrations
from comet.observability import events as event_pipeline


def event(
    name: str,
    *,
    created_at: float = 100,
    category: str = "SCRAPER",
    request_id: str | None = None,
    role: str = "web_worker",
) -> EventWrite:
    return EventWrite(
        created_at=created_at,
        instance_id="a" * 32,
        process_id=101,
        role=role,
        level="INFO",
        category=category,
        event=name,
        message=f"Event {name}",
        request_id=request_id,
        run_id=None,
        connection_id=None,
        media_type="movie",
        provider_name="test-provider",
        outcome="ok",
        error_code=None,
        details={"duration_ms": 12.5, "result_count": 3},
    )


class EventStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.database = ReplicaAwareDatabase(
            Database(f"sqlite+aiosqlite:///{self.temp_dir.name}/events.db")
        )
        await self.database.connect()
        await run_schema_migrations(
            self.database,
            is_sqlite=True,
            is_postgres=False,
        )
        self.store = EventStore(self.database)

    async def asyncTearDown(self):
        event_pipeline.stop_event_persistence()
        await self.database.disconnect()
        self.temp_dir.cleanup()

    async def test_batches_receive_monotonic_ids_and_persist_dropped_count(self):
        await self.store.append(
            [event("search.accepted"), event("search.completed")],
            dropped=2,
        )
        await EventStore(self.database).append([event("stream.started")], dropped=1)

        page = await self.store.page(EventFilters(), limit=10)
        self.assertEqual([item["id"] for item in page.items], [3, 2, 1])
        self.assertEqual(page.dropped_events, 3)
        self.assertEqual(page.items[0]["details"]["result_count"], 3)

    async def test_append_waits_for_a_concurrent_sqlite_writer(self):
        await self.database.execute("PRAGMA journal_mode=WAL")
        blocker = sqlite3.connect(f"{self.temp_dir.name}/events.db")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            """
            UPDATE operational_event_state
            SET dropped_events = dropped_events
            WHERE id = 1
            """
        )

        append = asyncio.create_task(self.store.append([event("search.concurrent")]))
        await asyncio.sleep(0.1)
        self.assertFalse(append.done())
        blocker.commit()
        blocker.close()
        await asyncio.wait_for(append, timeout=1)

        page = await self.store.page(EventFilters(), limit=10)
        self.assertEqual([item["event"] for item in page.items], ["search.concurrent"])

    async def test_prune_waits_for_a_concurrent_sqlite_writer(self):
        await self.store.append([event("search.old", created_at=0)])
        blocker = sqlite3.connect(f"{self.temp_dir.name}/events.db")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            """
            UPDATE operational_event_state
            SET dropped_events = dropped_events
            WHERE id = 1
            """
        )

        prune = asyncio.create_task(self.store.prune(max_age_seconds=1, now=2))
        await asyncio.sleep(0.1)
        self.assertFalse(prune.done())

        blocker.commit()
        blocker.close()

        self.assertEqual(await asyncio.wait_for(prune, timeout=1), 1)

    async def test_filters_cursor_and_ascending_resume_are_exact(self):
        request_id = "b" * 32
        await self.store.append(
            [
                event("search.accepted", request_id=request_id),
                event("database.ready", category="DATABASE"),
                event("search.completed", request_id=request_id),
            ]
        )

        first = await self.store.page(
            EventFilters(category="SCRAPER", request_id=request_id),
            limit=1,
        )
        self.assertEqual([item["id"] for item in first.items], [3])
        self.assertEqual(first.next_cursor, 3)
        second = await self.store.page(
            EventFilters(category="SCRAPER", request_id=request_id),
            limit=10,
            before=first.next_cursor,
        )
        self.assertEqual([item["id"] for item in second.items], [1])

        resumed = await self.store.page(
            EventFilters(),
            limit=10,
            after=1,
            ascending=True,
            include_dropped=False,
        )
        self.assertEqual([item["id"] for item in resumed.items], [2, 3])
        self.assertEqual(resumed.dropped_events, 0)

    async def test_search_covers_the_complete_stored_event(self):
        matching = replace(
            event("playback.started", category="PLAYBACK"),
            message="Started from the premium provider",
            details={"release": "Unique.Release.Name"},
        )
        await self.store.append([event("search.completed"), matching])

        by_message = await self.store.page(
            EventFilters(search="PREMIUM PROVIDER"), limit=10
        )
        by_details = await self.store.page(
            EventFilters(search="unique.release"), limit=10
        )

        self.assertEqual(
            [item["event"] for item in by_message.items], ["playback.started"]
        )
        self.assertEqual(
            [item["event"] for item in by_details.items], ["playback.started"]
        )

    async def test_retention_applies_age_and_row_bounds(self):
        now = time.time()
        await self.store.append(
            [
                event("search.old", created_at=now - 90_000),
                event(
                    "stream.recent",
                    category="STREAM",
                    created_at=now - 2 * 86_400,
                ),
                event("search.one", created_at=now),
                event("search.two", created_at=now),
                event("search.three", created_at=now),
            ]
        )
        removed = await self.store.prune(
            max_age_seconds=86_400,
            max_rows=10,
            now=now,
        )

        self.assertEqual(removed, 1)
        page = await self.store.page(EventFilters(), limit=10)
        self.assertEqual(
            [item["event"] for item in page.items],
            ["search.three", "search.two", "search.one", "stream.recent"],
        )

        removed = await self.store.prune(
            max_age_seconds=86_400,
            summary_age_seconds=86_400,
            max_rows=2,
            now=now,
        )
        self.assertEqual(removed, 2)
        page = await self.store.page(EventFilters(), limit=10)
        self.assertEqual(
            [item["event"] for item in page.items], ["search.three", "search.two"]
        )

    async def test_process_local_pipeline_flushes_one_bounded_batch_on_shutdown(self):
        event_pipeline.start_event_persistence(str(self.database.url))
        for index in range(10):
            event_pipeline.capture_event(
                {
                    "timestamp": "2026-07-31 00:00:00",
                    "level": "INFO",
                    "event": "search.completed",
                    "message": "Search completed",
                    "category": "SCRAPER",
                    "process_role": "web_worker",
                    "pid": 123,
                    "request_id": f"{index:032x}",
                    "result_count": index,
                }
            )
        event_pipeline.stop_event_persistence()

        page = await self.store.page(EventFilters(), limit=20)
        self.assertEqual(len(page.items), 10)
        self.assertEqual(page.items[0]["details"]["result_count"], 9)

    def test_process_buffer_counts_backpressure_without_growing(self):
        buffer = event_pipeline._EventBuffer()
        for index in range(event_pipeline._MAX_PENDING_EVENTS + 3):
            buffer.submit({"index": index})

        items, dropped = buffer.drain()
        self.assertEqual(len(items), event_pipeline._WRITE_BATCH_SIZE)
        self.assertEqual(dropped, 3)


if __name__ == "__main__":
    unittest.main()
