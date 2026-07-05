"""日志配置：loguru 统一入口。

- 控制台彩色输出 + 文件轮转
- 敏感字段（密钥/token）自动脱敏
- 每次运行独立 run_id 贯穿全链路
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from loguru import logger

_SECRETS = re.compile(r"(sk-[A-Za-z0-9]{6,}|token=[^&\s]+|password=[^&\s]+|api_key=[^&\s]+)", re.I)


def _redact(record: dict) -> bool:
    """对日志消息中的密钥片段脱敏。返回 True 让 loguru 记录该条。"""
    msg = record["message"]
    if _SECRETS.search(msg):
        record["message"] = _SECRETS.sub("***", msg)
    return True


def setup_logging(log_dir: str | Path = "logs", run_id: str | None = None, level: str = "INFO") -> None:
    """配置 loguru。run_id 非空时额外绑定到每条日志。"""
    logger.remove()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # 控制台
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> | {message}",
        filter=_redact,
    )
    # 全量文件（轮转 10MB，保留 14 天）
    logger.add(
        log_dir / "radar_{time}.log",
        level="DEBUG",
        rotation="10 MB",
        retention="14 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{function}:{line} | {message}",
        filter=_redact,
    )
    if run_id:
        logger.configure(extra={"run_id": run_id})
        logger.add(
            log_dir / f"run_{run_id}.log",
            level="DEBUG",
            encoding="utf-8",
            format="{time:HH:mm:ss} | {level: <7} | {message}",
            filter=_redact,
        )
    logger.info("logging initialized: dir={} run_id={}", log_dir, run_id)
