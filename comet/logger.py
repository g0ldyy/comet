import os
import sys
from datetime import datetime
from pathlib import Path

from loguru import logger


def setup_logger(level):
    """Setup the logger"""
    logs_dir_path = "logs"
    os.makedirs(logs_dir_path, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M")
    log_filename = Path(logs_dir_path) / f"comet-{timestamp}.log"

    logger.level("PROGRAM", no=36, color="<blue>", icon="☄️ ")
    logger.level("DEBRID", no=38, color="<fg #DC5F00>", icon="👻")
    logger.level("SCRAPER", no=40, color="<magenta>", icon="🌠")

    # set the default info and debug level icons
    logger.level("INFO", icon="📰", color="<fg #DC5F00>")
    logger.level("DEBUG", icon="🕸️", color="<fg #DC5F00>")
    logger.level("WARNING", icon="⚠️ ", color="<fg #DC5F00>")

    # Log format to match the old log format, but with color
    log_format = (
        "<white>{time:YYYY-MM-DD}</white> <magenta>{time:HH:mm:ss}</magenta> | "
        "<level>{level.icon}</level> <level>{level: <9}</level> | "
        "<cyan>{module}</cyan>.<cyan>{function}</cyan> - <level>{message}</level>"
    )

    logger.configure(handlers=[
        {
            "sink": sys.stderr,
            "level": "DEBUG",
            "format": log_format,
            "backtrace": False,
            "diagnose": False,
            "enqueue": True,
        },
        {
            "sink": log_filename, 
            "level": level, 
            "format": log_format, 
            "rotation": "1 hour", 
            "retention": "1 hour", 
            "compression": None, 
            "backtrace": False, 
            "diagnose": True,
            "enqueue": True,
        },
    ])


def clean_old_logs():
    """Remove old log files based on retention settings."""
    try:
        logs_dir_path = Path("logs")
        for log_file in logs_dir_path.glob("comet-*.log"):
            # remove files older than 1 hour so that logs dont get messy
            if (datetime.now() - datetime.fromtimestamp(log_file.stat().st_mtime)).total_seconds() / 3600 > 1:
                log_file.unlink()
                logger.log("PROGRAM", f"Old log file {log_file.name} removed.")
    except Exception as e:
        logger.error(f"Failed to clean old logs: {e}")

setup_logger("DEBUG")
clean_old_logs()
