# -*- coding: utf-8 -*-
"""
结构化日志。

- 控制台：彩色、简洁，级别由 LOG_LEVEL 控制
- 文件：按天切分，保留 30 天，压缩归档
- 风控拦截 / 数据异常等需要单独留痕的场景，用 get_logger(name) 取子 logger

用法：
    from src.common.logger import logger
    logger.info("开始抓取")
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from loguru import logger

from src.common.config import PROJECT_ROOT, load_dotenv_once

load_dotenv_once()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = PROJECT_ROOT / os.getenv("LOG_DIR", "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

# 移除 loguru 默认 sink，避免重复输出
logger.remove()

# 控制台（--windowed 无控制台窗口时 sys.stderr 为 None，跳过挂载避免 loguru 崩溃）
if sys.stderr is not None:
    logger.add(
        sys.stderr,
        level=LOG_LEVEL,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        colorize=True,
    )


def _safe_file_sink(pattern: str, **kwargs) -> None:
    """尝试添加文件 sink；文件被占用/不可写时降级为仅控制台，不让系统崩。

    此前日志文件被别的进程独占（残留服务/杀毒扫描）时 PermissionError
    直接在 import 阶段炸掉整个程序——日志不可用不该是一票否决。
    """
    try:
        logger.add(LOG_DIR / pattern, **kwargs)
    except (PermissionError, OSError) as e:
        logger.warning(f"日志文件 {pattern} 不可用（{e}），降级为仅控制台输出")


# 全量日志文件（按天切分，保留 30 天）
_safe_file_sink(
    "quant_{time:YYYY-MM-DD}.log",
    level="DEBUG",
    rotation="00:00",
    retention="30 days",
    compression="zip",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    enqueue=True,   # 多进程/多线程安全
)

# 错误单独留一份，便于告警直接 grep
_safe_file_sink(
    "error_{time:YYYY-MM-DD}.log",
    level="ERROR",
    rotation="00:00",
    retention="90 days",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    enqueue=True,
)


def get_logger(name: str):
    """取带命名空间的子 logger（日志中会显示 name，便于定位模块）。"""
    return logger.bind(name=name)


__all__ = ["logger", "get_logger", "LOG_DIR"]
