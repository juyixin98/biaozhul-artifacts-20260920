"""Name normalization: aliases and cross-language canonicalization.

This module deliberately does *not* decide identity. It only turns surface
strings into canonical forms and token sets so the similarity layer can
compare them. Two records with the same canonical form are still free to be
split by a cannot-link or by conflicting hard attributes ("same name != same
entity").

Three cross-language mechanisms are provided, all deterministic:

1. Unicode NFKC + case/punctuation/whitespace folding.
2. A small, explicit transliteration table (Cyrillic -> Latin) and a small
   CJK compatibility/variant map. These are intentionally minimal and
   table-driven; they are *not* a translation engine.
3. A user-supplied alias table, which is the intended bridge between
   genuinely different-language names (e.g. "Gazprom" <-> "Газпром").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .errors import InvalidRequestError

# ---------------------------------------------------------------------------
# Explicit transliteration / variant tables (scientific transliteration for
# Cyrillic; minimal CJK variants used by the fixtures). Extensible via the
# alias table rather than guessed.
# ---------------------------------------------------------------------------

_CYRILLIC_MAP: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Minimal traditional/compatibility -> simplified map. Not a full dictionary;
# full CJK conversion is out of scope and should be supplied via aliases.
_CJK_VARIANT_MAP: dict[str, str] = {
    "國": "国", "際": "际", "有": "有", "限": "限", "公": "公", "司": "司",
    "東": "东", "銀": "银", "行": "行", "集": "集", "團": "团",
}

# Legal-form suffixes stripped *only* as a whole token/segment, never as a
# substring, so "Atlas Copco" is not damaged by "co".
_LEGAL_SUFFIXES = {
    "ltd", "limited", "llc", "inc", "incorporated", "corp", "corporation",
    "company", "co", "gmbh", "ag", "plc", "lp", "ooo", "oa o", "zao", "pao",
    "oao",
}
_CJK_SUFFIXES = {"股份有限公司", "有限公司", "公司", "集团"}
# Cyrillic legal forms (post-transliteration / raw).
_CYRILLIC_LEGAL = {"ооо", "оао", "зао", "пао"}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedName:
    surface: str
    canonical: str           # suffix-stripped, folded, transliterated
    tokens: frozenset[str]   # whitespace tokens (Latin/Cyrillic)
    bigrams: frozenset[str]  # character bigrams (CJK / no-space scripts)
    script: str

    def bag(self) -> frozenset[str]:
        """Comparison bag: tokens for space scripts, bigrams for CJK."""
        return self.bigrams if self.script == "cjk" else self.tokens


def _detect_script(text: str) -> str:
    for ch in text:
        if "一" <= ch <= "鿿":
            return "cjk"
    if any(ch in _CYRILLIC_MAP or ch.upper() in {c.upper() for c in _CYRILLIC_MAP}
           for ch in text):
        return "cyrillic"
    return "latin"


def _fold_cjk(text: str) -> str:
    return "".join(_CJK_VARIANT_MAP.get(ch, ch) for ch in text)


def _transliterate(text: str) -> str:
    out: list[str] = []
    for ch in text.lower():
        if ch in _CYRILLIC_MAP:
            out.append(_CYRILLIC_MAP[ch])
        else:
            out.append(ch)
    return "".join(out)


def _strip_cjk_suffix(text: str) -> str:
    # Longest suffix first so "有限公司" wins over the contained "公司".
    for suffix in sorted(_CJK_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _tokens_for(script: str, folded: str) -> tuple[frozenset[str], frozenset[str]]:
    if script == "cjk":
        core = _strip_cjk_suffix(folded.replace(" ", ""))
        # Bigrams with a single-char fallback for very short names.
        grams = {core[i : i + 2] for i in range(max(0, len(core) - 1))} or {core}
        return frozenset(), frozenset(grams)

    parts = _SPACE_RE.split(folded.strip())
    tokens: set[str] = set()
    for part in parts:
        if not part:
            continue
        if part in _LEGAL_SUFFIXES or part in _CYRILLIC_LEGAL:
            continue
        tokens.add(part)
    return frozenset(tokens), frozenset()


def normalize_name(surface: str) -> NormalizedName:
    """Fold a raw surface name into its canonical, language-neutral form."""
    if not surface or not surface.strip():
        raise InvalidRequestError(
            "name must not be empty", {"field": "name"}
        )
    # NFKC folds full-width Latin, compatibility forms, ligatures, etc.
    nfkc = unicodedata.normalize("NFKC", surface)
    script = _detect_script(nfkc)
    if script == "cjk":
        folded = _fold_cjk(nfkc)
        folded = folded.lower()
    else:
        lowered = nfkc.lower()
        folded = _transliterate(lowered) if script == "cyrillic" else lowered
    folded = _PUNCT_RE.sub(" ", folded)
    folded = _SPACE_RE.sub(" ", folded).strip()
    tokens, bigrams = _tokens_for(script, folded)

    canonical = (
        _strip_cjk_suffix(folded.replace(" ", ""))
        if script == "cjk"
        else " ".join(sorted(tokens))
    )
    return NormalizedName(
        surface=surface,
        canonical=canonical,
        tokens=tokens,
        bigrams=bigrams,
        script=script,
    )


class AliasIndex:
    """Maps every known spelling (canonical surface + aliases) to a key.

    The key is the *normalized canonical surface*. Records whose normalized
    name matches any alias of ``canonical`` resolve to that same key, which
    lets the similarity layer recognize cross-language / abbreviation bridges
    without declaring identity.
    """

    def __init__(self, aliases: dict[str, list[str]] | None = None) -> None:
        # normalized alias/name -> normalized canonical key
        self._lookup: dict[str, str] = {}
        if aliases:
            for canonical, alt_names in aliases.items():
                self.add_group(canonical, alt_names)

    def add_group(self, canonical: str, alt_names: list[str]) -> None:
        key = normalize_name(canonical).canonical
        members = [canonical, *alt_names]
        for member in members:
            norm = normalize_name(member).canonical
            existing = self._lookup.get(norm)
            if existing is not None and existing != key:
                raise InvalidRequestError(
                    "alias assigned to two different canonical names",
                    {"alias": norm, "surface": member,
                     "first": existing, "second": key},
                )
            self._lookup[norm] = key

    def resolve(self, name: NormalizedName) -> str:
        """Return the alias-group key, or the name's own canonical if none."""
        return self._lookup.get(name.canonical, name.canonical)

    def same_alias_group(self, left: NormalizedName, right: NormalizedName) -> bool:
        return self.resolve(left) == self.resolve(right)

    def __len__(self) -> int:
        return len(self._lookup)
