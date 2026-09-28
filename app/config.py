"""固定配置与可调资源上限。

固定项（不可配置，属于语言契约）：
- Unicode 字母表固定为 U+0000..U+10FFFF
- 换行模式固定为 LF（\\n）；`.` 不匹配换行；\\r 视为普通字符
- 匹配语义：最长匹配优先，其次显式规则优先级，再其次声明顺序

可调项（资源上限，超限抛 ResourceExhausted）：可用环境变量覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# ---- 固定语言契约 ----
ALPHABET_LO = 0x00
ALPHABET_HI = 0x10FFFF
NEWLINE_CP = 0x0A  # '\n'，唯一换行符


@dataclass(frozen=True)
class Limits:
    max_rules: int = 64
    max_pattern_len: int = 2000
    max_repeat: int = 1000
    max_nfa_states: int = 20000
    max_dfa_states: int = 20000
    max_product_states: int = 200000
    max_input_chars: int = 1_000_000

    @staticmethod
    def from_env() -> "Limits":
        def env_int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw else default

        return Limits(
            max_rules=env_int("LEXGEN_MAX_RULES", 64),
            max_pattern_len=env_int("LEXGEN_MAX_PATTERN_LEN", 2000),
            max_repeat=env_int("LEXGEN_MAX_REPEAT", 1000),
            max_nfa_states=env_int("LEXGEN_MAX_NFA_STATES", 20000),
            max_dfa_states=env_int("LEXGEN_MAX_DFA_STATES", 20000),
            max_product_states=env_int("LEXGEN_MAX_PRODUCT_STATES", 200000),
            max_input_chars=env_int("LEXGEN_MAX_INPUT_CHARS", 1_000_000),
        )


@dataclass(frozen=True)
class Settings:
    db_path: str = "./lexgen.db"
    limits: Limits = field(default_factory=Limits.from_env)

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            db_path=os.environ.get("LEXGEN_DB", "./lexgen.db"),
            limits=Limits.from_env(),
        )
