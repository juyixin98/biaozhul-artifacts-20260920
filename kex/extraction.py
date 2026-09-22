"""Explainable local extraction engine.

No models, no network. Four entity types are recognized:

* ``PERSON``       — gazetteer dictionary of surnames + full-name aliases
* ``ORG``          — gazetteer dictionary + organizational suffix patterns
* ``TECH``         — gazetteer dictionary + tech token pattern rules
* ``DATE``         — deterministic regular-expression rules with an
                     explicit normalizer (Chinese and English date forms)

Every returned mention records the exact rule id that fired and the
original [start_char, end_char) character offsets into the source text, so
results are fully auditable. Aliases resolve to a canonical name while the
original surface form is always preserved.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

ENTITY_TYPES = ("PERSON", "ORG", "TECH", "DATE")

# Snapshot schema version — bump when the on-disk JSON layout changes.
SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Mention:
    entity_type: str
    text: str
    canonical_name: str
    start_char: int
    end_char: int
    matched_rule: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "text": self.text,
            "canonical_name": self.canonical_name,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "matched_rule": self.matched_rule,
        }


class RuleValidationError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Trie dictionary
# --------------------------------------------------------------------------- #
class _TrieNode:
    __slots__ = ("children", "value")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        # value: (canonical_name, rule_id, priority)
        self.value: tuple[str, str, int] | None = None


class TrieDictionary:
    """Longest-match dictionary with optional Latin word boundaries."""

    def __init__(self) -> None:
        self.root = _TrieNode()

    def add(self, surface: str, canonical: str, rule_id: str, priority: int) -> None:
        node = self.root
        for ch in surface:
            node = node.children.setdefault(ch, _TrieNode())
        # Longer explicit entries / higher priority win at the same span.
        if node.value is None or priority >= node.value[2]:
            node.value = (canonical, rule_id, priority)

    def match_at(self, text: str, pos: int) -> tuple[int, str, str, str, int] | None:
        """Longest match starting exactly at ``pos``.

        Returns (end_char, surface, canonical_name, rule_id, priority).
        """
        node = self.root
        best: tuple[int, str, str, str, int] | None = None
        i = pos
        n = len(text)
        while i < n:
            child = node.children.get(text[i])
            if child is None:
                break
            node = child
            i += 1
            if node.value is not None:
                canonical, rule_id, priority = node.value
                best = (i, text[pos:i], canonical, rule_id, priority)
        return best


def _is_latin_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


# --------------------------------------------------------------------------- #
# Date rules
# --------------------------------------------------------------------------- #
_CN_NUM = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_year(token: str) -> int | None:
    """Parse years like 二〇二五 / 二零二五; return None if not 4 digits."""
    if len(token) != 4:
        return None
    digits: list[int] = []
    for ch in token:
        if ch in _CN_NUM:
            digits.append(_CN_NUM[ch])
        else:
            return None
    year = digits[0] * 1000 + digits[1] * 100 + digits[2] * 10 + digits[3]
    return year if 1000 <= year <= 9999 else None


_CN_MONTH_DAY = re.compile(r"^([0-9]{1,2}|[一二两三四五六七八九十]{1,3})月([0-9]{1,2}|[一二三四五六七八九十]{1,3})日$")


def _cn_small(token: str) -> int | None:
    """Parse a 1..31 style Chinese number (月/日 parts)."""
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    # forms: 十一, 二十, 二十三, 三十一
    if "十" in token:
        left, _, right = token.partition("十")
        tens = 1 if left == "" else _CN_NUM.get(left)
        if tens is None:
            return None
        if right == "":
            return tens * 10
        ones = _CN_NUM.get(right)
        if ones is None:
            return None
        return tens * 10 + ones
    val = 0
    for ch in token:
        if ch not in _CN_NUM:
            return None
        val = val * 10 + _CN_NUM[ch]
    return val


_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _ymd(year: int, month: int, day: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1000 <= year <= 9999):
        return None
    try:
        import datetime as _dt

        _dt.date(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


# Ordered patterns. Each entry: (rule_id, compiled regex, builder)
def _build_date_rules() -> list[tuple[str, re.Pattern[str], Any]]:
    def iso(m: re.Match[str]) -> str | None:
        return _ymd(int(m.group("y")), int(m.group("mo")), int(m.group("d")))

    def cn_full(m: re.Match[str]) -> str | None:
        year = _cn_year(m.group("y"))
        md = _CN_MONTH_DAY.match(m.group("md") + "月" + m.group("d") + "日")
        if year is None or md is None:
            return None
        month = _cn_small(md.group(1))
        day = _cn_small(md.group(2))
        if month is None or day is None:
            return None
        return _ymd(year, month, day)

    def slash(m: re.Match[str]) -> str | None:
        return _ymd(int(m.group("y")), int(m.group("mo")), int(m.group("d")))

    def english(m: re.Match[str]) -> str | None:
        month = _EN_MONTHS.get(m.group("mon").lower())
        if month is None:
            return None
        return _ymd(int(m.group("y")), month, int(m.group("d")))

    month_alt = "|".join(sorted(_EN_MONTHS, key=len, reverse=True))
    return [
        (
            "date.iso",
            re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})(?!\d)"),
            iso,
        ),
        (
            "date.cn",
            re.compile(
                r"(?<![〇零一二两三四五六七八九])(?P<y>[〇零一二两三四五六七八九]{4})年"
                r"(?P<md>[0-9]{1,2}|[一二两三四五六七八九十]{1,3})月"
                r"(?P<d>[0-9]{1,2}|[一二三四五六七八九十]{1,3})日(?:(?![〇零一二两三四五六七八九十]))"
            ),
            cn_full,
        ),
        (
            "date.slash",
            re.compile(r"(?<!\d)(?P<y>\d{4})/(?P<mo>\d{1,2})/(?P<d>\d{1,2})(?!\d)"),
            slash,
        ),
        (
            "date.english",
            re.compile(
                rf"(?<![A-Za-z])(?P<mon>{month_alt})\.?\s+(?P<d>\d{{1,2}}),?\s+"
                rf"(?P<y>\d{{4}})(?!\d)",
                re.IGNORECASE,
            ),
            english,
        ),
    ]


_DATE_RULES = _build_date_rules()


# --------------------------------------------------------------------------- #
# Compiled rule set
# --------------------------------------------------------------------------- #
class CompiledRules:
    def __init__(self, snapshot: dict[str, Any]):
        self.snapshot = snapshot
        self.version_tag = snapshot.get("schema_version", SNAPSHOT_SCHEMA_VERSION)
        self.tries: dict[str, TrieDictionary] = {t: TrieDictionary() for t in ENTITY_TYPES if t != "DATE"}
        self._compile_gazetteer(snapshot.get("gazetteer", {}))
        # TECH regex pattern rules, e.g. versioned products "Kafka 2\\.6"
        self.tech_patterns: list[tuple[str, re.Pattern[str], int]] = []
        for i, pr in enumerate(snapshot.get("tech_patterns", [])):
            self.tech_patterns.append(
                (
                    pr["rule_id"],
                    re.compile(pr["pattern"]),
                    int(pr.get("priority", 50)),
                )
            )
        # Boundaries are enforced for ASCII-starting entries so "EU" does not
        # fire inside "europe"; CJK entries match on every position.
        self.date_rules_enabled: bool = bool(snapshot.get("date_rules_enabled", True))

    def _compile_gazetteer(self, gaz: dict[str, Any]) -> None:
        for entity_type in self.tries:
            entries = gaz.get(entity_type, [])
            for entry in entries:
                canonical = entry["canonical"]
                rule_id = entry.get("rule_id") or f"gazetteer.{entity_type.lower()}.{canonical}"
                priority = int(entry.get("priority", 100))
                aliases = entry.get("aliases", [])
                if canonical not in aliases:
                    aliases = [canonical, *aliases]
                for alias in aliases:
                    if not alias:
                        continue
                    self.tries[entity_type].add(alias, canonical, rule_id, priority)

    # ------------------------------------------------------------------ #
    def extract(self, text: str) -> list[Mention]:
        n = len(text)
        candidates: list[Mention] = []

        # Gazetteer matches at every position.
        for pos in range(n):
            for entity_type, trie in self.tries.items():
                hit = trie.match_at(text, pos)
                if hit is None:
                    continue
                end, surface, canonical, rule_id, _priority = hit
                if self._has_latin_boundary(text, pos, end, surface):
                    candidates.append(
                        Mention(entity_type, surface, canonical, pos, end, rule_id)
                    )

        # TECH regex rules.
        for rule_id, pattern, _priority in self.tech_patterns:
            for m in pattern.finditer(text):
                candidates.append(
                    Mention("TECH", m.group(0), m.group(0), m.start(), m.end(), rule_id)
                )

        # DATE regex rules.
        if self.date_rules_enabled:
            for rule_id, pattern, builder in _DATE_RULES:
                for m in pattern.finditer(text):
                    canonical = builder(m)
                    if canonical is not None:
                        candidates.append(
                            Mention("DATE", m.group(0), canonical, m.start(), m.end(), rule_id)
                        )

        return self._resolve_overlaps(candidates)

    @staticmethod
    def _gaz_rule_id(entity_type: str, canonical: str) -> str:
        return f"gazetteer.{entity_type.lower()}.{canonical}"

    @staticmethod
    def _has_latin_boundary(text: str, start: int, end: int, surface: str) -> bool:
        """Require non-alnum neighbors when the surface starts/ends ASCII."""
        if _is_latin_alnum(surface[0]):
            if start > 0 and _is_latin_alnum(text[start - 1]):
                return False
        if _is_latin_alnum(surface[-1]):
            if end < len(text) and _is_latin_alnum(text[end]):
                return False
        return True

    @staticmethod
    def _resolve_overlaps(mentions: list[Mention]) -> list[Mention]:
        """Greedy longest-span resolution with fully deterministic ties.

        Order by longest span, then earliest start, then a fixed type order
        and matched_rule/end_char; suppress any later candidate that overlaps
        an accepted span.
        """
        type_rank = {t: i for i, t in enumerate(ENTITY_TYPES)}
        ordered = sorted(
            mentions,
            key=lambda m: (
                -(m.end_char - m.start_char),
                m.start_char,
                type_rank.get(m.entity_type, 99),
                m.matched_rule,
                m.end_char,
            ),
        )
        accepted: list[Mention] = []
        occupied: list[tuple[int, int]] = []
        for m in ordered:
            if any(m.start_char < e and s < m.end_char for s, e in occupied):
                continue
            accepted.append(m)
            occupied.append((m.start_char, m.end_char))
        accepted.sort(key=lambda m: (m.start_char, m.end_char))
        return accepted


# --------------------------------------------------------------------------- #
# Snapshot helpers
# --------------------------------------------------------------------------- #
def canonicalize_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a client-supplied rule set and return canonical JSON form."""
    if not isinstance(raw, dict):
        raise RuleValidationError("rules must be a JSON object")
    gazetteer = raw.get("gazetteer", {})
    if not isinstance(gazetteer, dict):
        raise RuleValidationError("gazetteer must be an object")
    clean_gaz: dict[str, list[dict[str, Any]]] = {}
    seen_aliases: dict[str, dict[str, str]] = {}
    for etype in ENTITY_TYPES:
        if etype == "DATE":
            continue
        entries = gazetteer.get(etype, [])
        if not isinstance(entries, list):
            raise RuleValidationError(f"gazetteer.{etype} must be a list")
        clean_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("canonical"):
                raise RuleValidationError(f"{etype} entry requires canonical name")
            canonical = str(entry["canonical"]).strip()
            if not canonical:
                raise RuleValidationError(f"{etype} canonical name is blank")
            aliases = entry.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                raise RuleValidationError(f"{canonical} aliases must be a list")
            aliases = [str(a).strip() for a in aliases if str(a).strip()]
            rule_id = str(entry.get("rule_id") or f"gazetteer.{etype.lower()}.{canonical}")
            priority = int(entry.get("priority", 100))
            for alias in [canonical, *aliases]:
                prev = seen_aliases.setdefault(etype, {}).get(alias.lower())
                if prev is not None and prev != canonical:
                    raise RuleValidationError(
                        f"alias {alias!r} maps to both {prev!r} and {canonical!r} in {etype}"
                    )
                seen_aliases[etype][alias.lower()] = canonical
            clean_entries.append(
                {"canonical": canonical, "aliases": sorted(set(aliases)),
                 "rule_id": rule_id, "priority": priority}
            )
        clean_gaz[etype] = clean_entries

    tech_patterns: list[dict[str, Any]] = []
    for i, pr in enumerate(raw.get("tech_patterns", [])):
        if not isinstance(pr, dict) or "pattern" not in pr:
            raise RuleValidationError("each tech_patterns entry needs a regex pattern")
        try:
            compiled = re.compile(pr["pattern"])
        except re.error as exc:
            raise RuleValidationError(f"invalid regex in tech_patterns[{i}]: {exc}") from exc
        compiled.fullmatch("")  # touch to validate eagerly
        tech_patterns.append(
            {
                "pattern": pr["pattern"],
                "rule_id": str(pr.get("rule_id") or f"tech_pattern.{i}"),
                "priority": int(pr.get("priority", 50)),
            }
        )

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "gazetteer": clean_gaz,
        "tech_patterns": tech_patterns,
        "date_rules_enabled": bool(raw.get("date_rules_enabled", True)),
    }
    return snapshot


def serialize_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compile_snapshot(snapshot_json: str | dict[str, Any]) -> CompiledRules:
    snapshot = (
        json.loads(snapshot_json) if isinstance(snapshot_json, str) else snapshot_json
    )
    return CompiledRules(snapshot)


def builtin_snapshot() -> dict[str, Any]:
    """The default explainable dictionary shipped with the product."""
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "date_rules_enabled": True,
        "gazetteer": {
            "PERSON": [
                {"canonical": "李明", "aliases": ["小李", "明哥"],
                 "rule_id": "gazetteer.person.liming"},
                {"canonical": "王芳", "aliases": ["芳芳"],
                 "rule_id": "gazetteer.person.wangfang"},
                {"canonical": "张伟", "aliases": ["老张", "伟哥"],
                 "rule_id": "gazetteer.person.zhangwei"},
                {"canonical": "Alice Chen", "aliases": ["Alice", "Dr. Chen"],
                 "rule_id": "gazetteer.person.alice_chen"},
                {"canonical": "Bob Müller", "aliases": ["Bob"],
                 "rule_id": "gazetteer.person.bob_muller"},
            ],
            "ORG": [
                {"canonical": "清华大学", "aliases": ["清华", "Tsinghua University"],
                 "rule_id": "gazetteer.org.tsinghua"},
                {"canonical": "北京大学", "aliases": ["北大", "Peking University"],
                 "rule_id": "gazetteer.org.pku"},
                {"canonical": "Acme Corp", "aliases": ["Acme", "Acme Corporation"],
                 "rule_id": "gazetteer.org.acme"},
                {"canonical": "联合国", "aliases": ["UN", "United Nations", "United Nations (UN)"],
                 "rule_id": "gazetteer.org.un"},
            ],
            "TECH": [
                {"canonical": "Apache Kafka", "aliases": ["Kafka", "kafka"],
                 "rule_id": "gazetteer.tech.kafka"},
                {"canonical": "PostgreSQL", "aliases": ["postgres", "Postgres"],
                 "rule_id": "gazetteer.tech.postgres"},
                {"canonical": "Python", "aliases": ["python3", "CPython"],
                 "rule_id": "gazetteer.tech.python"},
                {"canonical": "SQLite", "aliases": ["sqlite3"],
                 "rule_id": "gazetteer.tech.sqlite"},
                {"canonical": "Flask", "aliases": [],
                 "rule_id": "gazetteer.tech.flask"},
            ],
        },
        "tech_patterns": [
            # Explicit regex rules demonstrate pattern-based technical names
            # beyond the static dictionary (e.g. versioned products).
            {"pattern": r"(?<![A-Za-z])Kafka\s+\d+(?:\.\d+)+(?!\d)",
             "rule_id": "tech_pattern.kafka_version", "priority": 120},
            {"pattern": r"(?<![A-Za-z])Python\s+\d+\.\d+(?!\d)",
             "rule_id": "tech_pattern.python_version", "priority": 120},
        ],
    }
