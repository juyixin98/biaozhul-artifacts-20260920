"""Runtime settings and resource limits.

All values can be overridden through environment variables so that a clean
checkout can be reproduced exactly (see README). Defaults are chosen so the
bundled fixtures compile well within limits while pathological patterns are
rejected with ``RESOURCE_EXHAUSTED``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/lexer.db"
    max_rules: int = 128
    max_pattern_chars: int = 2000
    max_repeat: int = 1000
    max_nfa_states: int = 4096
    max_dfa_states: int = 8192
    max_input_chars: int = 1_000_000

    @staticmethod
    def from_env() -> "Settings":
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw is not None else default

        return Settings(
            db_path=os.environ.get("LEXER_DB_PATH", "data/lexer.db"),
            max_rules=_int("LEXER_MAX_RULES", 128),
            max_pattern_chars=_int("LEXER_MAX_PATTERN_CHARS", 2000),
            max_repeat=_int("LEXER_MAX_REPEAT", 1000),
            max_nfa_states=_int("LEXER_MAX_NFA_STATES", 4096),
            max_dfa_states=_int("LEXER_MAX_DFA_STATES", 8192),
            max_input_chars=_int("LEXER_MAX_INPUT_CHARS", 1_000_000),
        )
