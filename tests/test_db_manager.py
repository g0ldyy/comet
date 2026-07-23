import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orjson

from comet.core.db_manager import DatabaseManager


class DatabaseManagerExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_writes_valid_json_lines_in_both_formats(self):
        rows = [{"id": 1, "value": "one"}, {"id": 2, "value": "two"}]

        for compress in (False, True):
            with self.subTest(compress=compress), tempfile.TemporaryDirectory() as tmp:
                database = AsyncMock()
                database.fetch_all.side_effect = [rows, []]
                manager = DatabaseManager(database=database)
                manager.get_table_info = AsyncMock(
                    return_value=manager_table_info(name="items", primary_key=["id"])
                )
                suffix = ".json.gz" if compress else ".json"
                output_file = Path(tmp) / f"items{suffix}"

                stats = await manager.export_table(
                    "items", output_file, compress=compress, batch_size=2
                )

                opener = gzip.open if compress else open
                with opener(output_file, "rb") as output:
                    records = [orjson.loads(line) for line in output]
                self.assertEqual(records[0]["table_name"], "items")
                self.assertEqual(records[1:], rows)
                self.assertEqual(stats.exported_rows, 2)

    async def test_primary_key_export_uses_keyset_pagination(self):
        database = AsyncMock()
        database.fetch_all.side_effect = [
            [{"tenant": "a", "id": 1}, {"tenant": "a", "id": 2}],
            [{"tenant": "b", "id": 1}],
            [],
        ]
        manager = DatabaseManager(database=database)
        table_info = manager_table_info(name="items", primary_key=["tenant", "id"])

        batches = [batch async for batch in manager._iter_export_batches(table_info, 2)]

        self.assertEqual([len(batch) for batch in batches], [2, 1])
        first_query, first_params = database.fetch_all.await_args_list[0].args
        second_query, second_params = database.fetch_all.await_args_list[1].args
        self.assertNotIn("OFFSET", first_query)
        self.assertNotIn("WHERE", first_query)
        self.assertEqual(first_params, {"batch_size": 2})
        self.assertIn("WHERE (tenant, id) > (:cursor_0, :cursor_1)", second_query)
        self.assertEqual(
            second_params,
            {"batch_size": 2, "cursor_0": "a", "cursor_1": 2},
        )

    async def test_export_without_primary_key_keeps_offset_pagination(self):
        database = AsyncMock()
        database.fetch_all.side_effect = [[{"value": 1}, {"value": 2}], []]
        manager = DatabaseManager(database=database)
        table_info = manager_table_info(name="items", primary_key=[])

        batches = [batch async for batch in manager._iter_export_batches(table_info, 2)]

        self.assertEqual([len(batch) for batch in batches], [2])
        _, second_params = database.fetch_all.await_args_list[1].args
        self.assertEqual(second_params, {"batch_size": 2, "offset": 2})


class DatabaseManagerImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_rows_are_counted_once_and_columns_stay_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp) / "items.json"
            input_file.write_bytes(
                b'{"table_name":"items"}\n'
                b'{"b":2,"a":1,"missing":9}\n'
                b"{broken\n"
                b"[]\n"
                b'{"a":3}\n'
            )
            manager = DatabaseManager(database=AsyncMock())
            manager.get_table_info = AsyncMock(
                return_value=manager_table_info(
                    name="items", primary_key=["a"], columns=["a", "b"]
                )
            )
            process_batch = AsyncMock(return_value=2)

            with patch.object(manager, "_process_batch", new=process_batch):
                stats = await manager.import_table(input_file, batch_size=10)

        self.assertEqual(stats.total_rows, 4)
        self.assertEqual(stats.inserted_rows, 2)
        self.assertEqual(stats.error_rows, 2)
        query, rows, table_name = process_batch.await_args.args
        self.assertIn("INSERT INTO items (b, a)", query)
        self.assertEqual(rows, [{"b": 2, "a": 1}, {"b": None, "a": 3}])
        self.assertEqual(table_name, "items")


def manager_table_info(
    *, name: str, primary_key: list[str], columns: list[str] | None = None
):
    from comet.core.db_manager import TableInfo

    return TableInfo(
        name=name,
        columns=columns or [],
        primary_key=primary_key,
        unique_constraints=[],
    )
