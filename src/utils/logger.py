"""
logger.py — Centralised logging setup using loguru.

WHY loguru over stdlib logging:
- One import, zero boilerplate handlers
- Automatic file rotation + retention
- Coloured console output out of the box
- Structured log records (easy to parse later)

USAGE anywhere in the project:
    from src.utils.logger import get_logger
    logger = get_logger(__name__)
    logger.info("Something happened")
"""

import sys
from pathlib import Path
from loguru import logger as _loguru_logger


# We keep a module-level flag so we only configure loguru once,
# even if get_logger() is called from multiple modules.
_configured = False


def setup_logger(
    log_dir: str = "logs",
    log_file: str = "pipeline.log",
    level: str = "INFO",
    rotation: str = "10 MB",
    retention: str = "7 days",
) -> None:
    """
    Configure loguru sinks (console + rotating file).
    Called once at application startup from main.py.

    Args:
        log_dir:   Directory to write log files into.
        log_file:  Base log filename.
        level:     Minimum log level to capture.
        rotation:  Rotate the file when it reaches this size.
        retention: Delete rotated files older than this.
    """
    global _configured
    if _configured:
        return  # Idempotent — safe to call multiple times

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Remove loguru's default stderr sink so we control format.
    _loguru_logger.remove()

    # --- Console sink ---
    # Coloured, human-readable, shows level + module name.
    _loguru_logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # --- File sink ---
    # Auto-rotates at `rotation` size, deletes old files after `retention`.
    # This prevents your 40GB SSD from filling up with log files.
    _loguru_logger.add(
        log_path / log_file,
        level=level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | "
            "{level: <8} | "
            "{name}:{line} — {message}"
        ),
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
    )

    _configured = True


def get_logger(name: str):
    """
    Return a loguru logger bound to the calling module's name.
    The name appears in log output so you always know which
    module produced a log line.

    Args:
        name: Typically pass __name__ from the calling module.

    Returns:
        A loguru logger instance with the module name bound.
    """
    return _loguru_logger.bind(name=name)