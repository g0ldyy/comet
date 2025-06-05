import sys
import logging

from loguru import logger

logging.getLogger("demagnetize").setLevel(
    logging.CRITICAL
)  # disable demagnetize logging


def setupLogger(level: str):
    logger.level("COMET", no=50, icon="🌠", color="<fg #7871d6>")
    logger.level("API", no=45, icon="👾", color="<fg #006989>")
    logger.level("SCRAPER", no=40, icon="👻", color="<fg #d6bb71>")
    logger.level("STREAM", no=35, icon="🎬", color="<fg #d171d6>")
    logger.level("LOCK", no=30, icon="🔒", color="<fg #71d6d6>")

    logger.level("INFO", icon="📰", color="<fg #FC5F39>")
    logger.level("DEBUG", icon="🕸️", color="<fg #DC5F00>")
    logger.level("WARNING", icon="⚠️", color="<fg #DC5F00>")

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
