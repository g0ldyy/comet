import asyncio
import gzip
import json
import os
import random
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import orjson
from databases import Database

from comet.core.database import IS_SQLITE
from comet.core.models import settings

_DATABASE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_MAX_IMPORT_METADATA_BYTES = 64 * 1024
_MAX_IMPORT_ROW_BYTES = 8 * 1024 * 1024
_MAX_IMPORT_OBJECT_KEYS = 1_024
_MAX_EXPORT_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_DATABASE_BATCH_SIZE = 100_000
_OVERSIZED_RECORD = object()


def _validate_identifier(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or _DATABASE_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"{field} is not a canonical database identifier")
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key not in result and len(result) >= _MAX_IMPORT_OBJECT_KEYS:
            raise ValueError("too many JSON keys")
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError("invalid JSON constant")


def _decode_json_object(document: bytes) -> dict:
    try:
        payload = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("invalid import JSON object") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid import JSON object")
    return payload


def _read_bounded_record(stream: BinaryIO, maximum: int):
    document = stream.readline(maximum + 1)
    if not document:
        return None
    if len(document) <= maximum:
        return document.strip()

    while document and not document.endswith(b"\n"):
        document = stream.readline(maximum + 1)
    return _OVERSIZED_RECORD


def _iter_bounded_records(stream: BinaryIO, maximum: int) -> Iterator[bytes | None]:
    while True:
        document = _read_bounded_record(stream, maximum)
        if document is None:
            return
        if document is _OVERSIZED_RECORD:
            yield None
        else:
            yield document


def _normalize_batch_size(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_DATABASE_BATCH_SIZE
    ):
        raise ValueError(
            f"database batch size must be between 1 and {_MAX_DATABASE_BATCH_SIZE}"
        )
    return value


def _open_private_output(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _run_file_io(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


@dataclass
class TableInfo:
    name: str
    columns: list[str]
    primary_key: list[str]
    unique_constraints: list[dict[str, Any]]
    row_count: int = 0


@dataclass
class ImportStats:
    table: str
    total_rows: int
    processed_rows: int
    error_rows: int
    duration_seconds: float


@dataclass
class ExportStats:
    table: str
    exported_rows: int
    duration_seconds: float
    file_size_mb: float


class DatabaseManager:
    def __init__(self, database: Database):
        self.database = database
        self.batch_size = _normalize_batch_size(settings.DATABASE_BATCH_SIZE)
        self._lock_retry_count = 0

    async def _get_sqlite_table_info(self, table_name: str) -> TableInfo:
        table_name = _validate_identifier(table_name, field="table name")
        columns_result = await self.database.fetch_all(
            f"PRAGMA table_info({table_name})"
        )
        columns = [
            _validate_identifier(row["name"], field="column name")
            for row in columns_result
        ]
        primary_key = [row["name"] for row in columns_result if row["pk"]]

        indexes_result = await self.database.fetch_all(
            f"PRAGMA index_list({table_name})"
        )
        unique_constraints = []
        for index in indexes_result:
            if not index["unique"]:
                continue

            index_name = _validate_identifier(index["name"], field="index name")
            index_info = await self.database.fetch_all(
                f"PRAGMA index_info({index_name})"
            )
            sql_result = await self.database.fetch_one(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=:name",
                {"name": index_name},
            )
            condition = None
            if sql_result and sql_result["sql"] and "WHERE" in sql_result["sql"]:
                condition = sql_result["sql"].split("WHERE", 1)[1].strip()

            unique_constraints.append(
                {
                    "name": index_name,
                    "columns": [col["name"] for col in index_info],
                    "condition": condition,
                }
            )

        return TableInfo(
            name=table_name,
            columns=columns,
            primary_key=primary_key,
            unique_constraints=unique_constraints,
        )

    async def _get_postgres_table_info(self, table_name: str) -> TableInfo:
        table_name = _validate_identifier(table_name, field="table name")
        columns_result = await self.database.fetch_all(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = :table_name
              AND table_schema = current_schema()
            ORDER BY ordinal_position
        """,
            {"table_name": table_name},
        )
        pk_result = await self.database.fetch_all(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
             AND tc.table_name = kcu.table_name
            WHERE tc.table_name = :table_name
              AND tc.table_schema = current_schema()
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
        """,
            {"table_name": table_name},
        )
        unique_result = await self.database.fetch_all(
            """
            SELECT
                c.conname as constraint_name,
                array_agg(a.attname ORDER BY k.ordinality) as columns,
                pg_get_expr(c.conbin, c.conrelid) as condition
            FROM pg_constraint c
            JOIN pg_class t ON c.conrelid = t.oid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE t.relname = :table_name
              AND n.nspname = current_schema()
              AND c.contype = 'u'
            GROUP BY c.conname, c.conbin, c.conrelid

            UNION ALL

            SELECT
                idx.indexname as constraint_name,
                array_agg(a.attname ORDER BY k.ordinality) as columns,
                pg_get_expr(i.indpred, i.indrelid) as condition
            FROM pg_indexes idx
            JOIN pg_class t ON t.relname = idx.tablename
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_index i ON i.indrelid = t.oid
            JOIN pg_class ic ON ic.oid = i.indexrelid AND ic.relname = idx.indexname
            JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE idx.tablename = :table_name
              AND idx.schemaname = current_schema()
              AND n.nspname = current_schema()
              AND i.indisunique = true
              AND NOT i.indisprimary
            GROUP BY idx.indexname, i.indpred, i.indrelid
        """,
            {"table_name": table_name},
        )
        return TableInfo(
            name=table_name,
            columns=[row["column_name"] for row in columns_result],
            primary_key=[row["column_name"] for row in pk_result],
            unique_constraints=[
                {
                    "name": row["constraint_name"],
                    "columns": row["columns"],
                    "condition": row["condition"],
                }
                for row in unique_result
            ],
        )

    async def get_table_info(self, table_name: str):
        table_name = _validate_identifier(table_name, field="table name")
        table_info = (
            await self._get_sqlite_table_info(table_name)
            if IS_SQLITE
            else await self._get_postgres_table_info(table_name)
        )

        # Get row count
        count_result = await self.database.fetch_val(
            f"SELECT COUNT(*) FROM {table_name}"
        )

        table_info.row_count = count_result or 0
        return table_info

    def _build_export_query(
        self,
        table_name: str,
        primary_key: list[str],
        batch_size: int,
        offset: int,
        last_primary_key: tuple | None = None,
    ) -> tuple[str, dict]:
        table_name = _validate_identifier(table_name, field="table name")
        primary_key = [
            _validate_identifier(column, field="primary-key column")
            for column in primary_key
        ]
        batch_size = _normalize_batch_size(batch_size)
        params = {"batch_size": batch_size}
        if primary_key:
            where_clause = ""
            if last_primary_key is not None:
                cursor_params = []
                for index, value in enumerate(last_primary_key):
                    param_name = f"cursor_{index}"
                    params[param_name] = value
                    cursor_params.append(f":{param_name}")
                where_clause = (
                    f"WHERE ({', '.join(primary_key)}) > ({', '.join(cursor_params)}) "
                )
            return (
                (
                    f"SELECT * FROM {table_name} {where_clause}"
                    f"ORDER BY {', '.join(primary_key)} LIMIT :batch_size"
                ),
                params,
            )

        params["offset"] = offset
        return (
            f"SELECT * FROM {table_name} LIMIT :batch_size OFFSET :offset",
            params,
        )

    async def _iter_export_batches(self, table_info: TableInfo, batch_size: int):
        offset = 0
        last_primary_key = None
        while True:
            query, params = self._build_export_query(
                table_info.name,
                table_info.primary_key,
                batch_size,
                offset,
                last_primary_key,
            )
            rows = await self.database.fetch_all(query, params)
            if not rows:
                return
            yield rows

            if table_info.primary_key:
                last_row = rows[-1]
                last_primary_key = tuple(
                    last_row[column] for column in table_info.primary_key
                )
            else:
                offset += len(rows)

    @staticmethod
    def _serialize_export_chunks(rows) -> Iterator[bytes]:
        chunk = bytearray()
        for row in rows:
            document = orjson.dumps(dict(row)) + b"\n"
            if len(document) > _MAX_IMPORT_ROW_BYTES:
                raise ValueError("database row exceeds the export record limit")
            if chunk and len(chunk) + len(document) > _MAX_EXPORT_CHUNK_BYTES:
                yield bytes(chunk)
                chunk.clear()
            chunk.extend(document)
        if chunk:
            yield bytes(chunk)

    async def list_tables(self):
        if IS_SQLITE:
            result = await self.database.fetch_all("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name != 'sqlite_sequence'
                ORDER BY name
            """)
        else:
            result = await self.database.fetch_all("""
                SELECT table_name as name
                FROM information_schema.tables 
                WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)

        return [row["name"] for row in result]

    async def export_table(
        self,
        table_name: str,
        output_file: Path,
        compress: bool = True,
        batch_size: int | None = None,
    ):
        start_time = time.time()
        table_name = _validate_identifier(table_name, field="table name")
        batch_size = _normalize_batch_size(
            self.batch_size if batch_size is None else batch_size
        )

        table_info = await self.get_table_info(table_name)

        exported_rows = 0

        metadata = {
            "table_name": table_name,
            "export_timestamp": datetime.now(UTC).isoformat(),
        }
        metadata_payload = orjson.dumps(metadata) + b"\n"
        temporary = output_file.with_name(
            f".{output_file.name}.{secrets.token_hex(8)}.tmp"
        )
        raw_output = None
        output = None
        try:
            raw_output = _open_private_output(temporary)
            output = (
                gzip.GzipFile(fileobj=raw_output, mode="wb") if compress else raw_output
            )
            await _run_file_io(output.write, metadata_payload)
            async for rows in self._iter_export_batches(table_info, batch_size):
                for chunk in self._serialize_export_chunks(rows):
                    await _run_file_io(output.write, chunk)
                exported_rows += len(rows)
            if output is not raw_output:
                await _run_file_io(output.close)
                output = None
            await _run_file_io(raw_output.flush)
            await _run_file_io(os.fsync, raw_output.fileno())
            if output is raw_output:
                output = None
            await _run_file_io(raw_output.close)
            raw_output = None
            await _run_file_io(os.replace, temporary, output_file)
            await _run_file_io(_fsync_directory, output_file.parent)
        finally:
            if output is not None and output is not raw_output:
                await _run_file_io(output.close)
            if raw_output is not None:
                await _run_file_io(raw_output.close)
            await _run_file_io(temporary.unlink, missing_ok=True)

        file_size_mb = output_file.stat().st_size / (1024 * 1024)
        duration = time.time() - start_time

        stats = ExportStats(
            table=table_name,
            exported_rows=exported_rows,
            duration_seconds=duration,
            file_size_mb=file_size_mb,
        )

        return stats

    def _build_upsert_query(self, table_info: TableInfo, columns: list[str]):
        table_name = _validate_identifier(table_info.name, field="table name")
        columns = [
            _validate_identifier(column, field="column name") for column in columns
        ]
        if not columns:
            raise ValueError("import contains no known columns")
        placeholders = ", ".join([":" + col for col in columns])

        return f"""
            INSERT INTO {table_name} ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT DO NOTHING
        """

    async def import_table(
        self,
        input_file: Path,
        table_name: str | None = None,
        batch_size: int | None = None,
    ):
        start_time = time.time()
        batch_size = _normalize_batch_size(
            self.batch_size if batch_size is None else batch_size
        )

        is_compressed = input_file.suffix.lower() == ".gz"
        file_opener = gzip.open if is_compressed else open
        mode = "rb"

        with file_opener(input_file, mode) as f:
            metadata_line = _read_bounded_record(f, _MAX_IMPORT_METADATA_BYTES)
            if metadata_line is None or metadata_line is _OVERSIZED_RECORD:
                raise ValueError("invalid import metadata")
            metadata = _decode_json_object(metadata_line)

            metadata_table_name = metadata.get("table_name")
            if not isinstance(metadata_table_name, str):
                raise ValueError("invalid import table name")
            actual_table_name = _validate_identifier(
                table_name or metadata_table_name,
                field="table name",
            )

            table_info = await self.get_table_info(actual_table_name)

            total_rows = 0
            processed_rows = 0
            error_rows = 0

            import_column_map = {}
            known_columns = set(table_info.columns)

            # First pass: collect all unique columns from the data
            current_pos = f.tell()
            for line in _iter_bounded_records(f, _MAX_IMPORT_ROW_BYTES):
                if not line:
                    if line is None:
                        total_rows += 1
                    continue

                total_rows += 1
                try:
                    row_data = _decode_json_object(line)
                    for column in row_data:
                        if column in known_columns:
                            import_column_map.setdefault(column, None)

                except ValueError:
                    # The second pass reports malformed rows exactly once.
                    continue

            # Reset file position for actual import
            f.seek(current_pos)

            # Filter columns to only those that exist in the target table
            import_columns = list(import_column_map)

            # Build upsert query
            upsert_query = self._build_upsert_query(table_info, import_columns)

            # Process data in batches with adaptive batch size
            current_batch = []
            row_count = 0
            adaptive_batch_size = batch_size

            for line in _iter_bounded_records(f, _MAX_IMPORT_ROW_BYTES):
                if not line:
                    if line is None:
                        row_count += 1
                        error_rows += 1
                    continue

                row_count += 1
                try:
                    row_data = _decode_json_object(line)

                    # Filter to import columns only
                    filtered_row = {col: row_data.get(col) for col in import_columns}

                    current_batch.append(filtered_row)

                    # Process batch when it reaches the adaptive batch size
                    if len(current_batch) >= adaptive_batch_size:
                        batch_processed = await self._process_batch(
                            upsert_query,
                            current_batch,
                        )
                        processed_rows += batch_processed
                        error_rows += len(current_batch) - batch_processed
                        current_batch = []

                        # Adjust batch sizes according to locking issues
                        if self._lock_retry_count > 3:
                            # Reduce batch sizes if there are too many locking issues
                            adaptive_batch_size = max(1000, adaptive_batch_size // 2)
                            self._lock_retry_count = 0
                        elif (
                            self._lock_retry_count == 0
                            and adaptive_batch_size < batch_size
                        ):
                            # Increase gradually if there are no issues
                            adaptive_batch_size = min(
                                batch_size, int(adaptive_batch_size * 1.5)
                            )

                except ValueError:
                    error_rows += 1
                except Exception:
                    error_rows += 1

            # Process final batch
            if current_batch:
                batch_processed = await self._process_batch(
                    upsert_query,
                    current_batch,
                )
                processed_rows += batch_processed
                error_rows += len(current_batch) - batch_processed

        duration = time.time() - start_time

        stats = ImportStats(
            table=actual_table_name,
            total_rows=total_rows,
            processed_rows=processed_rows,
            error_rows=error_rows,
            duration_seconds=duration,
        )

        return stats

    async def _process_batch_with_retry(
        self, query: str, batch_data: list[dict], max_retries: int = 5
    ):
        had_lock_error = False

        for attempt in range(max_retries + 1):
            try:
                async with self.database.transaction():
                    await self.database.execute_many(query, batch_data)
                    if had_lock_error and self._lock_retry_count > 0:
                        self._lock_retry_count -= 1
                    return len(batch_data)

            except Exception as e:
                error_msg = str(e).lower()

                if "locked" in error_msg:
                    had_lock_error = True
                    self._lock_retry_count += 1

                    if attempt < max_retries:
                        wait_time = min(16, (2**attempt)) + random.uniform(0.1, 0.5)
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        raise
                else:
                    raise

        return 0

    async def _process_batch(self, query: str, batch_data: list[dict]):
        if not batch_data:
            return 0

        try:
            return await self._process_batch_with_retry(query, batch_data)

        except Exception:
            return await self._process_batch_individual(query, batch_data)

    async def _process_batch_individual(self, query: str, batch_data: list[dict]):
        successful_inserts = 0
        for row_data in batch_data:
            try:
                await self.database.execute(query, row_data)
                successful_inserts += 1
            except Exception:
                pass
        return successful_inserts

    async def export_tables(
        self,
        table_names: list[str],
        output_dir: Path,
        compress: bool = True,
        parallel: bool = True,
    ):
        output_dir.mkdir(parents=True, exist_ok=True)

        async def export_single_table(table_name: str):
            suffix = ".json.gz" if compress else ".json"
            output_file = output_dir / f"{table_name}{suffix}"
            return await self.export_table(table_name, output_file, compress)

        if parallel:
            tasks = [export_single_table(table) for table in table_names]
            results = await asyncio.gather(*tasks)
        else:
            results = []
            for table_name in table_names:
                result = await export_single_table(table_name)
                results.append(result)

        return results

    async def import_tables(
        self,
        input_dir: Path,
        table_names: list[str] | None = None,
        parallel: bool = True,
    ):
        export_files = []
        for pattern in ["*.json", "*.json.gz"]:
            export_files.extend(input_dir.glob(pattern))

        if not export_files:
            raise ValueError(f"No export files found in {input_dir}")

        # Filter files if specific tables requested
        if table_names:
            filtered_files = []
            for file_path in export_files:
                table_name = file_path.stem.replace(".json", "")
                if table_name in table_names:
                    filtered_files.append(file_path)
            export_files = filtered_files

        if parallel and IS_SQLITE:
            results = []
            for file_path in export_files:
                result = await self.import_table(file_path)
                results.append(result)
        elif parallel:
            tasks = [self.import_table(file_path) for file_path in export_files]
            results = await asyncio.gather(*tasks)
        else:
            results = []
            for file_path in export_files:
                result = await self.import_table(file_path)
                results.append(result)

        return results
