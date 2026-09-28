"""服务配置与排序规则。

排序规则（区域、强度、数字排序、大小写优先）的指纹是索引版本的一部分：
任何规则变更都会改变 index_version，从而强制重建索引并使旧游标失效。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass

STRENGTHS = ("primary", "secondary", "tertiary", "quaternary", "identical")
CASE_FIRST_OPTIONS = ("off", "upper_first", "lower_first")


class RulesError(ValueError):
    """排序规则不合法。"""


@dataclass(frozen=True)
class CollationRules:
    """不可变的排序规则集合，整体参与索引版本指纹计算。"""

    locale: str = "en_US"
    strength: str = "tertiary"
    numeric: bool = False
    case_first: str = "off"

    def __post_init__(self) -> None:
        if not self.locale or not isinstance(self.locale, str):
            raise RulesError("locale 必须是非空字符串")
        if self.strength not in STRENGTHS:
            raise RulesError(
                f"strength 必须是 {STRENGTHS} 之一，得到 {self.strength!r}"
            )
        if self.case_first not in CASE_FIRST_OPTIONS:
            raise RulesError(
                f"case_first 必须是 {CASE_FIRST_OPTIONS} 之一，得到 {self.case_first!r}"
            )
        if not isinstance(self.numeric, bool):
            raise RulesError("numeric 必须是布尔值")

    def canonical_dict(self) -> dict:
        """字段顺序固定的字典，保证指纹稳定。"""
        return {
            "case_first": self.case_first,
            "locale": self.locale,
            "numeric": self.numeric,
            "strength": self.strength,
        }

    def fingerprint(self) -> str:
        """规则指纹：同一组规则永远得到同一指纹。"""
        blob = json.dumps(self.canonical_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    def to_json(self) -> str:
        return json.dumps(self.canonical_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "CollationRules":
        return cls(**json.loads(raw))


def load_rules_from_env(env: dict | None = None) -> CollationRules:
    """从环境变量读取规则，供启动配置使用。"""
    env = os.environ if env is None else env
    return CollationRules(
        locale=env.get("COLLATION_LOCALE", "en_US"),
        strength=env.get("COLLATION_STRENGTH", "tertiary"),
        numeric=env.get("COLLATION_NUMERIC", "false").lower() in ("1", "true", "yes"),
        case_first=env.get("COLLATION_CASE_FIRST", "off"),
    )


def db_path_from_env(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("COLLATION_DB", "collation.db")


def corpus_path_from_env(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("COLLATION_CORPUS", "data/sample_corpus.json")
