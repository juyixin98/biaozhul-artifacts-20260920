"""Corpus/specification layer (lazy exports).

Exports are provided lazily via :pep:`562` so that importing a leaf
module (e.g. :mod:`wfst_service.corpus.symbols` from the kernel) does not
eagerly import :mod:`wfst_service.corpus.spec`, which itself depends on
the kernel -- that would be a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .errors import (
        BudgetExhausted,
        CycleError,
        NotFoundError,
        QueryError,
        SpecError,
        WfstError,
    )
    from .spec import (
        BuiltCorpus,
        CorpusSpec,
        PipelineSpec,
        TransducerSpec,
    )
    from .symbols import EPS

__all__ = [
    "WfstError",
    "SpecError",
    "CycleError",
    "BudgetExhausted",
    "NotFoundError",
    "QueryError",
    "CorpusSpec",
    "PipelineSpec",
    "TransducerSpec",
    "load_corpus_document",
    "parse_corpus",
    "build_corpus",
    "build_explicit_fst",
    "build_lexicon_fst",
    "BuiltCorpus",
    "fst_to_dict",
    "fst_from_dict",
    "EPS",
    "EPS_LITERAL",
    "normalize_label",
]

_LAZY = {
    "WfstError": (".errors", "WfstError"),
    "SpecError": (".errors", "SpecError"),
    "CycleError": (".errors", "CycleError"),
    "BudgetExhausted": (".errors", "BudgetExhausted"),
    "NotFoundError": (".errors", "NotFoundError"),
    "QueryError": (".errors", "QueryError"),
    "CorpusSpec": (".spec", "CorpusSpec"),
    "PipelineSpec": (".spec", "PipelineSpec"),
    "TransducerSpec": (".spec", "TransducerSpec"),
    "load_corpus_document": (".spec", "load_corpus_document"),
    "parse_corpus": (".spec", "parse_corpus"),
    "build_corpus": (".spec", "build_corpus"),
    "build_explicit_fst": (".spec", "build_explicit_fst"),
    "build_lexicon_fst": (".spec", "build_lexicon_fst"),
    "BuiltCorpus": (".spec", "BuiltCorpus"),
    "fst_to_dict": (".serialize", "fst_to_dict"),
    "fst_from_dict": (".serialize", "fst_from_dict"),
    "EPS": (".symbols", "EPS"),
    "EPS_LITERAL": (".symbols", "EPS_LITERAL"),
    "normalize_label": (".symbols", "normalize_label"),
}


def __getattr__(name: str):  # noqa: D401 - PEP 562 hook
    if name in _LAZY:
        from importlib import import_module

        module_name, attr = _LAZY[name]
        value = getattr(import_module(module_name, __name__), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals()) + __all__)
