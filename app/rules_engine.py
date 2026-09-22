"""可解释的规则引擎：加载规则包 → 编译 → 在文本上跑出四类实体。

每条命中都带 rule_id、原文 Unicode 起止位置、匹配到的原文片段与归一化规范名。
别名（aliases）只影响 canonical 字段，原始提及永远保留。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

VALID_TYPES = {"person", "org", "tech", "date"}
VALID_MATCH = {"dict", "regex"}
CANONICAL_NORMALIZERS = {
    "alias",
    "person_en_title",
    "org_en_suffix",
    "tech_versioned",
    "date_ymd",
    "date_ym",
    "date_en",
}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class RulePackError(ValueError):
    pass


@dataclass(frozen=True)
class RawMention:
    entity_type: str
    canonical: str
    alias: str
    matched_text: str
    start: int
    end: int
    rule_id: str
    priority: int


@dataclass(frozen=True)
class CompiledRule:
    rule_id: str
    entity_type: str
    match: str
    priority: int
    case_insensitive: bool
    # dict 模式
    alias_map: dict[str, str] | None  # 匹配串(已按大小写策略) -> canonical
    canonical_by_alias: dict[str, str] | None  # 原始 alias 形式 -> canonical
    # regex 模式
    pattern: re.Pattern[str] | None
    canonical_mode: str | None
    stop_chars: frozenset[str] = frozenset()  # 正则匹配体含这些字符则丢弃


@dataclass(frozen=True)
class CompiledPack:
    version: str
    name: str
    rules: tuple[CompiledRule, ...]
    raw: dict


def _content_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def derive_version(pack: dict) -> str:
    """版本号 = pack 声明版本 + 内容哈希前 12 位。同名同内容必然同版本。"""
    name = str(pack.get("pack_name", "pack"))
    ver = str(pack.get("pack_version", "0"))
    blob = json.dumps(pack, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{name}-{ver}-{_content_sha(blob)[:12]}"


def pack_content_sha(content: str) -> str:
    return _content_sha(content)


def _validate(pack: dict) -> None:
    if not isinstance(pack, dict):
        raise RulePackError("规则包必须是 JSON 对象")
    if not pack.get("pack_name") or not pack.get("pack_version"):
        raise RulePackError("缺少 pack_name / pack_version")
    rules = pack.get("rules")
    if not isinstance(rules, list) or not rules:
        raise RulePackError("rules 必须是非空数组")
    seen_ids: set[str] = set()
    for i, r in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            raise RulePackError(f"{where} 必须是对象")
        rid = r.get("rule_id")
        if not isinstance(rid, str) or not rid:
            raise RulePackError(f"{where} 缺少 rule_id")
        if rid in seen_ids:
            raise RulePackError(f"rule_id 重复: {rid}")
        seen_ids.add(rid)
        et = r.get("entity_type")
        if et not in VALID_TYPES:
            raise RulePackError(f"{where} entity_type 非法: {et!r}（允许 {sorted(VALID_TYPES)}）")
        m = r.get("match")
        if m not in VALID_MATCH:
            raise RulePackError(f"{where} match 非法: {m!r}")
        ci = r.get("case_insensitive", False)
        if not isinstance(ci, bool):
            raise RulePackError(f"{where} case_insensitive 必须是布尔")
        if m == "dict":
            aliases = r.get("aliases")
            if not isinstance(aliases, dict) or not aliases:
                raise RulePackError(f"{where} dict 规则需要非空 aliases")
            for canon, al in aliases.items():
                if not isinstance(canon, str) or not canon:
                    raise RulePackError(f"{where} 规范名必须是非空字符串")
                if not isinstance(al, list) or not al or not all(isinstance(x, str) and x for x in al):
                    raise RulePackError(f"{where} aliases[{canon!r}] 必须是非空字符串数组")
        else:
            pat = r.get("pattern")
            if not isinstance(pat, str) or not pat:
                raise RulePackError(f"{where} regex 规则需要 pattern")
            if "(?P<alias>" not in pat:
                raise RulePackError(f"{where} pattern 必须包含命名分组 (?P<alias>...)")
            mode = r.get("canonical", "alias")
            if mode not in CANONICAL_NORMALIZERS:
                raise RulePackError(
                    f"{where} canonical 归一化器未知: {mode!r}（允许 {sorted(CANONICAL_NORMALIZERS)}）"
                )
            try:
                re.compile(pat)
            except re.error as exc:
                raise RulePackError(f"{where} 正则编译失败: {exc}") from exc


def _build_dict_alternatives(alias_map: dict[str, list[str]], case_insensitive: bool):
    """把 alias 串编成一个带词边界感知的大正则。

    - 含拉丁/数字的串：两侧加边界，避免 ``Postgres`` 命中 ``PostgreSQL`` 之类；
    - 纯 CJK/符号串：不加边界（中文没有词边界概念）；
    - 长 alias 优先。
    返回 (compiled_pattern, key->canonical)。
    """
    has_latin = re.compile(r"[A-Za-z0-9]")

    def esc(s: str) -> str:
        return re.escape(s)

    entries: list[tuple[str, str, str]] = []  # (key, original, canonical)
    for canon, alias_list in alias_map.items():
        for a in alias_list:
            key = a.lower() if case_insensitive else a
            entries.append((key, a, canon))
    # 去重：同一 key 只保留第一个 canonical
    dedup: dict[str, str] = {}
    display: dict[str, str] = {}
    for key, original, canon in entries:
        if key not in dedup:
            dedup[key] = canon
            display[key] = original
    parts: list[str] = []
    for key in sorted(dedup, key=len, reverse=True):
        body = esc(key)
        if has_latin.search(key):
            body = r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"
        parts.append(body)
    flags = re.IGNORECASE if case_insensitive else 0
    pattern = re.compile("|".join(parts), flags)
    return pattern, dedup, display


def compile_pack(pack: dict) -> CompiledPack:
    _validate(pack)
    compiled: list[CompiledRule] = []
    for r in pack["rules"]:
        ci = bool(r.get("case_insensitive", False))
        if r["match"] == "dict":
            pattern, key_to_canon, display = _build_dict_alternatives(r["aliases"], ci)
            # 用包装对象携带查表；塞进 CompiledRule.pattern 与辅助 map
            compiled.append(
                CompiledRule(
                    rule_id=r["rule_id"],
                    entity_type=r["entity_type"],
                    match="dict",
                    priority=int(r.get("priority", 100)),
                    case_insensitive=ci,
                    alias_map=key_to_canon,
                    canonical_by_alias=display,
                    pattern=pattern,
                    canonical_mode=None,
                )
            )
        else:
            flags = 0
            if not r["pattern"].startswith("(?i)") and ci:
                flags = re.IGNORECASE
            stop = frozenset(str(r.get("stop_chars", "")))
            compiled.append(
                CompiledRule(
                    rule_id=r["rule_id"],
                    entity_type=r["entity_type"],
                    match="regex",
                    priority=int(r.get("priority", 100)),
                    case_insensitive=ci,
                    alias_map=None,
                    canonical_by_alias=None,
                    pattern=re.compile(r["pattern"], flags),
                    canonical_mode=r.get("canonical", "alias"),
                    stop_chars=stop,
                )
            )
    version = derive_version(pack)
    return CompiledPack(version=version, name=str(pack["pack_name"]), rules=tuple(compiled), raw=pack)


def compile_pack_json(content: str) -> CompiledPack:
    try:
        pack = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RulePackError(f"规则包不是合法 JSON: {exc}") from exc
    return compile_pack(pack)


# --------------------------------------------------------------------------- #
# 规范名归一化器
# --------------------------------------------------------------------------- #

def _norm_person_en_title(g: re.Match) -> str:
    # "Dr. Smith" -> "Dr Smith"（去句号、压缩空白），保留头衔与姓氏
    token = re.sub(r"\s+", " ", g.group("alias").replace(".", "")).strip()
    return token


def _norm_org_en_suffix(g: re.Match) -> str:
    token = re.sub(r"\s+", " ", g.group("alias")).strip(" ,.")
    # 统一常见后缀写法
    token = re.sub(r"\bCorp\.?$", "Corp", token)
    token = re.sub(r"\bInc\.?$", "Inc", token)
    token = re.sub(r"\bLtd\.?$", "Ltd", token)
    token = re.sub(r"\bLLC\.?$", "LLC", token)
    return token


def _norm_tech_versioned(g: re.Match) -> str:
    token = g.group("alias")
    m = re.match(r"^([A-Za-z]+)[ ._-]?(\d[\d.]*)$", token)
    if not m:
        return token
    name, ver = m.group(1), m.group(2)
    ver = re.sub(r"\.+$", "", ver)
    return f"{name} {ver}"


def _pad2(s: str) -> str:
    return s.zfill(2)


def _norm_date_ymd(g: re.Match) -> str:
    try:
        m_i, d_i = int(g.group("m")), int(g.group("d"))
        if not (1 <= m_i <= 12 and 1 <= d_i <= 31):
            return g.group("alias")
    except (IndexError, ValueError):
        return g.group("alias")
    return f"{int(g.group('y')):04d}-{_pad2(g.group('m'))}-{_pad2(g.group('d'))}"


def _norm_date_ym(g: re.Match) -> str:
    m_i = int(g.group("m"))
    if not (1 <= m_i <= 12):
        return g.group("alias")
    return f"{int(g.group('y')):04d}-{_pad2(g.group('m'))}"


def _norm_date_en(g: re.Match) -> str:
    mon = _MONTHS.get(g.group("m").lower().rstrip("."))
    if mon is None:
        return g.group("alias")
    d_i = int(g.group("d"))
    if not (1 <= d_i <= 31):
        return g.group("alias")
    return f"{int(g.group('y')):04d}-{mon:02d}-{d_i:02d}"


_NORMALIZERS = {
    "alias": lambda g: g.group("alias"),
    "person_en_title": _norm_person_en_title,
    "org_en_suffix": _norm_org_en_suffix,
    "tech_versioned": _norm_tech_versioned,
    "date_ymd": _norm_date_ymd,
    "date_ym": _norm_date_ym,
    "date_en": _norm_date_en,
}


# --------------------------------------------------------------------------- #
# 在文本上执行
# --------------------------------------------------------------------------- #

def _scan_rule(text: str, rule: CompiledRule) -> list[RawMention]:
    out: list[RawMention] = []
    if rule.match == "dict":
        assert rule.pattern is not None and rule.alias_map is not None
        for m in rule.pattern.finditer(text):
            key = m.group(0).lower() if rule.case_insensitive else m.group(0)
            canon = rule.alias_map[key]
            out.append(
                RawMention(
                    entity_type=rule.entity_type,
                    canonical=canon,
                    alias=rule.canonical_by_alias[key],  # 词典里登记的别名写法
                    matched_text=m.group(0),  # 原文里的真实表面形式（大小写等）
                    start=m.start(),
                    end=m.end(),
                    rule_id=rule.rule_id,
                    priority=rule.priority,
                )
            )
    else:
        assert rule.pattern is not None and rule.canonical_mode is not None
        norm = _NORMALIZERS[rule.canonical_mode]
        for m in rule.pattern.finditer(text):
            alias = m.group("alias")
            if rule.stop_chars and any(ch in rule.stop_chars for ch in alias):
                continue
            matched = m.group(0)
            out.append(
                RawMention(
                    entity_type=rule.entity_type,
                    canonical=norm(m),
                    alias=m.group("alias"),
                    matched_text=matched,
                    start=m.start(),
                    end=m.end(),
                    rule_id=rule.rule_id,
                    priority=rule.priority,
                )
            )
    return out


def extract(text: str, pack: CompiledPack) -> list[RawMention]:
    """扫描全部规则并做跨规则去重。

    冲突解决（确定性）：
    1. 与已选区间重叠的候选丢弃；区间集合贪心扫描；
    2. 候选按 (start ASC, 长度 DESC, priority ASC, rule_id ASC) 排序后贪心，
       因此长匹配/词典（小 priority）压过短/正则匹配；
    3. 不同类型同位置：排序后先者胜出（rule_id 字典序兜底，绝不依赖 dict 迭代序）。
    """
    candidates: list[RawMention] = []
    for rule in pack.rules:
        candidates.extend(_scan_rule(text, rule))
    candidates.sort(key=lambda x: (x.start, -(x.end - x.start), x.priority, x.rule_id))

    accepted: list[RawMention] = []
    occupied: list[tuple[int, int]] = []  # 已按 start 排序且互不相交
    for cand in candidates:
        # 与任何已接受区间重叠？
        overlap = False
        for s, e in reversed(occupied):
            if s >= cand.end:
                break
            if cand.start < e and s < cand.end:
                overlap = True
                break
        if overlap:
            continue
        # 插入 occupied（保持有序）
        occupied.append((cand.start, cand.end))
        occupied.sort()
        accepted.append(cand)
    accepted.sort(key=lambda x: (x.start, x.end, x.rule_id))
    return accepted
