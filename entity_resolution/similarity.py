"""Pairwise similarity and per-pair candidate evidence.

This module scores *pairs only*. It never groups records and never propagates
a match: a high A-B and B-C score says nothing about A-C. Transitivity is a
property the clustering layer must earn under constraints, not assume.

"Same name != same entity" is enforced two ways here and one way downstream:

* Hard attributes (e.g. a registered legal identifier / jurisdiction) that are
  present on both sides but disagree produce a *veto*: the pair is never a
  candidate no matter how similar the text is.
* An identical/alias name is still only evidence; a downstream cannot-link
  constraint always overrides it.

The score is an explainable blend, not a single opaque number: callers receive
every component plus the matching/conflicting attributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .normalization import AliasIndex, NormalizedName, normalize_name

# Attribute keys whose inequality (when both are present) vetoes a candidate.
# Populated from configuration; empty by default so synthetic corpora opt in.
DEFAULT_HARD_ATTRIBUTE_KEYS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SimilarityConfig:
    threshold: float = 0.75
    weight_token: float = 0.45
    weight_char: float = 0.35
    alias_boost: float = 0.20
    # When both sides share every present hard attribute, add this much.
    attribute_agree_boost: float = 0.10
    hard_attribute_keys: frozenset[str] = DEFAULT_HARD_ATTRIBUTE_KEYS

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        total = self.weight_token + self.weight_char
        if not 0.0 <= total <= 1.0:
            raise ValueError("token+char weights must be in [0, 1]")


@dataclass(frozen=True)
class PreparedRecord:
    """A record plus its normalized name (the indexing unit)."""

    id: str
    name: str
    language: str
    attributes: dict[str, str]
    normalized: NormalizedName


@dataclass(frozen=True)
class PairScore:
    left: str
    right: str
    left_canonical: str
    right_canonical: str
    score: float
    token_overlap: float
    char_similarity: float
    alias_match: bool
    same_attributes: dict[str, str] = field(default_factory=dict)
    conflicting_attributes: dict[str, str] = field(default_factory=dict)
    vetoed: bool = False
    candidate: bool = False

    def as_evidence(self) -> dict:
        """Wire-shaped evidence dict (validated into PairEvidence upstream)."""
        return {
            "left": self.left,
            "right": self.right,
            "left_canonical": self.left_canonical,
            "right_canonical": self.right_canonical,
            "score": round(self.score, 6),
            "token_overlap": round(self.token_overlap, 6),
            "char_similarity": round(self.char_similarity, 6),
            "alias_match": self.alias_match,
            "same_attributes": self.same_attributes,
            "conflicting_attributes": self.conflicting_attributes,
            "candidate": self.candidate,
        }


def prepare_record(
    record_id: str,
    name: str,
    language: str = "",
    attributes: dict[str, str] | None = None,
) -> PreparedRecord:
    return PreparedRecord(
        id=record_id,
        name=name,
        language=language,
        attributes=dict(attributes or {}),
        normalized=normalize_name(name),
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 0.0
    inter = len(left & right)
    union = len(left | right)
    return inter / union if union else 0.0


def _char_ratio(left: NormalizedName, right: NormalizedName) -> float:
    # Compare suffix-stripped canonical cores. CJK compares the unspaced core;
    # alphabetic compares the space-joined sorted token string.
    return SequenceMatcher(None, left.canonical, right.canonical).ratio()


def _compare_attributes(
    left_attrs: dict[str, str],
    right_attrs: dict[str, str],
    hard_keys: frozenset[str],
) -> tuple[dict[str, str], dict[str, str], bool]:
    same: dict[str, str] = {}
    conflicting: dict[str, str] = {}
    veto = False
    for key, lval in left_attrs.items():
        if key not in right_attrs:
            continue
        rval = right_attrs[key]
        if lval == rval:
            same[key] = lval
        else:
            conflicting[key] = f"{lval} != {rval}"
            if key in hard_keys:
                veto = True
    return same, conflicting, veto


def score_pair(
    left: PreparedRecord,
    right: PreparedRecord,
    config: SimilarityConfig,
    aliases: AliasIndex | None = None,
) -> PairScore:
    """Compute explainable evidence for one ordered-independent pair."""
    ln, rn = left.normalized, right.normalized

    # Cross-script bag comparison: when scripts differ, compare canonical
    # strings directly (transliteration/alias does the bridging), and use a
    # token/bigram Jaccard that is meaningful within each script family.
    if ln.script == rn.script:
        token_overlap = _jaccard(ln.bag(), rn.bag())
    else:
        # Across scripts there are no shared surface tokens; rely on char core
        # and alias resolution rather than a misleading 0/1 token overlap.
        token_overlap = 1.0 if aliases and aliases.same_alias_group(ln, rn) else 0.0

    char_similarity = _char_ratio(ln, rn)
    alias_match = bool(aliases and aliases.same_alias_group(ln, rn))

    same, conflicting, veto = _compare_attributes(
        left.attributes, right.attributes, config.hard_attribute_keys
    )

    score = (
        config.weight_token * token_overlap
        + config.weight_char * char_similarity
    )
    if alias_match:
        score += config.alias_boost
    if same and not conflicting:
        score += config.attribute_agree_boost
    score = min(1.0, score)

    if veto:
        # Hard disagreement collapses the score and forbids candidacy.
        score = min(score, 0.0)

    candidate = (not veto) and score >= config.threshold

    return PairScore(
        left=left.id,
        right=right.id,
        left_canonical=ln.canonical,
        right_canonical=rn.canonical,
        score=score,
        token_overlap=token_overlap,
        char_similarity=char_similarity,
        alias_match=alias_match,
        same_attributes=same,
        conflicting_attributes=conflicting,
        vetoed=veto,
        candidate=candidate,
    )


def build_candidates(
    records: list[PreparedRecord],
    config: SimilarityConfig,
    aliases: AliasIndex | None = None,
) -> dict[tuple[str, str], PairScore]:
    """Score every unordered pair once.

    Returns a mapping keyed by the lexicographic, undirected pair id.
    """
    out: dict[tuple[str, str], PairScore] = {}
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            ps = score_pair(records[i], records[j], config, aliases)
            key = tuple(sorted((ps.left, ps.right)))
            out[key] = ps  # type: ignore[index]
    return out
