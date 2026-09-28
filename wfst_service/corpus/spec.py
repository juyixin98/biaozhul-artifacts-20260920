"""Corpus JSON schema, validation and transducer construction.

Document shape (see ``fixtures/corpora/*.json``)::

    {
      "version": 1,
      "meta": {"description": "..."},
      "transducers": [
        {"name": "spell", "kind": "fst",
         "start": 0, "finals": [{"state": 2, "cost": 0.0}],
         "arcs": [{"src": 0, "dst": 1, "in": "a", "out": "b", "cost": 1.0}]},
        {"name": "lexicon", "kind": "lexicon",
         "identity_alphabet": "abc",   // optional
         "entries": [{"input": "cat", "outputs": [["cat", 0.0], ["kat", 0.5]]}]}
      ],
      "pipelines": [
        {"name": "spell_then_morph", "sequence": ["spell", "morph"]}
      ]
    }

Validation is explicit (no schema library): every error is a
:class:`~wfst_service.corpus.errors.SpecError` whose ``path`` locates the
offending JSON position.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.fst import Arc, Fst
from ..corpus.errors import SpecError
from ..corpus.symbols import EPS, EPS_LITERAL, normalize_label

SPEC_VERSION = 1


@dataclass(frozen=True, slots=True)
class TransducerSpec:
    name: str
    kind: str
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PipelineSpec:
    name: str
    sequence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorpusSpec:
    version: int
    meta: dict[str, Any]
    transducers: tuple[TransducerSpec, ...]
    pipelines: tuple[PipelineSpec, ...]


@dataclass(frozen=True, slots=True)
class BuiltCorpus:
    spec: CorpusSpec
    fsts: dict[str, Fst]
    pipelines: dict[str, tuple[str, ...]]


# --------------------------------------------------------------------------
# Loading / validation
# --------------------------------------------------------------------------


def load_corpus_document(path: str | Path) -> CorpusSpec:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read corpus file {path}: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecError(
            f"corpus file {path.name} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, col {exc.colno})",
            path=str(path),
        ) from exc
    return parse_corpus(doc, source=path.name)


def parse_corpus(doc: Any, *, source: str = "<document>") -> CorpusSpec:
    if not isinstance(doc, dict):
        raise SpecError("corpus root must be an object", path="$")
    version = doc.get("version", SPEC_VERSION)
    if version != SPEC_VERSION:
        raise SpecError(
            f"unsupported corpus version {version!r}; expected {SPEC_VERSION}",
            path="$.version",
        )

    meta = doc.get("meta", {})
    if not isinstance(meta, dict):
        raise SpecError("meta must be an object", path="$.meta")

    raw_tds = doc.get("transducers", [])
    if not isinstance(raw_tds, list) or not raw_tds:
        raise SpecError(
            "transducers must be a non-empty array", path="$.transducers"
        )

    tds: list[TransducerSpec] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(raw_tds):
        base = f"$.transducers[{idx}]"
        if not isinstance(item, dict):
            raise SpecError("transducer entry must be an object", path=base)
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise SpecError("transducer name must be a non-empty string",
                            path=f"{base}.name")
        if name in seen_names:
            raise SpecError(f"duplicate transducer name {name!r}",
                            path=f"{base}.name")
        seen_names.add(name)
        kind = item.get("kind")
        if kind not in ("fst", "lexicon"):
            raise SpecError(
                f"transducer {name!r}: kind must be 'fst' or 'lexicon', "
                f"got {kind!r}",
                path=f"{base}.kind",
            )
        if kind == "fst":
            _validate_fst_body(item, f"{base}")
        else:
            _validate_lexicon_body(item, f"{base}")
        tds.append(TransducerSpec(name=name, kind=kind, raw=item))

    raw_pipes = doc.get("pipelines", [])
    if not isinstance(raw_pipes, list):
        raise SpecError("pipelines must be an array", path="$.pipelines")
    pipes: list[PipelineSpec] = []
    seen_pipe_names: set[str] = set()
    for idx, item in enumerate(raw_pipes):
        base = f"$.pipelines[{idx}]"
        if not isinstance(item, dict):
            raise SpecError("pipeline entry must be an object", path=base)
        pname = item.get("name")
        if not isinstance(pname, str) or not pname:
            raise SpecError("pipeline name must be a non-empty string",
                            path=f"{base}.name")
        if pname in seen_pipe_names:
            raise SpecError(f"duplicate pipeline name {pname!r}",
                            path=f"{base}.name")
        seen_pipe_names.add(pname)
        seq = item.get("sequence")
        if not isinstance(seq, list) or len(seq) < 2:
            raise SpecError(
                "pipeline sequence must be an array of at least 2 names",
                path=f"{base}.sequence",
            )
        names: list[str] = []
        for j, ref in enumerate(seq):
            if not isinstance(ref, str) or not ref:
                raise SpecError(
                    "pipeline steps must be non-empty strings",
                    path=f"{base}.sequence[{j}]",
                )
            if ref not in seen_names:
                raise SpecError(
                    f"pipeline {pname!r} references unknown transducer "
                    f"{ref!r}",
                    path=f"{base}.sequence[{j}]",
                )
            names.append(ref)
        pipes.append(PipelineSpec(name=pname, sequence=tuple(names)))

    return CorpusSpec(
        version=version,
        meta=dict(meta),
        transducers=tuple(tds),
        pipelines=tuple(pipes),
    )


def _validate_fst_body(item: dict[str, Any], base: str) -> None:
    if not isinstance(item.get("start"), int) or item["start"] < 0:
        raise SpecError("fst start must be a non-negative integer",
                        path=f"{base}.start")
    finals = item.get("finals")
    if not isinstance(finals, list) or not finals:
        raise SpecError("fst finals must be a non-empty array",
                        path=f"{base}.finals")
    for j, fin in enumerate(finals):
        fb = f"{base}.finals[{j}]"
        if not isinstance(fin, dict) or not isinstance(fin.get("state"), int):
            raise SpecError("final entry needs integer 'state'", path=fb)
        _cost(fin.get("cost", 0.0), f"{fb}.cost")
    arcs = item.get("arcs", [])
    if not isinstance(arcs, list):
        raise SpecError("fst arcs must be an array", path=f"{base}.arcs")
    for j, arc in enumerate(arcs):
        ab = f"{base}.arcs[{j}]"
        for key in ("src", "dst"):
            if not isinstance(arc.get(key), int) or arc[key] < 0:
                raise SpecError(f"arc {key} must be a non-negative integer",
                                path=f"{ab}.{key}")
        for key in ("in", "out"):
            if key not in arc:
                raise SpecError(f"arc missing {key!r}", path=ab)
            try:
                normalize_label(arc[key])
            except (TypeError, ValueError) as exc:
                raise SpecError(str(exc), path=f"{ab}.{key}") from exc
        _cost(arc.get("cost", 0.0), f"{ab}.cost")


def _validate_lexicon_body(item: dict[str, Any], base: str) -> None:
    alphabet = item.get("identity_alphabet")
    if alphabet is not None and not isinstance(alphabet, str):
        raise SpecError("identity_alphabet must be a string of characters",
                        path=f"{base}.identity_alphabet")
    entries = item.get("entries")
    if not isinstance(entries, list) or not entries:
        raise SpecError("lexicon entries must be a non-empty array",
                        path=f"{base}.entries")
    for j, entry in enumerate(entries):
        eb = f"{base}.entries[{j}]"
        if not isinstance(entry, dict):
            raise SpecError("lexicon entry must be an object", path=eb)
        inp = entry.get("input")
        if not isinstance(inp, str) or not inp:
            raise SpecError("lexicon entry input must be a non-empty string",
                            path=f"{eb}.input")
        outputs = entry.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            raise SpecError(
                "lexicon entry outputs must be a non-empty array of "
                "[output, cost] pairs",
                path=f"{eb}.outputs",
            )
        for k, pair in enumerate(outputs):
            pb = f"{eb}.outputs[{k}]"
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
            ):
                raise SpecError("output must be a [string, cost] pair",
                                path=pb)
            _cost(pair[1], f"{pb}[1]")


def _cost(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"cost must be a number, got {value!r}", path=path)
    fvalue = float(value)
    if fvalue != fvalue or fvalue in (float("inf"), float("-inf")):
        raise SpecError("cost must be finite", path=path)
    return fvalue


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def build_corpus(spec: CorpusSpec) -> BuiltCorpus:
    fsts: dict[str, Fst] = {}
    for td in spec.transducers:
        if td.kind == "fst":
            fsts[td.name] = build_explicit_fst(td)
        else:
            fsts[td.name] = build_lexicon_fst(td)
    pipelines = {p.name: p.sequence for p in spec.pipelines}
    return BuiltCorpus(spec=spec, fsts=fsts, pipelines=pipelines)


def build_explicit_fst(td: TransducerSpec) -> Fst:
    raw = td.raw
    state_ids: set[int] = {raw["start"]}
    arcs: list[Arc] = []
    for a in raw.get("arcs", []):
        src, dst = a["src"], a["dst"]
        state_ids.update((src, dst))
        arcs.append(
            Arc(
                src=src,
                dst=dst,
                ilabel=normalize_label(a["in"]),
                olabel=normalize_label(a["out"]),
                cost=_cost(a.get("cost", 0.0), "$"),
            )
        )
    finals = {f["state"]: _cost(f.get("cost", 0.0), "$") for f in raw["finals"]}
    state_ids.update(finals)
    used = sorted(state_ids)
    renumber = {old: new for new, old in enumerate(used)}
    arcs = [
        Arc(renumber[a.src], renumber[a.dst], a.ilabel, a.olabel, a.cost)
        for a in arcs
    ]
    return Fst.create(
        td.name,
        len(used),
        renumber[raw["start"]],
        {renumber[s]: w for s, w in finals.items()},
        arcs,
    )


def build_lexicon_fst(td: TransducerSpec) -> Fst:
    """Expand dictionary entries into a trie-backed transducer.

    Two clearly separated phases per entry:

    1. **Consume** the input word on a shared prefix trie whose arcs are
       ``(ch, EPS, 0)`` (input tape moves, output tape waits).
    2. **Emit** each listed output string from the word's terminal trie
       node on a linear chain of ``(EPS, ch, w)`` arcs; the entry cost is
       attached to the first emission arc and the chain ends in a final
       state.  Multiple outputs of one entry branch at the same node,
       modelling lexical ambiguity.

    Emission happens one character per arc, so the transducer composes
    naturally with character-level rule transducers.

    When ``identity_alphabet`` is supplied, the start state also loops on
    each alphabet character as ``(c, c, 0)`` and is final at cost 0, so
    characters not covered by an entry pass through unchanged.
    """
    raw = td.raw
    next_state = 1

    def new_state() -> int:
        nonlocal next_state
        sid = next_state
        next_state += 1
        return sid

    children: dict[int, dict[str, int]] = {0: {}}
    # (src, dst, ilabel, olabel) -> minimum cost
    arc_costs: dict[tuple[int, int, str, str], float] = {}
    finals: dict[int, float] = {}

    def add_arc(src: int, dst: int, ilab: str, olab: str, cost: float) -> None:
        key = (src, dst, ilab, olab)
        prev = arc_costs.get(key)
        arc_costs[key] = cost if prev is None else min(prev, cost)

    for entry in raw["entries"]:
        word: str = entry["input"]
        node = 0
        # Phase 1: shared trie consuming the whole input word.
        for ch in word:
            nxt = children[node].get(ch)
            if nxt is None:
                nxt = new_state()
                children.setdefault(nxt, {})
                children[node][ch] = nxt
                add_arc(node, nxt, ch, EPS, 0.0)
            node = nxt

        # Phase 2: one emission chain per listed output.  The entry cost
        # rides on the first emission arc; each arc emits exactly one
        # output character, preserving the single-symbol invariant.
        for output, cost_value in entry["outputs"]:
            cost = _cost(cost_value, "$")
            cur = node
            if output == "":
                # Empty output: terminal trie node itself is final.
                prev = finals.get(cur)
                finals[cur] = cost if prev is None else min(prev, cost)
                continue
            for j, ch in enumerate(output):
                label = normalize_label(ch)
                nxt = new_state()
                add_arc(cur, nxt, EPS, label, cost if j == 0 else 0.0)
                cur = nxt
            finals[cur] = 0.0

    alphabet = raw.get("identity_alphabet")
    if alphabet is not None:
        for ch in sorted(set(alphabet)):
            label = normalize_label(ch)
            add_arc(0, 0, label, label, 0.0)
        finals[0] = 0.0

    arcs = tuple(
        Arc(src, dst, ilab, olab, cost)
        for (src, dst, ilab, olab), cost in sorted(arc_costs.items())
    )
    return Fst.create(td.name, next_state, 0, finals, arcs)
