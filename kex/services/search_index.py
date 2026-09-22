"""Generationed TF-IDF index: incremental maintenance and search.

All maintenance is transactional and idempotent. A query only ever reads the
single ``active`` generation recorded on ``workspace_states``; a rebuild
builds a separate ``building`` generation and the pointer is swapped once
it is complete, so readers can never see a half-new / half-old index.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Document,
    IndexDocumentStat,
    IndexGeneration,
    IndexPosting,
    IndexTerm,
    WorkspaceDocument,
)
from ..tfidf import smooth_idf, sublinear_tf, term_frequencies


class IndexError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class SearchHit:
    workspace_document_id: int
    title: str
    sha256: str
    score: float


def _count_docs(db: Session, generation_id: int) -> int:
    rows = db.execute(
        select(IndexDocumentStat.id).where(
            IndexDocumentStat.index_generation_id == generation_id
        )
    ).all()
    return len(rows)


def _get_term(db: Session, generation_id: int, term: str) -> IndexTerm | None:
    return db.scalar(
        select(IndexTerm).where(
            IndexTerm.index_generation_id == generation_id,
            IndexTerm.term == term,
        )
    )


def index_document(
    db: Session, generation_id: int, ws_doc: WorkspaceDocument, content: str
) -> bool:
    """Add one document to a generation. Returns False if already indexed.

    Adding a document changes N and df of its terms, so IDF and the stored
    weights of previously indexed documents sharing those terms are rescaled
    in the same transaction. The index therefore always reflects the exact
    TF-IDF definition of the module docstring.
    """
    existing = db.scalar(
        select(IndexDocumentStat).where(
            IndexDocumentStat.index_generation_id == generation_id,
            IndexDocumentStat.workspace_document_id == ws_doc.id,
        )
    )
    if existing is not None:
        return False

    tfs: Counter[str] = term_frequencies(content)
    if not tfs:
        # Still record the (empty) doc so corpus statistics stay consistent.
        db.add(
            IndexDocumentStat(
                index_generation_id=generation_id,
                workspace_document_id=ws_doc.id,
                norm_sq=0.0,
            )
        )
        return True

    n_old = _count_docs(db, generation_id)
    n_new = n_old + 1
    new_weights: dict[str, float] = {}

    for term, tf in tfs.items():
        term_row = _get_term(db, generation_id, term)
        df_old = term_row.document_frequency if term_row else 0
        idf_old = smooth_idf(n_old, df_old)
        df_new = df_old + 1
        idf_new = smooth_idf(n_new, df_new)

        # Rescale every prior posting of this term and its doc norms.
        if term_row is not None and idf_old != idf_new:
            prior_postings = db.scalars(
                select(IndexPosting).where(
                    IndexPosting.index_generation_id == generation_id,
                    IndexPosting.index_term_id == term_row.id,
                )
            ).all()
            for p in prior_postings:
                p.tf_idf = sublinear_tf(p.term_frequency) * idf_new
            _rescale_stats(
                db,
                generation_id,
                [p.workspace_document_id for p in prior_postings],
                term_row.id,
                idf_old,
                idf_new,
            )

        if term_row is None:
            term_row = IndexTerm(
                index_generation_id=generation_id,
                term=term,
                document_frequency=df_new,
                idf=idf_new,
            )
            db.add(term_row)
            db.flush()
        else:
            term_row.document_frequency = df_new
            term_row.idf = idf_new

        w = sublinear_tf(tf) * idf_new
        new_weights[term] = w
        db.add(
            IndexPosting(
                index_generation_id=generation_id,
                index_term_id=term_row.id,
                workspace_document_id=ws_doc.id,
                term_frequency=tf,
                tf_idf=w,
            )
        )

    norm_sq = sum(w * w for w in new_weights.values())
    db.add(
        IndexDocumentStat(
            index_generation_id=generation_id,
            workspace_document_id=ws_doc.id,
            norm_sq=norm_sq,
        )
    )
    return True


def _rescale_stats(
    db: Session,
    generation_id: int,
    doc_ids: list[int],
    term_id: int,
    idf_old: float,
    idf_new: float,
) -> None:
    if not doc_ids or idf_old == idf_new:
        return
    postings = db.scalars(
        select(IndexPosting).where(
            IndexPosting.index_generation_id == generation_id,
            IndexPosting.index_term_id == term_id,
            IndexPosting.workspace_document_id.in_(doc_ids),
        )
    ).all()
    by_doc = {p.workspace_document_id: p for p in postings}
    for doc_id in doc_ids:
        p = by_doc.get(doc_id)
        if p is None:
            continue
        stat = db.scalar(
            select(IndexDocumentStat).where(
                IndexDocumentStat.index_generation_id == generation_id,
                IndexDocumentStat.workspace_document_id == doc_id,
            )
        )
        if stat is None:
            continue
        old_w = sublinear_tf(p.term_frequency) * idf_old
        new_w = sublinear_tf(p.term_frequency) * idf_new
        stat.norm_sq = max(0.0, stat.norm_sq - old_w * old_w + new_w * new_w)


def remove_document(db: Session, generation_id: int, ws_doc_id: int) -> bool:
    """Inverse of index_document: drop postings and rescale survivors."""
    stat = db.scalar(
        select(IndexDocumentStat).where(
            IndexDocumentStat.index_generation_id == generation_id,
            IndexDocumentStat.workspace_document_id == ws_doc_id,
        )
    )
    if stat is None:
        return False

    postings = db.scalars(
        select(IndexPosting).where(
            IndexPosting.index_generation_id == generation_id,
            IndexPosting.workspace_document_id == ws_doc_id,
        )
    ).all()
    n_old = _count_docs(db, generation_id)
    n_new = n_old - 1

    for p in postings:
        term_row = db.get(IndexTerm, p.index_term_id)
        df_old = term_row.document_frequency
        idf_old = smooth_idf(n_old, df_old)
        df_new = df_old - 1
        if df_new <= 0:
            # Term vanishes from the generation; all its other postings are
            # gone with this document anyway.
            db.delete(term_row)
            db.delete(p)
            continue
        idf_new = smooth_idf(max(n_new, 1), df_new)
        # Remove this posting, then rescale the remaining ones + norms.
        remaining = db.scalars(
            select(IndexPosting).where(
                IndexPosting.index_generation_id == generation_id,
                IndexPosting.index_term_id == term_row.id,
                IndexPosting.workspace_document_id != ws_doc_id,
            )
        ).all()
        db.delete(p)
        _rescale_stats(
            db,
            generation_id,
            [rp.workspace_document_id for rp in remaining],
            term_row.id,
            idf_old,
            idf_new,
        )
        for rp in remaining:
            rp.tf_idf = sublinear_tf(rp.term_frequency) * idf_new
        term_row.document_frequency = df_new
        term_row.idf = idf_new

    db.delete(stat)
    return True


# --------------------------------------------------------------------------- #
# Queries (read the active generation only)
# --------------------------------------------------------------------------- #
def _query_weights(db: Session, generation_id: int, query: str) -> tuple[dict[str, float], float, dict[str, IndexTerm]]:
    qtf = term_frequencies(query)
    weights: dict[str, float] = {}
    term_rows: dict[str, IndexTerm] = {}
    for term, tf in qtf.items():
        term_row = _get_term(db, generation_id, term)
        if term_row is None:
            # Unknown term has no postings and cannot match anything.
            weights[term] = 0.0
            continue
        w = sublinear_tf(tf) * term_row.idf
        weights[term] = w
        term_rows[term] = term_row
    norm = math.sqrt(sum(w * w for w in weights.values()))
    return weights, norm, term_rows


def search(
    db: Session,
    generation: IndexGeneration,
    query: str,
    *,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[SearchHit], int]:
    if not query.strip():
        return [], 0
    weights, q_norm, term_rows = _query_weights(db, generation.id, query)
    if q_norm == 0.0 or not term_rows:
        return [], 0

    dots: dict[int, float] = defaultdict(float)
    for term, term_row in term_rows.items():
        for p in db.scalars(
            select(IndexPosting).where(
                IndexPosting.index_generation_id == generation.id,
                IndexPosting.index_term_id == term_row.id,
            )
        ):
            dots[p.workspace_document_id] += weights[term] * p.tf_idf

    hits: list[SearchHit] = []
    for ws_doc_id, dot in dots.items():
        stat = db.scalar(
            select(IndexDocumentStat).where(
                IndexDocumentStat.index_generation_id == generation.id,
                IndexDocumentStat.workspace_document_id == ws_doc_id,
            )
        )
        if stat is None or stat.norm_sq <= 0:
            continue
        score = dot / (q_norm * math.sqrt(stat.norm_sq))
        if score <= 0:
            continue
        ws_doc = db.get(WorkspaceDocument, ws_doc_id)
        if ws_doc is None:
            # Document deleted in this transaction's view — skip (no leak).
            continue
        doc = db.get(Document, ws_doc.document_id)
        hits.append(SearchHit(ws_doc_id, ws_doc.title, doc.sha256, score))

    # Deterministic ordering: score desc, then workspace_document_id asc.
    hits.sort(key=lambda h: (-h.score, h.workspace_document_id))
    total = len(hits)
    return hits[offset : offset + limit], total


def keywords(
    db: Session, generation: IndexGeneration, ws_doc_id: int, *, top_k: int = 20
) -> list[dict[str, float | str]]:
    """TF-IDF keywords of one document within the active generation."""
    rows = db.execute(
        select(IndexTerm.term, IndexPosting.tf_idf)
        .join(IndexTerm, IndexTerm.id == IndexPosting.index_term_id)
        .where(
            IndexPosting.index_generation_id == generation.id,
            IndexPosting.workspace_document_id == ws_doc_id,
        )
    ).all()
    out = [{"term": term, "weight": weight} for term, weight in rows]
    # Stable: weight desc, then term asc.
    out.sort(key=lambda r: (-float(r["weight"]), str(r["term"])))
    return out[:top_k]
