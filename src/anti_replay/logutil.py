"""日志工具：结构化事件日志 + 密钥脱敏过滤器。

防重放服务的日志**绝不能**出现密钥或签名。防线有两层：

1. 代码层面只记录 key id、对等地址、拒绝原因、nonce 短指纹（SHA-256 前 8 hex）；
2. :class:`RedactingFilter` 再对已注册的密钥字符串（hex 等形态）做一次全消息替换，
   万一未来有代码误把密钥塞进日志，也会被打成 ``***REDACTED***``。
"""

from __future__ import annotations

import logging
import sys
from typing import Iterable

REDACTION_TOKEN = "***REDACTED***"


class RedactingFilter(logging.Filter):
    """把任何已知密钥字符串从日志记录中抹掉。"""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # 长字符串优先替换，避免“部分形态”漏网。
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def add_secret(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        leaked = False
        for secret in self._secrets:
            if secret in message:
                message = message.replace(secret, REDACTION_TOKEN)
                leaked = True
        if leaked:
            # 直接替换 msg/args，让 formatter 不再拼接原始参数。
            record.msg = message
            record.args = ()
        return True


def configure_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> logging.Logger:
    """配置 anti_replay 专属 logger，挂上脱敏过滤器。"""
    logger = logging.getLogger("anti_replay")
    logger.setLevel(level)
    logger.propagate = False

    redactor = RedactingFilter(secrets)

    # 幂等：重复配置时先清掉旧 handler 与旧过滤器。
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.filters = [f for f in logger.filters if not isinstance(f, RedactingFilter)]

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    # 过滤器同时挂在 logger 与 handler 上：无论日志走到哪个 handler，
    # 记录在分发前就已完成脱敏（filter 会就地改写 record.msg）。
    logger.addFilter(redactor)
    handler.addFilter(redactor)
    logger.addHandler(handler)
    logger.redactor = redactor  # type: ignore[attr-defined]
    return logger
