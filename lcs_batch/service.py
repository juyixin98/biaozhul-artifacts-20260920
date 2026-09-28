"""Orchestration: index building and explainable batch querying.

Every query item produces a structured result with:

* the correlated ``request_id`` and the ``index_version`` it ran against;
* the concrete outcome (candidates with original offsets and per-document
  coverage) or a typed failure (``error.category``);
* ``warnings`` for uncertain/truncated conclusions, kept separate from hard
  failures so reviewers can scan them independently.

Each item is also written to the SQLite audit log (``query_log`` table).
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid

from .config import KERNEL_VERSION, Settings
from .corpus import Document, encode_corpus
from .index_store import IndexStore
from .kernel import SuffixKernel
from .logging_setup import get_logger
from .validation import (
    LcsError,
    validate_batch,
    validate_document_payloads,
    validate_query_spec,
)

_log = get_logger("service")


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:16]}"


class LcsService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = IndexStore(settings.db_path)
        self._kernel: SuffixKernel | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        self._store.close()

    # -- index build ---------------------------------------------------------

    def build_index(self, raw_documents: object, request_id: str | None = None) -> dict:
        request_id = request_id or _new_request_id()
        started = time.perf_counter()
        parsed = validate_document_payloads(raw_documents, self._settings)
        documents = [Document(doc_id=doc_id, content=content) for doc_id, content in parsed]
        corpus = encode_corpus(documents)

        kernel = SuffixKernel(corpus)
        doc_sha256 = {
            doc.doc_id: hashlib.sha256(doc.content).hexdigest() for doc in documents
        }
        corpus_sha256 = hashlib.sha256(
            b"".join(doc.content for doc in documents)
        ).hexdigest()
        with self._lock:
            self._store.save_index(
                corpus, kernel.suffix_array, kernel.lcp_array, corpus_sha256, doc_sha256
            )
            self._kernel = kernel

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        report = {
            "request_id": request_id,
            "index_version": KERNEL_VERSION,
            "doc_count": corpus.doc_count,
            "symbol_count": len(corpus.symbols),
            "corpus_sha256": corpus_sha256,
            "duration_ms": duration_ms,
        }
        _log.info(
            "index built",
            extra={
                "request_id": request_id,
                "index_version": KERNEL_VERSION,
                "duration_ms": duration_ms,
                "detail": f"docs={corpus.doc_count} symbols={len(corpus.symbols)}",
            },
        )
        return report

    # -- batch query -----------------------------------------------------------

    def run_batch(
        self, raw_queries: object, request_id: str | None = None
    ) -> dict:
        request_id = request_id or _new_request_id()
        kernel = self._ensure_kernel()
        raw_specs = validate_batch(raw_queries)

        items = []
        failures = []
        uncertainties = []
        for position, raw in enumerate(raw_specs):
            item = self._run_one(kernel, raw, request_id, position)
            items.append(item)
            if item["status"] == "error":
                failures.append(
                    {"query_id": item["query_id"], **item["error"]}
                )
            uncertainties.extend(
                {"query_id": item["query_id"], "warning": w}
                for w in item["warnings"]
            )

        return {
            "request_id": request_id,
            "index_version": KERNEL_VERSION,
            "item_count": len(items),
            "items": items,
            "failures": failures,
            "uncertainties": uncertainties,
        }

    def _run_one(
        self, kernel: SuffixKernel, raw: object, request_id: str, position: int
    ) -> dict:
        query_id = raw.get("query_id") if isinstance(raw, dict) else None
        started = time.perf_counter()
        try:
            spec = validate_query_spec(raw, kernel.corpus.doc_count, self._settings)
            query_id = spec.query_id
            result = kernel.longest_common_substrings(
                spec.min_docs, spec.max_candidates
            )
            warnings = []
            if result.max_length == 0:
                warnings.append(
                    f"no substring is shared by >= {spec.min_docs} documents"
                )
            if result.truncated:
                warnings.append(
                    f"candidate list truncated to max_candidates="
                    f"{spec.max_candidates}; more ties of the same length exist"
                )
            item = {
                "query_id": spec.query_id,
                "status": "ok",
                "request_id": request_id,
                "index_version": KERNEL_VERSION,
                "params": {
                    "min_docs": spec.min_docs,
                    "max_candidates": spec.max_candidates,
                },
                "result": self._render_result(kernel, result),
                "warnings": warnings,
                "error": None,
            }
            self._store.log_query(
                request_id, spec.query_id, item["params"], "ok",
                {"max_length": result.max_length,
                 "candidate_count": len(result.candidates),
                 "truncated": result.truncated},
                None,
            )
            status, category = "ok", None
        except LcsError as exc:
            item = {
                "query_id": query_id if isinstance(query_id, str) else f"position-{position}",
                "status": "error",
                "request_id": request_id,
                "index_version": KERNEL_VERSION,
                "params": raw if isinstance(raw, dict) else {},
                "result": None,
                "warnings": [],
                "error": exc.to_dict(),
            }
            self._store.log_query(
                request_id, item["query_id"],
                raw if isinstance(raw, dict) else {"raw_type": type(raw).__name__},
                "error", None, exc.to_dict(),
            )
            status, category = "error", exc.category.value

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        _log.info(
            "query executed",
            extra={
                "request_id": request_id,
                "query_id": item["query_id"],
                "index_version": KERNEL_VERSION,
                "duration_ms": duration_ms,
                "status": status,
                "category": category,
            },
        )
        return item

    @staticmethod
    def _render_result(kernel: SuffixKernel, result) -> dict:
        documents = kernel.corpus.documents
        candidates = []
        for candidate in result.candidates:
            per_doc: dict[int, list[int]] = {}
            for occ in candidate.occurrences:
                per_doc.setdefault(occ.doc_index, []).append(occ.offset)
            candidates.append(
                {
                    "substring_hex": candidate.substring.hex(),
                    "length": candidate.length,
                    "doc_coverage": [
                        documents[i].doc_id for i in candidate.doc_coverage
                    ],
                    "doc_coverage_count": len(candidate.doc_coverage),
                    "occurrences": [
                        {"doc_id": documents[i].doc_id, "offsets": offsets}
                        for i, offsets in sorted(per_doc.items())
                    ],
                }
            )
        return {
            "max_length": result.max_length,
            "candidate_count": len(candidates),
            "truncated": result.truncated,
            "candidates": candidates,
            "stats": result.stats,
        }

    # -- state ----------------------------------------------------------------

    def _ensure_kernel(self) -> SuffixKernel:
        with self._lock:
            if self._kernel is None:
                stored = self._store.load_index()
                self._kernel = SuffixKernel(
                    stored.corpus, sa=stored.suffix_array, lcp=stored.lcp_array
                )
            return self._kernel

    def index_info(self) -> dict | None:
        return self._store.index_info()

    def recent_queries(self, limit: int = 20) -> list[dict]:
        return self._store.recent_queries(limit)
