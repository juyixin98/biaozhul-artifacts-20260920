"""Document service: edit/query facade over the block index.

Responsibilities that live here rather than in routes or the treap:

* offset-version checks -- an edit carries the version its offsets were read
  from; a mismatch is rejected before any block is touched (stale edits cannot
  shift later text by the wrong amount);
* turning the root :class:`Reduction` into a :class:`StructureResult`;
* reporting what an edit invalidated (window, blocks, rescanned characters),
  so diagnostics can show unrelated text was not rescanned.
"""
from __future__ import annotations

from dataclasses import dataclass

from .corpus import BracketLexicon, default_lexicon
from .mining.index import BlockTreap
from .mining.lexer import Lexer
from .mining.scanner import StructureResult, scan_tokens
from .mining.tokens import Reduction
from .storage import Database, DocumentRecord, DocumentRepository

# Failure categories shared with the API layer.
VERSION_CONFLICT = "VERSION_CONFLICT"
NOT_FOUND = "NOT_FOUND"
OFFSET_OUT_OF_RANGE = "OFFSET_OUT_OF_RANGE"
NOT_STRUCTURAL = "NOT_STRUCTURAL"
STALE_CLIENT_VERSION = "STALE_CLIENT_VERSION"


class ServiceError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class EditReport:
    document_id: int
    version: int
    length: int
    window: tuple[int, int]
    blocks_removed: int
    blocks_added: int
    blocks_total: int
    rescanned_chars: int
    mask_blocks_absorbed: int


class DocumentService:
    def __init__(self, db: Database, lexicon: BracketLexicon | None = None,
                 chunk_size: int = 64) -> None:
        self._db = db
        self._repo = DocumentRepository(db)
        self._lexicon = lexicon or default_lexicon()
        self._lexer = Lexer(self._lexicon)
        self._chunk_size = chunk_size
        self._treaps: dict[int, BlockTreap] = {}

    # ------------------------------------------------------------- treap mgmt
    def _treap(self, doc_id: int) -> BlockTreap:
        existing = self._treaps.get(doc_id)
        if existing is not None:
            return existing
        record = self._repo.get(doc_id)
        if record is None:
            raise ServiceError(NOT_FOUND, f"document {doc_id} not found")
        treap = BlockTreap(
            self._db, doc_id, self._lexer, self._chunk_size,
            rng=_stable_rng(doc_id),
        )
        self._treaps[doc_id] = treap
        return treap

    def _require(self, doc_id: int) -> DocumentRecord:
        record = self._repo.get(doc_id)
        if record is None:
            raise ServiceError(NOT_FOUND, f"document {doc_id} not found")
        return record

    @property
    def lexer(self) -> Lexer:
        return self._lexer

    # ------------------------------------------------------------ documents
    def create_document(self, name: str, text: str) -> DocumentRecord:
        if self._repo.find_by_name(name) is not None:
            raise ServiceError("NAME_CONFLICT", f"document {name!r} exists")
        with self._db.transaction():
            doc_id = self._repo.create_document(name, "default")
            treap = BlockTreap(
                self._db, doc_id, self._lexer, self._chunk_size,
                rng=_stable_rng(doc_id),
            )
            if text:
                treap.build(text)
            self._repo.update_length_version(doc_id, treap.length, 1)
            self._treaps[doc_id] = treap
        return self._require(doc_id)

    def get(self, doc_id: int) -> DocumentRecord:
        return self._require(doc_id)

    def list_documents(self) -> list[DocumentRecord]:
        return self._repo.list_documents()

    def text_of(self, doc_id: int) -> str:
        self._require(doc_id)
        return self._treap(doc_id).full_text()

    # ---------------------------------------------------------------- edits
    def edit(
        self,
        doc_id: int,
        start: int,
        end: int,
        replacement: str,
        base_version: int,
    ) -> EditReport:
        record = self._require(doc_id)
        if record.version != base_version:
            raise ServiceError(
                VERSION_CONFLICT,
                f"edit based on version {base_version} but current is "
                f"{record.version}; offsets may point at stale positions",
            )
        treap = self._treap(doc_id)
        if not (0 <= start <= end <= record.length):
            raise ServiceError(
                OFFSET_OUT_OF_RANGE,
                f"range [{start}, {end}) outside document length "
                f"{record.length}",
            )
        before_blocks = treap.block_count()
        with self._db.transaction():
            new_len = treap.edit(start, end, replacement)
            new_version = record.version + 1
            self._repo.update_length_version(doc_id, new_len, new_version)
        after_blocks = treap.block_count()
        stats = treap.last_edit_stats
        return EditReport(
            document_id=doc_id,
            version=new_version,
            length=new_len,
            window=stats["window"],
            blocks_removed=stats["blocks_removed"],
            blocks_added=stats["blocks_added"],
            blocks_total=after_blocks,
            rescanned_chars=stats["rescanned_chars"],
            mask_blocks_absorbed=stats["mask_blocks_absorbed"],
        )

    # --------------------------------------------------------------- queries
    def analyze(self, doc_id: int) -> StructureResult:
        self._require(doc_id)
        return reduction_to_structure(
            self._treap(doc_id).root_reduction(), self._treap(doc_id).length
        )

    def match_at(self, doc_id: int, offset: int) -> dict:
        record = self._require(doc_id)
        treap = self._treap(doc_id)
        if not (0 <= offset < record.length):
            raise ServiceError(
                OFFSET_OUT_OF_RANGE,
                f"offset {offset} outside [0, {record.length})",
            )
        result = reduction_to_structure(treap.root_reduction(), treap.length)
        text = treap.full_text()
        tokens, _ = self._lexer.scan(text)
        structural = {t.offset for t in tokens}
        if offset not in structural:
            # Either ordinary text or a bracket-shaped char masked by a
            # quote/comment: the lexer deliberately made it non-structural.
            raise ServiceError(
                NOT_STRUCTURAL,
                f"offset {offset} is not a structural bracket "
                "(content inside a quote/comment or ordinary text)",
            )
        pair = result.match_for(offset)
        if pair is None:
            defect = result.defect_for(offset)
            return {
                "matched": False,
                "offset": offset,
                "version": record.version,
                "defect": defect.as_state() if defect else None,
            }
        opener, closer = pair
        return {
            "matched": True,
            "offset": offset,
            "partner_offset": closer.offset if opener.offset == offset
            else opener.offset,
            "open_offset": opener.offset,
            "close_offset": closer.offset,
            "type": opener.type,
            "version": record.version,
        }

    def shortest_unbalanced(self, doc_id: int) -> dict:
        record = self._require(doc_id)
        treap = self._treap(doc_id)
        result = reduction_to_structure(treap.root_reduction(), treap.length)
        defect = result.shortest_unbalanced_interval()
        return {
            "balanced": defect is None,
            "version": record.version,
            "length": record.length,
            "defect": defect.as_state() if defect else None,
            "all_defects": [d.as_state() for d in result.defects()],
        }

    def verify_against_full_scan(self, doc_id: int) -> dict:
        """Cross-check the indexed pipeline with the complete stack scan."""
        record = self._require(doc_id)
        treap = self._treap(doc_id)
        text = treap.full_text()
        indexed = reduction_to_structure(treap.root_reduction(), len(text))
        tokens, _ = self._lexer.scan(text)
        full = scan_tokens(tokens, len(text))
        return compare_structures(indexed, full, record.version)


def reduction_to_structure(r: Reduction, length: int) -> StructureResult:
    return StructureResult(
        matches=r.matches,
        mismatches=r.mismatches,
        stray_closes=r.prefix,
        stray_opens=r.suffix,
        length=length,
    )


def compare_structures(indexed: StructureResult, full: StructureResult,
                       version: int) -> dict:
    """Diff two analyses into concrete disagreement lists."""
    def pairs(events):
        return sorted((o.offset, c.offset, o.type) for o, c in events)

    disagreements: list[dict] = []
    if pairs(indexed.matches) != pairs(full.matches):
        disagreements.append({
            "category": "MATCH_SET_MISMATCH",
            "indexed": pairs(indexed.matches),
            "full_scan": pairs(full.matches),
        })
    if pairs(indexed.mismatches) != pairs(full.mismatches):
        disagreements.append({
            "category": "MISMATCH_SET_DIFFERS",
            "indexed": pairs(indexed.mismatches),
            "full_scan": pairs(full.mismatches),
        })
    if [t.offset for t in indexed.stray_closes] != \
            [t.offset for t in full.stray_closes]:
        disagreements.append({"category": "STRAY_CLOSE_SET_DIFFERS"})
    if [t.offset for t in indexed.stray_opens] != \
            [t.offset for t in full.stray_opens]:
        disagreements.append({"category": "STRAY_OPEN_SET_DIFFERS"})
    return {
        "agrees": not disagreements,
        "version": version,
        "disagreements": disagreements,
        "balanced": full.balanced,
        "defect_count": len(full.defects()),
    }


def _stable_rng(doc_id: int):
    import random

    # Deterministic per document so rebuilt fixtures are reproducible.
    return random.Random(0x5EED ^ doc_id)
