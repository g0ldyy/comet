import sys
import logging
from collections import deque
from typing import List, Dict, Any

from loguru import logger

logging.getLogger("demagnetize").setLevel(
    logging.CRITICAL
)  # disable demagnetize logging


LOG_BUFFER_MAX = 500
log_buffer: deque = deque(maxlen=LOG_BUFFER_MAX)


def get_recent_logs(limit: int = 200) -> List[Dict[str, Any]]:
    limit = min(limit, LOG_BUFFER_MAX)
    # Return newest first
    return list(reversed(list(log_buffer)[-limit:]))


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

    def buffer_sink(message):
        try:
            record = message.record
            log_buffer.append(
                {
                    "time": record["time"].isoformat(),
                    "level": record["level"].name,
                    "icon": record["level"].icon,
                    "module": record["module"],
                    "function": record["function"],
                    "message": record["message"],
                }
            )
        except Exception:
            pass

    logger.configure(
        handlers=[
            {
                "sink": sys.stderr,
                "level": level,
                "format": log_format,
                "backtrace": False,
                "diagnose": False,
                "enqueue": True,
            },
            {
                "sink": buffer_sink,
                "level": level,
                "enqueue": True,
            },
        ]
    )


setupLogger("DEBUG")
