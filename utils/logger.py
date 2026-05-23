import sys
from pathlib import Path
from loguru import logger


def setup_logger(log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        colorize=True,
    )
    logger.add(
        f"{log_dir}/{{time:YYYY-MM-DD}}.log",
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
        encoding="utf-8",
    )


setup_logger()
