import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

import aiohttp

from comet.core.database import setup_database
from comet.core.db_manager import DatabaseManager
from comet.core.logger import logger
from comet.core.models import database
from comet.core.schema_specs import DEBRID_ACCOUNT_TRACKER_PREDICATE
from comet.metadata.manager import MetadataScraper
from comet.metadata.tmdb import TMDBApi
from comet.services.anime import anime_mapper
from comet.services.debrid_account_cache_cleanup import (
    DEFAULT_CLEANUP_BATCH_SIZE,
    repair_debrid_account_cache_for_media,
)


async def list_tables_command(db_manager: DatabaseManager):
    tables = await db_manager.list_tables()

    print(f"\nFound {len(tables)} tables:")
    print("-" * 40)

    for table in tables:
        table_info = await db_manager.get_table_info(table)
        print(f"{table:<30} {table_info.row_count:>10,} rows")

    print("-" * 40)


async def table_info_command(db_manager: DatabaseManager, table_name: str):
    try:
        table_info = await db_manager.get_table_info(table_name)
    except Exception as e:
        print(f"Error getting table info: {e}")
        return

    print(f"\nTable: {table_info.name}")
    print("=" * 50)
    print(f"Rows: {table_info.row_count:,}")
    print(f"Columns ({len(table_info.columns)}): {', '.join(table_info.columns)}")

    if table_info.primary_key:
        print(f"Primary Key: {', '.join(table_info.primary_key)}")

    if table_info.unique_constraints:
        print(f"\nUnique Constraints ({len(table_info.unique_constraints)}):")
        for constraint in table_info.unique_constraints:
            condition_str = (
                f" WHERE {constraint['condition']}" if constraint["condition"] else ""
            )
            print(
                f"  - {constraint['name']}: ({', '.join(constraint['columns'])}){condition_str}"
            )


async def export_command(
    db_manager: DatabaseManager,
    table_names: List[str],
    output_path: Path,
    compress: bool,
    parallel: bool,
):
    all_tables = await db_manager.list_tables()
    invalid_tables = [t for t in table_names if t not in all_tables]

    if invalid_tables:
        print(f"Error: These tables don't exist: {', '.join(invalid_tables)}")
        print(f"Available tables: {', '.join(all_tables)}")
        return

    print(f"Exporting {len(table_names)} tables to {output_path}")
    print(f"Compression: {'enabled' if compress else 'disabled'}")
    print(f"Parallel: {'enabled' if parallel else 'disabled'}")
    print()

    results = await db_manager.export_tables(
        table_names, output_path, compress=compress, parallel=parallel
    )

    print("\nExport Results:")
    print("=" * 80)
    total_rows = 0
    total_size = 0.0

    for stats in results:
        print(
            f"{stats.table:<25} {stats.exported_rows:>10,} rows  "
            f"{stats.file_size_mb:>8.2f}MB  {stats.duration_seconds:>8.2f}s"
        )
        total_rows += stats.exported_rows
        total_size += stats.file_size_mb

    print("-" * 80)
    print(f"{'TOTAL':<25} {total_rows:>10,} rows  {total_size:>8.2f}MB")
    print()


async def import_command(
    db_manager: DatabaseManager,
    input_path: Path,
    table_names: Optional[List[str]],
    parallel: bool,
):
    if not input_path.exists():
        print(f"Error: Input path {input_path} does not exist")
        return

    if not input_path.is_dir():
        print(f"Error: Input path {input_path} is not a directory")
        return

    print(f"Importing from {input_path}")
    if table_names:
        print(f"Specific tables: {', '.join(table_names)}")
    else:
        print("All available tables")
    print(f"Parallel: {'enabled' if parallel else 'disabled'}")
    print()

    try:
        results = await db_manager.import_tables(
            input_path, table_names=table_names, parallel=parallel
        )
    except Exception as e:
        print(f"Import failed: {e}")
        return

    print("\nImport Results:")
    print("=" * 100)
    print(
        f"{'Table':<20} {'Total':<10} {'Inserted':<10} {'Conflicts':<10} {'Errors':<8} {'Duration':<10}"
    )
    print("-" * 100)

    total_inserted = 0
    total_conflicts = 0
    total_errors = 0

    for stats in results:
        print(
            f"{stats.table:<20} {stats.total_rows:<10,} {stats.inserted_rows:<10,} "
            f"{stats.conflicts_resolved:<10,} {stats.error_rows:<8,} {stats.duration_seconds:<10.2f}s"
        )
        total_inserted += stats.inserted_rows
        total_conflicts += stats.conflicts_resolved
        total_errors += stats.error_rows

    print("-" * 100)
    print(
        f"{'TOTAL':<20} {'':<10} {total_inserted:<10,} {total_conflicts:<10,} {total_errors:<8,}"
    )
    print()

    if total_conflicts > 0:
        print(
            f"ℹ️  {total_conflicts:,} rows were skipped due to uniqueness constraints (expected behavior)"
        )
    if total_errors > 0:
        print(f"⚠️  {total_errors:,} rows had errors and were skipped")


def parse_table_list(table_str: str):
    if not table_str:
        return []
    return [table.strip() for table in table_str.split(",") if table.strip()]


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _classify_cleanup_media_id(media_id: str) -> tuple[str, str | None]:
    if media_id.startswith("kitsu:"):
        provider_id = media_id.partition(":")[2]
        return provider_id, "kitsu" if provider_id.isdigit() else None
    if media_id.startswith("tt"):
        return media_id, "imdb"
    if media_id.isdigit():
        return media_id, "kitsu"
    return media_id, None


async def _get_debrid_account_cleanup_candidates(
    media_ids: list[str] | None,
    min_rows: int,
    limit: int | None,
    provider: str | None,
):
    if media_ids:
        candidates = []
        seen = set()
        for media_id in media_ids:
            normalized_id, detected_provider = _classify_cleanup_media_id(media_id)
            if normalized_id not in seen and (
                provider is None or provider == detected_provider
            ):
                candidates.append((normalized_id, None, detected_provider))
                seen.add(normalized_id)
        return candidates

    limit_sql = "LIMIT :limit" if limit is not None else ""
    params = {"min_rows": min_rows}
    if limit is not None:
        params["limit"] = limit

    if provider == "kitsu":
        from_sql = """
            torrents AS t
            INNER JOIN anime_ids AS ai
                ON ai.provider = 'kitsu'
               AND ai.provider_id = t.media_id
        """
        provider_sql = ""
    else:
        from_sql = "torrents AS t"
        provider_sql = (
            "AND substr(t.media_id, 1, 2) = 'tt'" if provider == "imdb" else ""
        )

    rows = await database.fetch_all(
        f"""
        SELECT t.media_id, COUNT(*) AS row_count
        FROM {from_sql}
        WHERE {DEBRID_ACCOUNT_TRACKER_PREDICATE}
        {provider_sql}
        GROUP BY t.media_id
        HAVING COUNT(*) >= :min_rows
        ORDER BY row_count DESC, t.media_id
        {limit_sql}
        """,
        params,
        force_primary=True,
    )
    candidates = []
    for row in rows:
        media_id, detected_provider = _classify_cleanup_media_id(row["media_id"])
        candidates.append((media_id, int(row["row_count"]), detected_provider))
    return candidates


async def cleanup_debrid_account_command(
    *,
    media_ids: list[str] | None,
    media_type: str | None,
    provider: str | None,
    min_rows: int,
    limit: int | None,
    batch_size: int,
    apply: bool,
):
    candidates = await _get_debrid_account_cleanup_candidates(
        media_ids, min_rows, limit, provider
    )
    action = "APPLY" if apply else "DRY RUN"
    print(f"\nDebridAccount cache cleanup ({action}): {len(candidates):,} media items")
    if not apply:
        print(
            "No rows will be deleted. Re-run with --apply after reviewing the report."
        )

    totals = {
        "scanned": 0,
        "matched": 0,
        "invalid": 0,
        "invalid_rows": 0,
        "unverifiable": 0,
        "skipped": 0,
    }
    async with aiohttp.ClientSession() as session:
        metadata_scraper = MetadataScraper(session)
        tmdb = TMDBApi(session)
        needs_kitsu = any(
            candidate_provider == "kitsu" for _, _, candidate_provider in candidates
        )
        kitsu_mapping_ready = (
            await anime_mapper.load_cached_mapping() if needs_kitsu else False
        )

        for index, (media_id, known_rows, candidate_provider) in enumerate(
            candidates, 1
        ):
            if candidate_provider is None or (
                candidate_provider == "kitsu" and not kitsu_mapping_ready
            ):
                totals["skipped"] += 1
                print(
                    f"[{index}/{len(candidates)}] {media_id}: "
                    "skipped (unknown provider or unavailable anime mapping)"
                )
                continue

            resolved_type = media_type
            if resolved_type is None:
                if candidate_provider == "imdb":
                    resolved_type = await tmdb.get_media_type_from_imdb(media_id)
                elif candidate_provider == "kitsu" and kitsu_mapping_ready:
                    resolved_type = await anime_mapper.get_media_type(
                        f"kitsu:{media_id}"
                    )

            if resolved_type is None:
                totals["skipped"] += 1
                print(
                    f"[{index}/{len(candidates)}] {media_id}: skipped (unknown media type)"
                )
                continue

            source_media_id = (
                f"kitsu:{media_id}" if candidate_provider == "kitsu" else media_id
            )
            metadata, aliases = await metadata_scraper.fetch_metadata_and_aliases(
                resolved_type, source_media_id, id=media_id
            )
            if metadata is None:
                totals["skipped"] += 1
                print(
                    f"[{index}/{len(candidates)}] {media_id}: skipped (metadata unavailable)"
                )
                continue

            stats = await repair_debrid_account_cache_for_media(
                media_id=media_id,
                media_type=resolved_type,
                title=metadata["title"],
                year=metadata["year"],
                year_end=metadata["year_end"],
                aliases=aliases,
                apply=apply,
                batch_size=batch_size,
            )
            totals["scanned"] += stats.scanned_hashes
            totals["matched"] += stats.matched_hashes
            totals["invalid"] += stats.invalid_hashes
            totals["invalid_rows"] += stats.invalid_rows
            totals["unverifiable"] += stats.unverifiable_hashes
            source_count = f", source rows={known_rows:,}" if known_rows else ""
            verb = "removed" if apply else "would remove"
            print(
                f"[{index}/{len(candidates)}] {media_id} ({metadata['title']}): "
                f"hashes={stats.scanned_hashes:,}, valid={stats.matched_hashes:,}, "
                f"{verb}={stats.invalid_hashes:,} hashes/{stats.invalid_rows:,} rows, "
                f"unverifiable={stats.unverifiable_hashes:,}{source_count}"
            )

    verb = "Removed" if apply else "Would remove"
    print(
        f"\nScanned {totals['scanned']:,} hashes; retained {totals['matched']:,}; "
        f"{verb} {totals['invalid']:,} hashes / {totals['invalid_rows']:,} rows; "
        f"retained {totals['unverifiable']:,} unverifiable hashes; "
        f"skipped {totals['skipped']:,} media items."
    )


async def main():
    parser = argparse.ArgumentParser(
        description="Comet database maintenance tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all tables
  python -m comet.db_cli list-tables
  
  # Show table info
  python -m comet.db_cli info --table torrents
  
  # Export specific tables
  python -m comet.db_cli export --tables torrents,media_metadata_cache --output ./backup/
  
  # Export all tables with compression
  python -m comet.db_cli export --output ./backup/
  
  # Import specific tables
  python -m comet.db_cli import --input ./backup/ --tables torrents
  
  # Import all tables (parallel disabled for safety)
  python -m comet.db_cli import --input ./backup/ --no-parallel

  # Audit a polluted DebridAccount media cache without deleting rows
  python -m comet.db_cli cleanup-debrid-account --media-id tt29552248 --media-type movie

  # Apply the reviewed cleanup
  python -m comet.db_cli cleanup-debrid-account --media-id tt29552248 --media-type movie --apply
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    subparsers.add_parser("list-tables", help="List all database tables")

    info_parser = subparsers.add_parser("info", help="Show table information")
    info_parser.add_argument("--table", required=True, help="Table name to inspect")

    export_parser = subparsers.add_parser("export", help="Export database tables")
    export_parser.add_argument(
        "--tables", help="Comma-separated list of tables (default: all)"
    )
    export_parser.add_argument(
        "--output", required=True, type=Path, help="Output directory"
    )
    export_parser.add_argument(
        "--no-compress", action="store_true", help="Disable compression"
    )
    export_parser.add_argument(
        "--no-parallel", action="store_true", help="Disable parallel processing"
    )

    import_parser = subparsers.add_parser("import", help="Import database tables")
    import_parser.add_argument(
        "--input", required=True, type=Path, help="Input directory"
    )
    import_parser.add_argument(
        "--tables", help="Comma-separated list of tables (default: all found)"
    )
    import_parser.add_argument(
        "--no-parallel", action="store_true", help="Disable parallel processing"
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup-debrid-account",
        help="Revalidate and optionally remove invalid DebridAccount torrent associations",
    )
    cleanup_parser.add_argument(
        "--media-id",
        action="append",
        dest="media_ids",
        help="Only clean this media ID (repeatable; default: all candidates)",
    )
    cleanup_parser.add_argument(
        "--media-type",
        choices=("movie", "series"),
        help="Force the media type instead of auto-detection",
    )
    cleanup_parser.add_argument(
        "--provider",
        choices=("imdb", "kitsu"),
        help="Only scan media IDs from this provider",
    )
    cleanup_parser.add_argument(
        "--min-rows",
        type=positive_int,
        default=1,
        help="Only scan media with at least this many DebridAccount rows (default: 1)",
    )
    cleanup_parser.add_argument(
        "--limit", type=positive_int, help="Maximum number of media items to scan"
    )
    cleanup_parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=DEFAULT_CLEANUP_BATCH_SIZE,
        help=(
            "Distinct hashes validated per DB batch "
            f"(default: {DEFAULT_CLEANUP_BATCH_SIZE})"
        ),
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete invalid associations (default is a non-deleting dry run)",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        await setup_database()
        db_manager = DatabaseManager(database)

        if args.command == "list-tables":
            await list_tables_command(db_manager)

        elif args.command == "info":
            await table_info_command(db_manager, args.table)

        elif args.command == "export":
            if args.tables:
                table_names = parse_table_list(args.tables)
            else:
                table_names = await db_manager.list_tables()

            await export_command(
                db_manager,
                table_names,
                args.output,
                compress=not args.no_compress,
                parallel=not args.no_parallel,
            )

        elif args.command == "import":
            table_names = parse_table_list(args.tables) if args.tables else None

            await import_command(
                db_manager, args.input, table_names, parallel=not args.no_parallel
            )

        elif args.command == "cleanup-debrid-account":
            await cleanup_debrid_account_command(
                media_ids=args.media_ids,
                media_type=args.media_type,
                provider=args.provider,
                min_rows=args.min_rows,
                limit=args.limit,
                batch_size=args.batch_size,
                apply=args.apply,
            )

    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        logger.exception("CLI command failed")
        sys.exit(1)
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
