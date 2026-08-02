import asyncio

from comet.core.operator_settings import prepare_effective_settings_environment
from comet.observability.logging import (
    bootstrap_failure,
    configuration_invalid,
    configure_entrypoint,
)


def main() -> None:
    try:
        prepare_effective_settings_environment()
    except Exception as exc:
        bootstrap_failure(exception=exc, process_role="cli")
        raise SystemExit(78) from None
    configure_entrypoint(process_role="cli")
    try:
        from comet.db_cli import main as database_main
    except Exception as exc:
        configuration_invalid(exception=exc)
        raise SystemExit(78) from None
    asyncio.run(database_main())


if __name__ == "__main__":
    main()
