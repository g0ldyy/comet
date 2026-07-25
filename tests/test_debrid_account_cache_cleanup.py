import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orjson
from databases import Database
from RTN import parse

import comet.services.debrid_account_cache_cleanup as cache_cleanup
from comet.utils.parsing import default_dump


class DebridAccountCacheCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_deletes_only_proven_invalid_account_associations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cleanup.db"
            database = Database(f"sqlite+aiosqlite:///{path}")
            await database.connect()
            try:
                await database.execute(
                    """
                    CREATE TABLE torrents (
                        media_id TEXT NOT NULL,
                        info_hash TEXT NOT NULL,
                        title TEXT NOT NULL,
                        tracker TEXT,
                        parsed_json TEXT NOT NULL
                    )
                    """
                )

                def row(info_hash, title, tracker="DebridAccount|realdebrid"):
                    return {
                        "media_id": "tt29552248",
                        "info_hash": info_hash,
                        "title": title,
                        "tracker": tracker,
                        "parsed_json": orjson.dumps(
                            default_dump(parse(title))
                        ).decode(),
                    }

                valid_hash = "a" * 40
                invalid_hash = "b" * 40
                mixed_hash = "c" * 40
                corrupt_hash = "d" * 40
                rows = [
                    row(valid_hash, "Swapped.2026.1080p.WEB-DL"),
                    row(invalid_hash, "The.Big.Bang.Theory.S09E01.1080p"),
                    row(
                        invalid_hash,
                        "The.Big.Bang.Theory.S09E01.1080p",
                        "Torrentio|1337x",
                    ),
                    row(mixed_hash, "Friends.S01E01.1080p"),
                    row(mixed_hash, "Pookoo.2026.1080p"),
                    {
                        **row(corrupt_hash, "Unknown.Release"),
                        "parsed_json": "not-json",
                    },
                ]
                await database.execute_many(
                    """
                    INSERT INTO torrents (
                        media_id, info_hash, title, tracker, parsed_json
                    ) VALUES (
                        :media_id, :info_hash, :title, :tracker, :parsed_json
                    )
                    """,
                    rows,
                )

                params = {
                    "media_id": "tt29552248",
                    "media_type": "movie",
                    "title": "Swapped",
                    "year": 2026,
                    "year_end": None,
                    "aliases": {"us": ["Pookoo"]},
                    "batch_size": 2,
                }

                class PrimaryCompatibleDatabase:
                    async def fetch_all(self, query, values=None, **kwargs):
                        kwargs.pop("force_primary", None)
                        return await database.fetch_all(query, values, **kwargs)

                    async def execute(self, query, values=None):
                        return await database.execute(query, values)

                with patch.object(
                    cache_cleanup, "database", PrimaryCompatibleDatabase()
                ):
                    dry_run = await cache_cleanup.repair_debrid_account_cache_for_media(
                        **params, apply=False
                    )
                    self.assertEqual(dry_run.scanned_hashes, 4)
                    self.assertEqual(dry_run.matched_hashes, 2)
                    self.assertEqual(dry_run.invalid_hashes, 1)
                    self.assertEqual(dry_run.invalid_rows, 1)
                    self.assertEqual(dry_run.unverifiable_hashes, 1)

                    await cache_cleanup.repair_debrid_account_cache_for_media(
                        **params, apply=True
                    )

                remaining = await database.fetch_all(
                    "SELECT info_hash, tracker FROM torrents ORDER BY info_hash, tracker"
                )
                self.assertEqual(
                    [(value["info_hash"], value["tracker"]) for value in remaining],
                    [
                        (valid_hash, "DebridAccount|realdebrid"),
                        (invalid_hash, "Torrentio|1337x"),
                        (mixed_hash, "DebridAccount|realdebrid"),
                        (mixed_hash, "DebridAccount|realdebrid"),
                        (corrupt_hash, "DebridAccount|realdebrid"),
                    ],
                )
            finally:
                await database.disconnect()


if __name__ == "__main__":
    unittest.main()
