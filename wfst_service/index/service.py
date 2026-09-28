"""Corpus loading, validation and model materialisation service.

Loading pipeline (all failures are raised as structured service errors,
never silently downgraded):

1. parse + validate the corpus JSON document (:mod:`wfst_service.corpus`);
2. expand every transducer (lexicon entries included) to an explicit FST;
3. run cycle analysis on every FST -- reject pure epsilon cycles and
   negative-cost cycles with a witness;
4. precompose every declared pipeline in declared order, cycle-checking
   the result;
5. persist specification, transducers and composed models to SQLite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..corpus.errors import CycleError, SpecError
from ..corpus.spec import (
    BuiltCorpus,
    build_corpus,
    load_corpus_document,
)
from ..core.compose import compose
from ..core.cycles import analyze_cycles
from ..core.fst import Fst
from .repository import IndexRepository

COMPOSED_PREFIX = "pipeline:"


@dataclass(frozen=True, slots=True)
class LoadedModel:
    name: str
    kind: str
    num_states: int
    num_arcs: int


@dataclass(frozen=True, slots=True)
class LoadReport:
    corpus_id: str
    models: tuple[LoadedModel, ...]
    pipelines: tuple[str, ...]
    composition_logs: tuple[str, ...] = field(default_factory=tuple)

    def as_lines(self) -> list[str]:
        lines = [f"loaded corpus {self.corpus_id!r}:"]
        lines.extend(
            f"  model {m.name} [{m.kind}] states={m.num_states} arcs={m.num_arcs}"
            for m in self.models
        )
        lines.extend(f"  pipeline {p}" for p in self.pipelines)
        lines.extend(f"  {line}" for line in self.composition_logs)
        return lines


class IndexService:
    def __init__(self, repository: IndexRepository) -> None:
        self._repo = repository

    @property
    def repository(self) -> IndexRepository:
        return self._repo

    def load_corpus_file(
        self, corpus_id: str, path: str | Path, *, description: str = ""
    ) -> LoadReport:
        spec = load_corpus_document(path)
        desc = description or str(spec.meta.get("description", ""))
        return self._load(corpus_id, spec, desc)

    def load_corpus_json(
        self, corpus_id: str, document: dict, *, description: str = ""
    ) -> LoadReport:
        from ..corpus.spec import parse_corpus

        spec = parse_corpus(document)
        desc = description or str(spec.meta.get("description", ""))
        return self._load(corpus_id, spec, desc)

    def _load(self, corpus_id: str, spec, description: str) -> LoadReport:
        built: BuiltCorpus = build_corpus(spec)

        # ---- Validate everything before touching persistence ----------
        base_models: list[tuple[str, Fst]] = list(built.fsts.items())
        for name, fst in base_models:
            self._require_acyclic(fst, where=f"transducer {name!r}")

        composed_models: list[tuple[str, str, tuple[str, ...], Fst, list[str]]] = []
        composition_logs: list[str] = []
        for pipe_name, sequence in built.pipelines.items():
            composed = built.fsts[sequence[0]]
            for next_name in sequence[1:]:
                composed, trace = compose(
                    composed,
                    built.fsts[next_name],
                    name=f"{COMPOSED_PREFIX}{pipe_name}",
                )
                composition_logs.extend(trace.as_lines())
            self._require_acyclic(
                composed, where=f"composed pipeline {pipe_name!r}"
            )
            composed_models.append(
                (
                    pipe_name,
                    f"{COMPOSED_PREFIX}{pipe_name}",
                    sequence,
                    composed,
                    composition_logs,
                )
            )

        # ---- All valid: replace corpus row, then materialise ----------
        self._repo.upsert_corpus(
            corpus_id,
            description,
            json.dumps(
                {"version": spec.version, "meta": spec.meta},
                ensure_ascii=False,
            ),
        )

        models: list[LoadedModel] = []
        for name, fst in base_models:
            models.append(
                LoadedModel(name, "fst", fst.num_states, len(fst.arcs))
            )
            self._repo.put_transducer(corpus_id, name, "fst", fst)

        pipelines: list[str] = []
        for pipe_name, composed_name, sequence, composed, _logs in composed_models:
            self._repo.put_transducer(
                corpus_id, composed_name, "composed", composed
            )
            self._repo.put_pipeline(
                corpus_id, pipe_name, sequence, composed_name
            )
            pipelines.append(pipe_name)
            models.append(
                LoadedModel(
                    composed_name,
                    "composed",
                    composed.num_states,
                    len(composed.arcs),
                )
            )

        return LoadReport(
            corpus_id=corpus_id,
            models=tuple(models),
            pipelines=tuple(pipelines),
            composition_logs=tuple(composition_logs),
        )

    @staticmethod
    def _require_acyclic(fst: Fst, *, where: str) -> None:
        report = analyze_cycles(fst)
        if report.has_epsilon_cycle:
            raise CycleError(
                f"{where}: forbidden pure epsilon cycle through states "
                f"{report.epsilon_witness} (consumes/emits nothing; "
                "shortest paths are ill-defined)",
                states=report.epsilon_witness,
            )
        if report.has_negative_cycle:
            raise CycleError(
                f"{where}: forbidden negative-cost cycle through states "
                f"{report.negative_witness} with total cost "
                f"{report.negative_cycle_cost:g}",
                states=report.negative_witness,
            )

    def resolve_model(self, corpus_id: str, target: str) -> tuple[Fst, str]:
        """Resolve a pipeline name (preferred) or transducer name.

        Returns ``(fst, kind)``.  Raises :class:`NotFoundError` for
        unknown targets or corpora.
        """
        from ..corpus.errors import NotFoundError

        if self._repo.get_corpus(corpus_id) is None:
            raise NotFoundError(f"unknown corpus {corpus_id!r}")

        pipeline = self._repo.get_pipeline(corpus_id, target)
        if pipeline is not None:
            fst = self._repo.get_transducer(
                corpus_id, pipeline["composed_name"]
            )
            if fst is None:  # pragma: no cover - invariant
                raise NotFoundError(
                    f"composed model for pipeline {target!r} is missing"
                )
            return fst, "pipeline"

        fst = self._repo.get_transducer(corpus_id, target)
        if fst is not None:
            return fst, "transducer"
        raise NotFoundError(
            f"corpus {corpus_id!r} has no transducer or pipeline named "
            f"{target!r}"
        )
