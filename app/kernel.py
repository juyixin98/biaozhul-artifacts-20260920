"""挖掘内核：基于成熟排序库 PyICU 的排序键生成与稳定排序。

内核是唯一直接接触 ICU 的模块。排序键（bytes）的字典序即 ICU 全序；
索引与查询层只比较排序键，绝不回退到 UTF-8 字节比较。
"""
from __future__ import annotations

import icu

from .config import CollationRules
from .corpus import CorpusEntry

_STRENGTH_MAP = {
    "primary": icu.Collator.PRIMARY,
    "secondary": icu.Collator.SECONDARY,
    "tertiary": icu.Collator.TERTIARY,
    "quaternary": icu.Collator.QUATERNARY,
    "identical": icu.Collator.IDENTICAL,
}

_CASE_FIRST_MAP = {
    "upper_first": icu.UCollAttributeValue.UPPER_FIRST,
    "lower_first": icu.UCollAttributeValue.LOWER_FIRST,
}


class CollationKernel:
    """把 Unicode 字符串映射为排序键，并给出稳定全序。"""

    def __init__(self, rules: CollationRules) -> None:
        self.rules = rules
        collator = icu.Collator.createInstance(icu.Locale(rules.locale))
        collator.setStrength(_STRENGTH_MAP[rules.strength])
        collator.setAttribute(
            icu.UCollAttribute.NUMERIC_COLLATION,
            icu.UCollAttributeValue.ON if rules.numeric else icu.UCollAttributeValue.OFF,
        )
        if rules.case_first != "off":
            collator.setAttribute(
                icu.UCollAttribute.CASE_FIRST, _CASE_FIRST_MAP[rules.case_first]
            )
        self._collator = collator
        self.icu_version: str = icu.ICU_VERSION
        # 索引版本 = ICU 版本 + 规则指纹：规则或库升级都会改变版本。
        self.index_version: str = f"icu{icu.ICU_VERSION}-{rules.fingerprint()}"

    def sort_key(self, text: str) -> bytes:
        """ICU 排序键；bytes 的字典序与 ICU 全序一致。"""
        key = self._collator.getSortKey(text)
        if not isinstance(key, bytes):
            # 防御：PyICU 版本差异时显式失败，而不是静默改变排序语义。
            raise TypeError(f"getSortKey 应返回 bytes，得到 {type(key).__name__}")
        return key

    def sort_entries(self, entries: list[CorpusEntry]) -> list[tuple[CorpusEntry, bytes]]:
        """稳定全序：主键为排序键，相等时按载入顺序 seq 决胜。

        Python sorted 本身稳定，这里显式给出 (sort_key, seq) 使决胜规则
        不依赖排序算法的稳定性承诺。
        """
        keyed = [(entry, self.sort_key(entry.text)) for entry in entries]
        keyed.sort(key=lambda pair: (pair[1], pair[0].seq))
        return keyed
