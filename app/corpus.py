"""语料规范：载入、校验与身份保留。

核心约束：规范等价（canonical-equivalent）但原文不同的字符串是不同身份，
载入时绝不规范化合并；原文逐字节保留，NFC 形式仅作为派生字段另存。
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import corpus_error


@dataclass(frozen=True)
class CorpusEntry:
    """一条语料：身份 id + 原文 text + 载入顺序 seq（稳定排序的决胜键）。"""

    id: str
    text: str
    seq: int

    @property
    def nfc(self) -> str:
        """NFC 派生形式，仅用于展示与等价性检测，不参与身份。"""
        return unicodedata.normalize("NFC", self.text)


def validate_entries(raw_entries: Iterable[dict]) -> list[CorpusEntry]:
    """校验原始语料并赋予载入顺序。

    失败类别：CORPUS_ERROR（id 缺失/重复、text 缺失/非字符串/为空）。
    """
    entries: list[CorpusEntry] = []
    seen_ids: set[str] = set()
    for seq, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise corpus_error("语料条目必须是对象", position=seq, got=type(raw).__name__)
        entry_id = raw.get("id")
        text = raw.get("text")
        if not entry_id or not isinstance(entry_id, str):
            raise corpus_error("语料条目缺少字符串 id", position=seq)
        if entry_id in seen_ids:
            raise corpus_error("语料条目 id 重复", position=seq, id=entry_id)
        if not isinstance(text, str) or text == "":
            raise corpus_error("语料条目 text 必须是非空字符串", position=seq, id=entry_id)
        seen_ids.add(entry_id)
        entries.append(CorpusEntry(id=entry_id, text=text, seq=seq))
    if not entries:
        raise corpus_error("语料为空")
    return entries


def load_corpus(path: str | Path) -> list[CorpusEntry]:
    """从 JSON 文件载入语料。文件格式：[{"id": ..., "text": ...}, ...]"""
    path = Path(path)
    if not path.exists():
        raise corpus_error("语料文件不存在", path=str(path))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise corpus_error("语料文件不是合法 UTF-8 JSON", path=str(path), reason=str(exc))
    if not isinstance(raw, list):
        raise corpus_error("语料文件顶层必须是数组", path=str(path))
    return validate_entries(raw)


def canonical_equivalence_groups(entries: list[CorpusEntry]) -> dict[str, list[str]]:
    """按 NFC 分组，用于检测规范等价但原文不同的身份簇（仅供报告）。"""
    groups: dict[str, list[str]] = {}
    for entry in entries:
        groups.setdefault(entry.nfc, []).append(entry.id)
    return {nfc: ids for nfc, ids in groups.items() if len(ids) > 1}
