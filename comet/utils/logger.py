import sys
import logging

from loguru import logger
from comet.utils.log_levels import CUSTOM_LOG_LEVELS, STANDARD_LOG_LEVELS

logging.getLogger("demagnetize").setLevel(
    logging.CRITICAL
)  # disable demagnetize logging


def setupLogger(level: str):
    # Configure custom log levels
    for level_name, level_config in CUSTOM_LOG_LEVELS.items():
        logger.level(
            level_name,
            no=level_config["no"],
            icon=level_config["icon"],
            color=level_config["loguru_color"],
        )

    # Configure standard log levels (override defaults)
    for level_name, level_config in STANDARD_LOG_LEVELS.items():
        logger.level(
            level_name, icon=level_config["icon"], color=level_config["loguru_color"]
        )

    log_format = (
        "<white>{time:YYYY-MM-DD}</white> <magenta>{time:HH:mm:ss}</magenta> | "
        "<level>{level.icon}</level> <level>{level}</level> | "
        "<cyan>{module}</cyan>.<cyan>{function}</cyan> - <level>{message}</level>"
    )

    logger.configure(
        handlers=[
            {
                "sink": sys.stderr,
                "level": level,
                "format": log_format,
                "backtrace": False,
                "diagnose": False,
                "enqueue": True,
            }
        ]
    )


setupLogger("DEBUG")
