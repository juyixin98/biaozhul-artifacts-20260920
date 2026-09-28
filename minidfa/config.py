"""配置层：集中管理可配置项，支持环境变量覆盖。"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field

LOGGER_NAME = "minidfa"

# 空词支持规则（固定，见 README）：
# 空词 "" 是合法词；在字典序中排最前；每个词典至多出现一次；
# 接受语义体现为初始状态带终结标记。
ALLOW_EMPTY_WORD = True


@dataclass(frozen=True)
class Settings:
    """运行配置。默认 strict 模式：未排序或重复输入一律拒绝。"""

    db_path: str = "./minidfa.db"
    log_level: str = "INFO"
    # 单个自动机允许的最大词数，防止误提交超大语料拖垮服务。
    max_words: int = 1_000_000

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("MINIDFA_DB_PATH", "./minidfa.db"),
            log_level=os.environ.get("MINIDFA_LOG_LEVEL", "INFO"),
            max_words=int(os.environ.get("MINIDFA_MAX_WORDS", "1000000")),
        )


_configured = False


def configure_logging(level: str = "INFO") -> logging.Logger:
    """配置并返回项目 logger。格式包含级别与 logger 名，便于关联运行身份。"""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False
        _configured = True
    logger.setLevel(level.upper())
    return logger


def get_logger() -> logging.Logger:
    return configure_logging(os.environ.get("MINIDFA_LOG_LEVEL", "INFO"))
