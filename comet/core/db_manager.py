import asyncio
import gzip
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import orjson
from databases import Database

from comet.core.database import IS_SQLITE, ON_CONFLICT_DO_NOTHING, OR_IGNORE
from comet.core.logger import logger
from comet.core.models import settings


@dataclass
class TableInfo:
    name: str
    columns: List[str]
    primary_key: List[str]
    unique_constraints: List[Dict[str, Any]]
    row_count: int = 0


@dataclass
class ImportStats:
    table: str
    total_rows: int
    inserted_rows: int
    skipped_rows: int
    error_rows: int
    duration_seconds: float
    conflicts_resolved: int = 0


@dataclass
class ExportStats:
    table: str
    exported_rows: int
    duration_seconds: float
    file_size_mb: float


class DatabaseManager:
    def __init__(self, database: Database):
        self.database = database
        self.batch_size = settings.DATABASE_BATCH_SIZE
        self._lock_retry_count = 0

    async def get_table_info(self, table_name: str):
        if IS_SQLITE:
            # Get column information
            columns_result = await self.database.fetch_all(
                f"PRAGMA table_info({table_name})"
            )
            columns = [row["name"] for row in columns_result]
            primary_key = [row["name"] for row in columns_result if row["pk"]]

            # Get unique indexes/constraints
            indexes_result = await self.database.fetch_all(
                f"PRAGMA index_list({table_name})"
            )
            unique_constraints = []

            for index in indexes_result:
                if index["unique"]:
                    index_info = await self.database.fetch_all(
                        f"PRAGMA index_info({index['name']})"
                    )
                    constraint_columns = [col["name"] for col in index_info]

                    # Try to get partial index condition
                    try:
                        sql_result = await self.database.fetch_one(
                            "SELECT sql FROM sqlite_master WHERE type='index' AND name=:name",
                            {"name": index["name"]},
                        )
                        condition = None
                        if (
                            sql_result
                            and sql_result["sql"]
                            and "WHERE" in sql_result["sql"]
                        ):
                            condition = sql_result["sql"].split("WHERE", 1)[1].strip()

                        unique_constraints.append(
                            {
                                "name": index["name"],
                                "columns": constraint_columns,
                                "condition": condition,
                            }
                        )
                    except Exception:
                        unique_constraints.append(
                            {
                                "name": index["name"],
                                "columns": constraint_columns,
                                "condition": None,
                            }
                        )

        else:  # PostgreSQL
            # Get column information
            columns_result = await self.database.fetch_all(
                """
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = :table_name 
                ORDER BY ordinal_position
            """,
                {"table_name": table_name},
            )
            columns = [row["column_name"] for row in columns_result]

            # Get primary key
            pk_result = await self.database.fetch_all(
                """
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu 
                  ON tc.constraint_name = kcu.constraint_name
                WHERE tc.table_name = :table_name 
                  AND tc.constraint_type = 'PRIMARY KEY'
                ORDER BY kcu.ordinal_position
            """,
                {"table_name": table_name},
            )
            primary_key = [row["column_name"] for row in pk_result]

            # Get unique constraints and indexes
            unique_result = await self.database.fetch_all(
                """
                SELECT 
                    c.conname as constraint_name,
                    array_agg(a.attname ORDER BY k.ordinality) as columns,
                    pg_get_expr(c.conbin, c.conrelid) as condition
                FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE t.relname = :table_name 
                  AND c.contype IN ('u', 'p')
                  AND c.conname != :primary_key_name
                GROUP BY c.conname, c.conbin, c.conrelid
                
                UNION ALL
                
                SELECT 
                    idx.indexname as constraint_name,
                    array_agg(a.attname ORDER BY k.ordinality) as columns,
                    pg_get_expr(i.indpred, i.indrelid) as condition
                FROM pg_indexes idx
                JOIN pg_class t ON t.relname = idx.tablename
                JOIN pg_index i ON i.indrelid = t.oid
                JOIN pg_class ic ON ic.oid = i.indexrelid AND ic.relname = idx.indexname
                JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE idx.tablename = :table_name 
                  AND i.indisunique = true
                  AND NOT i.indisprimary
                GROUP BY idx.indexname, i.indpred, i.indrelid
            """,
                {"table_name": table_name, "primary_key_name": f"{table_name}_pkey"},
            )

            unique_constraints = []
            for row in unique_result:
                unique_constraints.append(
                    {
                        "name": row["constraint_name"],
                        "columns": row["columns"],
                        "condition": row["condition"],
                    }
                )

        # Get row count
        count_result = await self.database.fetch_val(
            f"SELECT COUNT(*) FROM {table_name}"
        )

        return TableInfo(
            name=table_name,
            columns=columns,
            primary_key=primary_key,
            unique_constraints=unique_constraints,
            row_count=count_result or 0,
        )

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
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)

        return [row["name"] for row in result]

    async def export_table(
        self,
        table_name: str,
        output_file: Path,
        compress: bool = True,
        batch_size: Optional[int] = None,
    ):
        start_time = time.time()
        batch_size = batch_size or self.batch_size

        logger.log(
            "DB_EXPORT", f"Starting export of table '{table_name}' to {output_file}"
        )

        table_info = await self.get_table_info(table_name)

        exported_rows = 0

        async with aiofiles.open(output_file, "wb" if compress else "w") as f:
            metadata = {
                "table_name": table_name,
                "export_timestamp": datetime.now(timezone.utc).isoformat(),
            }

            if compress:
                # For gzip, we need to handle it differently
                with gzip.open(output_file, "wt", encoding="utf-8") as gf:
                    gf.write(orjson.dumps(metadata).decode("utf-8") + "\n")

                    # Export data in batches
                    offset = 0
                    while True:
                        if IS_SQLITE:
                            query = f"SELECT * FROM {table_name} LIMIT {batch_size} OFFSET {offset}"
                        else:
                            if table_info.primary_key:
                                query = f"SELECT * FROM {table_name} ORDER BY {', '.join(table_info.primary_key)} LIMIT {batch_size} OFFSET {offset}"
                            else:
                                # No primary key, use simple pagination without ORDER BY
                                query = f"SELECT * FROM {table_name} LIMIT {batch_size} OFFSET {offset}"

                        rows = await self.database.fetch_all(query)
                        if not rows:
                            break

                        for row in rows:
                            row_dict = dict(row)

                            gf.write(orjson.dumps(row_dict).decode("utf-8") + "\n")
                            exported_rows += 1

                        offset += batch_size
            else:
                # Non-compressed version
                await f.write(orjson.dumps(metadata).decode("utf-8") + "\n")

                offset = 0
                while True:
                    if IS_SQLITE:
                        query = f"SELECT * FROM {table_name} LIMIT {batch_size} OFFSET {offset}"
                    else:
                        if table_info.primary_key:
                            query = f"SELECT * FROM {table_name} ORDER BY {', '.join(table_info.primary_key)} LIMIT {batch_size} OFFSET {offset}"
                        else:
                            # No primary key, use simple pagination without ORDER BY
                            query = f"SELECT * FROM {table_name} LIMIT {batch_size} OFFSET {offset}"

                    rows = await self.database.fetch_all(query)
                    if not rows:
                        break

                    for row in rows:
                        row_dict = dict(row)

                        await f.write(orjson.dumps(row_dict).decode("utf-8") + "\n")
                        exported_rows += 1

                    offset += batch_size

        file_size_mb = output_file.stat().st_size / (1024 * 1024)
        duration = time.time() - start_time

        stats = ExportStats(
            table=table_name,
            exported_rows=exported_rows,
            duration_seconds=duration,
            file_size_mb=file_size_mb,
        )

        return stats

    def _build_upsert_query(self, table_info: TableInfo, columns: List[str]):
        table_name = table_info.name
        placeholders = ", ".join([":" + col for col in columns])

        return f"""
            INSERT {OR_IGNORE} INTO {table_name} ({", ".join(columns)})
            VALUES ({placeholders})
            {ON_CONFLICT_DO_NOTHING}
        """

    async def import_table(
        self,
        input_file: Path,
        table_name: Optional[str] = None,
        batch_size: Optional[int] = None,
    ):
        start_time = time.time()
        batch_size = batch_size or self.batch_size

        logger.log("DB_IMPORT", f"Starting import from {input_file}")

        is_compressed = input_file.suffix.lower() == ".gz"
        file_opener = gzip.open if is_compressed else open
        mode = "rt" if is_compressed else "r"

        with file_opener(input_file, mode, encoding="utf-8") as f:
            metadata_line = f.readline().strip()
            metadata = orjson.loads(metadata_line)

            actual_table_name = table_name or metadata["table_name"]

            table_info = await self.get_table_info(actual_table_name)

            total_rows = 0
            inserted_rows = 0
            skipped_rows = 0
            error_rows = 0
            conflicts_resolved = 0

            all_columns = set()

            # First pass: collect all unique columns from the data
            current_pos = f.tell()
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    row_data = orjson.loads(line)
                    all_columns.update(row_data.keys())
                    total_rows += 1

                except orjson.JSONDecodeError as e:
                    error_rows += 1
                    logger.log(
                        "DB_IMPORT", f"JSON decode error on row {total_rows + 1}: {e}"
                    )

            # Reset file position for actual import
            f.seek(current_pos)

            # Filter columns to only those that exist in the target table
            import_columns = [col for col in all_columns if col in table_info.columns]
            missing_columns = all_columns - set(table_info.columns)

            if missing_columns:
                logger.log("DB_IMPORT", f"Skipping missing columns: {missing_columns}")

            logger.log(
                "DB_IMPORT",
                f"Importing {len(import_columns)} columns: {import_columns}",
            )

            # Build upsert query
            upsert_query = self._build_upsert_query(table_info, import_columns)
            logger.log(
                "DB_IMPORT", f"Using query strategy: {upsert_query.split()[0:6]}"
            )  # Log first part

            # Process data in batches with adaptive batch size
            current_batch = []
            row_count = 0
            adaptive_batch_size = batch_size

            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    row_data = orjson.loads(line)

                    # Filter to import columns only
                    filtered_row = {col: row_data.get(col) for col in import_columns}

                    current_batch.append(filtered_row)
                    row_count += 1

                    # Process batch when it reaches the adaptive batch size
                    if len(current_batch) >= adaptive_batch_size:
                        batch_inserted = await self._process_batch(
                            upsert_query, current_batch, table_info.name
                        )
                        inserted_rows += batch_inserted
                        conflicts_resolved += len(current_batch) - batch_inserted
                        current_batch = []

                        # Adjust batch sizes according to locking issues
                        if self._lock_retry_count > 3:
                            # Reduce batch sizes if there are too many locking issues
                            adaptive_batch_size = max(1000, adaptive_batch_size // 2)
                            logger.log(
                                "DB_IMPORT",
                                f"Reducing batch size to {adaptive_batch_size} due to lock contention",
                            )
                            self._lock_retry_count = 0
                        elif (
                            self._lock_retry_count == 0
                            and adaptive_batch_size < batch_size
                        ):
                            # Increase gradually if there are no issues
                            adaptive_batch_size = min(
                                batch_size, int(adaptive_batch_size * 1.5)
                            )

                except orjson.JSONDecodeError as e:
                    error_rows += 1
                    logger.log(
                        "DB_IMPORT", f"JSON decode error on row {row_count + 1}: {e}"
                    )
                except Exception as e:
                    error_rows += 1
                    logger.log(
                        "DB_IMPORT", f"Error processing row {row_count + 1}: {e}"
                    )

            # Process final batch
            if current_batch:
                batch_inserted = await self._process_batch(
                    upsert_query, current_batch, table_info.name
                )
                inserted_rows += batch_inserted
                conflicts_resolved += len(current_batch) - batch_inserted

                if adaptive_batch_size != batch_size:
                    logger.log(
                        "DB_IMPORT",
                        f"Final batch size was {adaptive_batch_size} (started with {batch_size}) due to lock contention",
                    )

        duration = time.time() - start_time

        stats = ImportStats(
            table=actual_table_name,
            total_rows=total_rows,
            inserted_rows=inserted_rows,
            skipped_rows=skipped_rows,
            error_rows=error_rows,
            duration_seconds=duration,
            conflicts_resolved=conflicts_resolved,
        )

        return stats

    async def _process_batch_with_retry(
        self, query: str, batch_data: List[Dict], max_retries: int = 5
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
                        logger.log(
                            "DB_IMPORT",
                            f"Database locked, retry {attempt + 1}/{max_retries} in {wait_time:.1f}s (lock_count: {self._lock_retry_count})",
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.log(
                            "DB_IMPORT",
                            f"Database still locked after {max_retries} retries, falling back to individual inserts",
                        )
                        raise
                else:
                    logger.log("DB_IMPORT", f"Non-recoverable batch error: {e}")
                    raise

        return 0

    async def _process_batch(self, query: str, batch_data: List[Dict], table_name: str):
        if not batch_data:
            return 0

        try:
            return await self._process_batch_with_retry(query, batch_data)

        except Exception as e:
            logger.log("DB_IMPORT", f"Batch processing failed definitively: {e}")
            return await self._process_batch_individual(query, batch_data)

    async def _process_batch_individual(self, query: str, batch_data: List[Dict]):
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
        table_names: List[str],
        output_dir: Path,
        compress: bool = True,
        parallel: bool = True,
    ):
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.log("DB_EXPORT", f"Exporting {len(table_names)} tables to {output_dir}")

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

        sum(r.exported_rows for r in results)
        sum(r.file_size_mb for r in results)
        max(r.duration_seconds for r in results) if results else 0

        return results

    async def import_tables(
        self,
        input_dir: Path,
        table_names: Optional[List[str]] = None,
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

        logger.log(
            "DB_IMPORT", f"Importing {len(export_files)} tables from {input_dir}"
        )

        if parallel and IS_SQLITE:
            logger.log(
                "DB_IMPORT",
                "SQLite detected, forcing sequential processing to prevent lock contention",
            )

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

        sum(r.inserted_rows for r in results)
        sum(r.conflicts_resolved for r in results)
        sum(r.error_rows for r in results)

        return results
