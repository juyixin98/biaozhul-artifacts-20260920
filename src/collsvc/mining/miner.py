"""Mining kernel.

Turns a corpus specification into the material an index build and the test
suites need. It does real extraction work rather than hard-coded demos:

* records NFC/NFD normalizations and codepoint inventory per entry;
* groups canonically-equivalent-but-textually-different strings (NFC vs NFD)
  into equivalence classes **without merging their identities**;
* flags exact duplicate texts (same bytes, different doc ids);
* derives categorical probe queries (accent, numeric, case, Turkish,
  canonical-equivalence) used to demonstrate ordering semantics.

The mining kernel is collation-locale agnostic; it only relies on Unicode
normalization (``unicodedata``), keeping it independent from ICU.
"""
from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

from ..corpus.spec import CorpusSpec


@dataclass(frozen=True)
class MinedRecord:
    doc_id: str
    text: str
    nfc: str
    nfd: str
    codepoints: tuple[str, ...]
    has_combining_marks: bool
    nfc_class: str  # NFC text — identity of the canonical-equivalence class


@dataclass(frozen=True)
class EquivalenceClass:
    nfc_text: str
    doc_ids: tuple[str, ...]
    distinct_raw_texts: tuple[str, ...]

    @property
    def identity_preserved(self) -> bool:
        """True when more than one distinct raw spelling exists for one key."""
        return len(self.distinct_raw_texts) > 1


@dataclass(frozen=True)
class Probe:
    category: str
    label: str
    members: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class MiningReport:
    corpus_name: str
    records: tuple[MinedRecord, ...]
    equivalence_classes: tuple[EquivalenceClass, ...]
    duplicate_text_groups: tuple[tuple[str, ...], ...]
    probes: tuple[Probe, ...]
    stats: dict[str, int] = field(default_factory=dict)


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def _codepoints(text: str) -> tuple[str, ...]:
    return tuple(f"U+{ord(ch):04X}:{unicodedata.name(ch, '?')}" for ch in text)


def mine(spec: CorpusSpec) -> MiningReport:
    records: list[MinedRecord] = []
    class_members: dict[str, list[MinedRecord]] = defaultdict(list)
    raw_by_text: dict[str, list[str]] = defaultdict(list)

    for entry in spec.entries:
        nfc_text = nfc(entry.text)
        rec = MinedRecord(
            doc_id=entry.doc_id,
            text=entry.text,
            nfc=nfc_text,
            nfd=nfd(entry.text),
            codepoints=_codepoints(entry.text),
            has_combining_marks=any(
                unicodedata.combining(ch) for ch in entry.text
            ),
            nfc_class=nfc_text,
        )
        records.append(rec)
        class_members[nfc_text].append(rec)
        raw_by_text[entry.text].append(entry.doc_id)

    classes = tuple(
        EquivalenceClass(
            nfc_text=key,
            doc_ids=tuple(r.doc_id for r in members),
            distinct_raw_texts=tuple(sorted({r.text for r in members})),
        )
        for key, members in sorted(class_members.items())
    )
    dup_groups = tuple(
        tuple(ids) for ids in raw_by_text.values() if len(ids) > 1
    )

    probes = _derive_probes(records)
    stats = {
        "entry_count": len(records),
        "equivalence_class_count": len(classes),
        "multi_spelling_class_count": sum(
            1 for c in classes if c.identity_preserved
        ),
        "duplicate_text_group_count": len(dup_groups),
        "combining_mark_entry_count": sum(
            1 for r in records if r.has_combining_marks
        ),
    }
    return MiningReport(
        corpus_name=spec.name,
        records=tuple(records),
        equivalence_classes=classes,
        duplicate_text_groups=dup_groups,
        probes=probes,
        stats=stats,
    )


def _derive_probes(records: list[MinedRecord]) -> tuple[Probe, ...]:
    probes: list[Probe] = []
    by_nfc: dict[str, list[MinedRecord]] = defaultdict(list)
    for r in records:
        by_nfc[r.nfc].append(r)

    # Canonical-equivalence probe: classes with distinct raw spellings.
    for nfc_text, members in sorted(by_nfc.items()):
        spellings = tuple(sorted({r.text for r in members}))
        if len(spellings) > 1:
            probes.append(
                Probe(
                    category="canonical_equivalence",
                    label=nfc_text,
                    members=spellings,
                    note="distinct raw strings must share a sort key but "
                    "keep separate doc identities",
                )
            )

    text_by_nfc = {r.nfc: r.text for r in records}

    def _members_for(nfcs: list[str]) -> tuple[str, ...]:
        return tuple(text_by_nfc[n] for n in nfcs if n in text_by_nfc)

    accent_nfcs = [n for n in text_by_nfc if _is_accent_probe(n)]
    if accent_nfcs:
        probes.append(
            Probe(
                category="accent",
                label="cote family",
                members=_members_for(sorted(accent_nfcs)),
                note="secondary strength orders accents; primary ignores them",
            )
        )

    numeric_nfcs = [n for n in text_by_nfc if _has_digit_run(n)]
    if numeric_nfcs:
        probes.append(
            Probe(
                category="numeric",
                label="digit runs",
                members=_members_for(sorted(numeric_nfcs)),
                note="numeric collation orders 2 < 10; plain collation 10 < 2",
            )
        )

    case_nfcs = [n for n in text_by_nfc if any(c.isalpha() for c in n)]
    if case_nfcs:
        probes.append(
            Probe(
                category="case",
                label="mixed case present",
                members=_members_for(sorted(case_nfcs))[:8],
                note="tertiary strength distinguishes case; case-first "
                "changes the tie order",
            )
        )

    turkish_nfcs = [n for n in text_by_nfc if _has_turkish_letters(n)]
    if turkish_nfcs:
        probes.append(
            Probe(
                category="turkish",
                label="dotless/dotted I and Turkish letters",
                members=_members_for(sorted(turkish_nfcs)),
                note="tr_TR tailoring separates ı/i and I/İ",
            )
        )

    return tuple(probes)


def _is_accent_probe(text: str) -> bool:
    return any(unicodedata.combining(ch) for ch in nfd(text)) and all(
        not ch.isdigit() for ch in text
    )


def _has_digit_run(text: str) -> bool:
    run = 0
    for ch in text:
        if ch.isdigit():
            run += 1
            if run >= 1:
                return True
        else:
            run = 0
    return False


def _has_turkish_letters(text: str) -> bool:
    return bool(set(text) & set("ıİşŞğĞüÜöÖçÇ"))
