import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from databases import Database

import comet.services.debrid_account_scraper as account_scraper


@asynccontextmanager
async def _http_session_lease():
    yield object()


class DebridAccountSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_page_is_consumed_without_revalidation(self):
        client = type(
            "Client",
            (),
            {
                "list_magnets": AsyncMock(
                    return_value=(
                        [
                            {
                                "id": "one",
                                "hash": "a" * 40,
                            }
                        ],
                        500,
                    )
                )
            },
        )()

        self.assertEqual(
            await account_scraper._fetch_all_magnets(client),
            [{"id": "one", "hash": "a" * 40}],
        )
        client.list_magnets.assert_awaited_once_with(limit=500, offset=0)

    async def test_snapshot_scan_fetches_every_page(self):
        first_page = [
            {
                "id": str(index),
                "hash": f"{index:040x}",
            }
            for index in range(500)
        ]
        last_page = [{"id": "500", "hash": f"{500:040x}"}]
        client = type(
            "Client",
            (),
            {
                "list_magnets": AsyncMock(
                    side_effect=[(first_page, 501), (last_page, 501)]
                ),
            },
        )()

        result = await account_scraper._fetch_all_magnets(client)

        self.assertEqual(result, first_page + last_page)
        self.assertEqual(
            client.list_magnets.await_args_list,
            [call(limit=500, offset=0), call(limit=500, offset=500)],
        )

    async def test_failed_snapshot_replacement_rolls_back_all_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.db"
            database = Database(f"sqlite+aiosqlite:///{path}")
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE debrid_account_magnets (
                        debrid_service TEXT NOT NULL,
                        account_key_hash TEXT NOT NULL,
                        magnet_id TEXT NOT NULL,
                        info_hash TEXT NOT NULL,
                        name TEXT NOT NULL,
                        size BIGINT,
                        status TEXT NOT NULL,
                        added_at REAL NOT NULL,
                        PRIMARY KEY (debrid_service, account_key_hash, magnet_id)
                    )
                    """
                )
                await database.execute(
                    """
                    CREATE TABLE debrid_account_sync_state (
                        debrid_service TEXT NOT NULL,
                        account_key_hash TEXT NOT NULL,
                        last_sync_at REAL NOT NULL CHECK (last_sync_at < 0),
                        PRIMARY KEY (debrid_service, account_key_hash)
                    )
                    """
                )
                await database.execute(
                    """
                    INSERT INTO debrid_account_magnets (
                        debrid_service, account_key_hash, magnet_id, info_hash,
                        name, size, status, added_at
                    ) VALUES (
                        'realdebrid', 'account', 'old', 'old-hash',
                        'old', 1, 'cached', 1
                    )
                    """
                )

                replacement = {
                    "magnet_id": "new",
                    "info_hash": "new-hash",
                    "name": "new",
                    "size": 2,
                    "status": "cached",
                    "added_at": 2,
                }
                with patch.object(account_scraper, "database", database):
                    with self.assertRaises(sqlite3.IntegrityError):
                        await account_scraper._replace_account_snapshot(
                            "realdebrid", "account", 2, [replacement]
                        )

                rows = await database.fetch_all(
                    """
                    SELECT magnet_id, info_hash
                    FROM debrid_account_magnets
                    ORDER BY magnet_id
                    """
                )
                self.assertEqual(
                    [dict(row) for row in rows],
                    [{"magnet_id": "old", "info_hash": "old-hash"}],
                )
                self.assertIsNone(
                    await database.fetch_one("SELECT 1 FROM debrid_account_sync_state")
                )
            finally:
                await database.disconnect()


class DebridAccountTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await account_scraper.shutdown_account_sync_tasks()

    async def test_shutdown_releases_lock_when_task_has_not_started(self):
        lock = AsyncMock()
        sync = AsyncMock()

        with patch.object(account_scraper, "_sync_single_account", new=sync):
            task = account_scraper._schedule_sync_task(
                lock, "realdebrid", "key", "ip", "account"
            )
            await account_scraper.shutdown_account_sync_tasks()

        self.assertTrue(task.cancelled())
        sync.assert_not_awaited()
        lock.release.assert_awaited_once()
        self.assertFalse(account_scraper._background_tasks)

    async def test_shutdown_cancels_running_sync_and_releases_lock(self):
        started = asyncio.Event()

        async def sync_account(*args):
            started.set()
            await asyncio.Event().wait()

        lock = AsyncMock()

        async def run_locked(operation):
            return await operation

        lock.run.side_effect = run_locked
        with (
            patch.object(account_scraper, "_sync_single_account", new=sync_account),
            patch.object(
                account_scraper.http_client_manager,
                "bind",
                new=_http_session_lease,
            ),
        ):
            task = account_scraper._schedule_sync_task(
                lock, "alldebrid", "key", "ip", "account"
            )
            await started.wait()
            await account_scraper.shutdown_account_sync_tasks()

        self.assertTrue(task.cancelled())
        lock.release.assert_awaited()
        self.assertFalse(account_scraper._background_tasks)

    async def test_account_freshness_probes_start_concurrently(self):
        realdebrid_started = asyncio.Event()
        alldebrid_started = asyncio.Event()

        async def has_snapshot(service, account_key_hash, min_timestamp):
            del account_key_hash, min_timestamp
            if service == "realdebrid":
                realdebrid_started.set()
                await alldebrid_started.wait()
            else:
                alldebrid_started.set()
                await realdebrid_started.wait()
            return True

        entries = [
            {"service": "realdebrid", "apiKey": "first"},
            {"service": "alldebrid", "apiKey": "second"},
        ]
        with patch.object(account_scraper, "_has_fresh_snapshot", new=has_snapshot):
            await asyncio.wait_for(
                account_scraper.ensure_account_snapshot_ready(entries, "127.0.0.1"),
                timeout=1,
            )

        self.assertTrue(realdebrid_started.is_set())
        self.assertTrue(alldebrid_started.is_set())

    async def test_initial_wait_keeps_unfinished_sync_running(self):
        started = asyncio.Event()
        finish = asyncio.Event()
        lock = AsyncMock()
        lock.acquire.return_value = True

        async def run_locked(operation):
            return await operation

        async def sync_account(*args):
            started.set()
            await finish.wait()

        async def return_without_cancelling(tasks, *, timeout):
            self.assertGreater(timeout, 0)
            await started.wait()
            return set(), set(tasks)

        lock.run.side_effect = run_locked
        entries = [{"service": "realdebrid", "apiKey": "key"}]
        with (
            patch.object(
                account_scraper,
                "_get_fresh_snapshot_states",
                new=AsyncMock(return_value=[False]),
            ),
            patch.object(account_scraper, "DistributedLock", return_value=lock),
            patch.object(account_scraper, "_sync_single_account", new=sync_account),
            patch.object(
                account_scraper.http_client_manager,
                "bind",
                new=_http_session_lease,
            ),
            patch.object(
                account_scraper.asyncio, "wait", new=return_without_cancelling
            ),
        ):
            await account_scraper.ensure_account_snapshot_ready(entries, "127.0.0.1")

        tasks = tuple(account_scraper._background_tasks)
        self.assertEqual(len(tasks), 1)
        self.assertFalse(tasks[0].done())
        finish.set()
        await asyncio.gather(*tasks)

    async def test_refresh_state_reads_start_concurrently(self):
        scheduled = []
        realdebrid_started = asyncio.Event()
        alldebrid_started = asyncio.Event()

        async def fetch_one(query, params, force_primary):
            del query, force_primary
            if params["debrid_service"] == "realdebrid":
                realdebrid_started.set()
                await alldebrid_started.wait()
            else:
                alldebrid_started.set()
                await realdebrid_started.wait()
            return {"last_sync_at": account_scraper.time.time()}

        entries = [
            {"service": "realdebrid", "apiKey": "first"},
            {"service": "alldebrid", "apiKey": "second"},
        ]
        with patch.object(account_scraper.database, "fetch_one", new=fetch_one):
            account_scraper.schedule_account_snapshot_refresh(
                lambda *args: scheduled.append(args),
                entries,
                "127.0.0.1",
            )

            self.assertFalse(realdebrid_started.is_set())
            self.assertFalse(alldebrid_started.is_set())
            self.assertEqual(len(scheduled), 1)
            await asyncio.wait_for(
                scheduled[0][0](*scheduled[0][1:]),
                timeout=1,
            )

        self.assertTrue(realdebrid_started.is_set())
        self.assertTrue(alldebrid_started.is_set())
