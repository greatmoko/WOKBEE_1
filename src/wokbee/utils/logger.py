"""日志工具。"""

import logging
import sys
from pathlib import Path

from wokbee import __app_name__


def setup_logger(level: int = logging.INFO) -> logging.Logger:
    """初始化应用日志。"""
    logger = logging.getLogger(__app_name__)
    logger.setLevel(level)

    if not logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console.setFormatter(fmt)
        logger.addHandler(console)

        log_dir = Path.home() / ".wokbee" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "wokbee.log", encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    return logger
