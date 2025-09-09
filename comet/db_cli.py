import asyncio
import argparse
import sys

from pathlib import Path
from typing import List, Optional

from comet.utils.db_manager import DatabaseManager
from comet.utils.database import setup_database
from comet.utils.models import database
from comet.utils.logger import logger


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


async def main():
    parser = argparse.ArgumentParser(
        description="Comet Database Import/Export Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all tables
  python -m comet.db_cli list-tables
  
  # Show table info
  python -m comet.db_cli info --table torrents
  
  # Export specific tables
  python -m comet.db_cli export --tables torrents,metadata_cache --output ./backup/
  
  # Export all tables with compression
  python -m comet.db_cli export --output ./backup/ --compress
  
  # Import specific tables
  python -m comet.db_cli import --input ./backup/ --tables torrents
  
  # Import all tables (parallel disabled for safety)
  python -m comet.db_cli import --input ./backup/ --no-parallel
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
