"""全局配置：数据库连接、判定规则版本与惩罚参数。

判定版本 (JUDGE_VERSION) 固化在每条证据/惩罚上，保存证据时同时记录判定版本，
便于将来规则升级后追溯历史证据所适用的规则。
"""
from __future__ import annotations

import os

# 默认走本机 PostgreSQL；测试通过环境变量覆盖。
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://slasher:slasher_pw@localhost:5432/slasher_db",
)

# 判定规则版本：证据的规范化、双签判定、签名规则一旦变化必须升级。
JUDGE_VERSION = "equivocation-rules/v1.0.0"

# 领域分离标签，防止其它协议消息复用同一段签名字节。
DOMAIN_SEPARATOR = b"OFFLINE_VOTE_SLASH/v1:"

# 双签惩罚比例：千分之一（10 bps = 1%），整数运算避免浮点。
SLASH_RATE_NUM = 1
SLASH_RATE_DEN = 100

# 故障注入：设为 "1" 时，在「证据已提交、惩罚尚未提交」之间退出进程，
# 用于真实崩溃恢复测试（见 tests/test_crash_recovery.py）。
CRASH_AFTER_EVIDENCE = os.environ.get("SLASHER_CRASH_AFTER_EVIDENCE", "") == "1"
