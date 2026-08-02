import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import orjson

import comet.core.db_manager as db_manager_module
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
                self.assertEqual(output_file.stat().st_mode & 0o777, 0o600)

    async def test_failed_export_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "items.json"
            output_file.write_bytes(b"previous-backup")
            database = AsyncMock()
            database.fetch_all.side_effect = [[{"id": 1, "value": "x" * 64}], []]
            manager = DatabaseManager(database=database)
            manager.get_table_info = AsyncMock(
                return_value=manager_table_info(name="items", primary_key=["id"])
            )

            with (
                patch.object(db_manager_module, "_MAX_IMPORT_ROW_BYTES", 32),
                self.assertRaisesRegex(ValueError, "export record limit"),
            ):
                await manager.export_table(
                    "items",
                    output_file,
                    compress=False,
                    batch_size=2,
                )

            self.assertEqual(output_file.read_bytes(), b"previous-backup")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

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
        self.assertEqual(stats.processed_rows, 2)
        self.assertEqual(stats.error_rows, 2)
        query, rows = process_batch.await_args.args
        self.assertIn("INSERT INTO items (b, a)", query)
        self.assertEqual(rows, [{"b": 2, "a": 1}, {"b": None, "a": 3}])

    async def test_import_rejects_noncanonical_table_before_sql(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp) / "items.json"
            input_file.write_bytes(
                b'{"table_name":"items; DROP TABLE users"}\n{"a":1}\n'
            )
            database = AsyncMock()
            manager = DatabaseManager(database=database)

            with self.assertRaisesRegex(ValueError, "canonical database identifier"):
                await manager.import_table(input_file)

        database.fetch_all.assert_not_awaited()
        database.fetch_val.assert_not_awaited()

    async def test_oversized_rows_are_bounded_and_duplicate_keys_use_last_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp) / "items.json"
            input_file.write_bytes(
                b'{"table_name":"items"}\n'
                b'{"a":"' + b"x" * 64 + b'"}\n'
                b'{"a":1,"a":2}\n'
                b'{"a":3}\n'
            )
            manager = DatabaseManager(database=AsyncMock())
            manager.get_table_info = AsyncMock(
                return_value=manager_table_info(
                    name="items",
                    primary_key=["a"],
                    columns=["a"],
                )
            )
            process_batch = AsyncMock(return_value=2)

            with (
                patch.object(db_manager_module, "_MAX_IMPORT_ROW_BYTES", 32),
                patch.object(manager, "_process_batch", new=process_batch),
            ):
                stats = await manager.import_table(input_file, batch_size=10)

        self.assertEqual(stats.total_rows, 3)
        self.assertEqual(stats.error_rows, 1)
        self.assertEqual(stats.processed_rows, 2)
        self.assertEqual(process_batch.await_args.args[1], [{"a": 2}, {"a": 3}])

    async def test_batch_failure_log_does_not_include_driver_message(self):
        secret = "credential=must-not-be-logged"
        database = AsyncMock()
        database.transaction = Mock(return_value=AsyncMock())
        database.execute_many.side_effect = RuntimeError(secret)
        manager = DatabaseManager(database=database)

        with (
            self.assertRaisesRegex(RuntimeError, "must-not-be-logged"),
        ):
            await manager._process_batch_with_retry(
                "INSERT INTO items (a) VALUES (:a)",
                [{"a": 1}],
                max_retries=0,
            )

    async def test_individual_database_failures_are_counted_as_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_file = Path(tmp) / "items.json"
            input_file.write_bytes(b'{"table_name":"items"}\n{"a":1}\n{"a":2}\n')
            manager = DatabaseManager(database=AsyncMock())
            manager.get_table_info = AsyncMock(
                return_value=manager_table_info(
                    name="items",
                    primary_key=["a"],
                    columns=["a"],
                )
            )

            with patch.object(
                manager,
                "_process_batch",
                new=AsyncMock(return_value=1),
            ):
                stats = await manager.import_table(input_file, batch_size=10)

        self.assertEqual(stats.total_rows, 2)
        self.assertEqual(stats.processed_rows, 1)
        self.assertEqual(stats.error_rows, 1)


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
