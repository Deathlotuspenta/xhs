"""日志工具模块"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

import config


def setup_logger() -> None:
    """初始化 loguru 日志，同时输出到控制台和文件"""
    logger.remove()

    # 控制台：彩色输出
    logger.add(
        sys.stderr,
        level=config.LOG_LEVEL,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        colorize=True,
    )

    # 文件：滚动日志
    log_path = Path(config.LOG_FILE)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_path),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {name}:{line} | {message}",
        rotation=config.get("logging.rotation", "10 MB"),
        retention=config.get("logging.retention", "30 days"),
        encoding="utf-8",
    )
