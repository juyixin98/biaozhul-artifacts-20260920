"""语料规范：词元规则规范的结构模型（查询验证的第一道边界）。"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_RULE_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SPEC_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


class TokenRuleIn(BaseModel):
    """单条词元规则。priority 大者等长优先；skip 为真的规则匹配但不产出词元。"""

    name: str = Field(min_length=1, max_length=64)
    pattern: str = Field(min_length=1)
    priority: int = 0
    skip: bool = False

    @field_validator("name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        if not _RULE_NAME.match(v):
            raise ValueError("规则名须为大写标识符（^[A-Z][A-Z0-9_]*$）")
        return v


class LexerSpecIn(BaseModel):
    """词法规范围绕一组有序规则；声明顺序是同优先级并列时的决胜依据。"""

    name: str = Field(min_length=1, max_length=64)
    version: str = Field(default="1", min_length=1, max_length=32)
    rules: list[TokenRuleIn] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _spec_name_ok(cls, v: str) -> str:
        if not _SPEC_NAME.match(v):
            raise ValueError("规范名须为小写 kebab-case（^[a-z][a-z0-9-]*$）")
        return v

    @field_validator("rules")
    @classmethod
    def _rule_names_unique(cls, v: list[TokenRuleIn]) -> list[TokenRuleIn]:
        seen: set[str] = set()
        for r in v:
            if r.name in seen:
                raise ValueError(f"规则名重复: {r.name}")
            seen.add(r.name)
        return v
