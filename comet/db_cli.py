import asyncio
from pathlib import Path

from comet.core.operator_settings import prepare_effective_settings_environment
from comet.observability.logging import (
    bootstrap_failure,
    configuration_invalid,
    configure_entrypoint,
    log,
)

if __name__ == "__main__":
    try:
        prepare_effective_settings_environment()
    except Exception:
        bootstrap_failure()
        raise SystemExit(78) from None
configure_entrypoint(process_role="cli")

try:
    from comet.core.database import setup_database
    from comet.core.db_manager import DatabaseManager
    from comet.core.models import database
except Exception as exc:
    configuration_invalid(exception=exc)
    raise SystemExit(78) from None
from comet.utils.safe_cli import SafeArgumentParser


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
    except Exception:
        print("Table information could not be read.")
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
    table_names: list[str],
    output_path: Path,
    compress: bool,
    parallel: bool,
):
    started_at = asyncio.get_running_loop().time()
    all_tables = await db_manager.list_tables()
    invalid_tables = [t for t in table_names if t not in all_tables]

    if invalid_tables:
        print(f"Error: These tables don't exist: {', '.join(invalid_tables)}")
        print(f"Available tables: {', '.join(all_tables)}")
        return

    log.info(
        "database.export.started",
        "Database export started",
        item_count=len(table_names),
    )
    print(f"Exporting {len(table_names)} tables to {output_path}")
    print(f"Compression: {'enabled' if compress else 'disabled'}")
    print(f"Parallel: {'enabled' if parallel else 'disabled'}")
    print()

    try:
        results = await db_manager.export_tables(
            table_names, output_path, compress=compress, parallel=parallel
        )
    except Exception as exc:
        log.terminal(
            "database.export.completed",
            "Database export completed",
            outcome="failed",
            item_count=len(table_names),
            duration_ms=(asyncio.get_running_loop().time() - started_at) * 1000,
            error_code="database_export_failed",
            exc=exc,
        )
        raise

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
    log.terminal(
        "database.export.completed",
        "Database export completed",
        outcome="ok",
        item_count=len(results),
        result_count=total_rows,
        duration_ms=(asyncio.get_running_loop().time() - started_at) * 1000,
    )


async def import_command(
    db_manager: DatabaseManager,
    input_path: Path,
    table_names: list[str] | None,
    parallel: bool,
):
    started_at = asyncio.get_running_loop().time()
    if not input_path.exists():
        print(f"Error: Input path {input_path} does not exist")
        return

    if not input_path.is_dir():
        print(f"Error: Input path {input_path} is not a directory")
        return

    log.info(
        "database.import.started",
        "Database import started",
        item_count=len(table_names) if table_names is not None else 0,
    )
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
    except Exception as exc:
        log.terminal(
            "database.import.completed",
            "Database import completed",
            outcome="failed",
            duration_ms=(asyncio.get_running_loop().time() - started_at) * 1000,
            error_code="database_import_failed",
            exc=exc,
        )
        print("Import failed.")
        return

    print("\nImport Results:")
    print("=" * 90)
    print(
        f"{'Table':<20} {'Total':<10} {'Processed':<12} {'Errors':<8} {'Duration':<10}"
    )
    print("-" * 90)

    total_processed = 0
    total_errors = 0

    for stats in results:
        print(
            f"{stats.table:<20} {stats.total_rows:<10,} "
            f"{stats.processed_rows:<12,} {stats.error_rows:<8,} "
            f"{stats.duration_seconds:<10.2f}s"
        )
        total_processed += stats.processed_rows
        total_errors += stats.error_rows

    print("-" * 90)
    print(f"{'TOTAL':<20} {'':<10} {total_processed:<12,} {total_errors:<8,}")
    print()

    if total_errors > 0:
        print(f"⚠️  {total_errors:,} rows had errors and were skipped")
    log.terminal(
        "database.import.completed",
        "Database import completed",
        outcome="partial" if total_errors else "ok",
        item_count=len(results),
        result_count=total_processed,
        failure_count=total_errors,
        duration_ms=(asyncio.get_running_loop().time() - started_at) * 1000,
    )


def parse_table_list(table_str: str):
    if not table_str:
        return []
    return [table.strip() for table in table_str.split(",") if table.strip()]


async def main() -> int:
    parser = SafeArgumentParser(
        description="Comet database maintenance tool",
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
        return 0

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
        return 1
    except Exception:
        print("Database command failed.")
        return 1
    finally:
        try:
            await database.disconnect()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
