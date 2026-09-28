"""Query validation.

This module is the accept/reject/undetermined gate in front of the service:

* **accept**   -- a well-formed request whose result is fully determined;
* **reject**   -- malformed range, stale version, non-structural offset;
* **undetermined** -- the index cannot answer without ambiguity (e.g. the
  document ends inside an unterminated quote, so matching near the tail is
  defined but the document itself is lexically incomplete).

Each verdict carries the request id and the state that explains it. Ad-hoc
stateless text is analyzed directly through lexer + full stack scan, which
also serves the cross-validation endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .diagnostics import (
    ACCEPTED,
    REJECTED,
    UNDETERMINED,
    DiagnosticLog,
    redact_snippet,
)
from .mining.lexer import Lexer
from .mining.scanner import StructureResult, scan_tokens
from .service import (
    NOT_STRUCTURAL,
    OFFSET_OUT_OF_RANGE,
    VERSION_CONFLICT,
    DocumentService,
    ServiceError,
)


@dataclass(frozen=True)
class Verdict:
    outcome: str
    reason: str
    request_id: str
    state: dict[str, Any]


def _state(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}


class QueryValidator:
    def __init__(self, service: DocumentService, diagnostics: DiagnosticLog,
                 lexer: Lexer) -> None:
        self._service = service
        self._diag = diagnostics
        self._lexer = lexer

    def validate_edit(
        self, doc_id: int, start: int, end: int, replacement: str,
        base_version: int, request_id: str,
    ) -> Verdict:
        if start > end:
            rec = self._diag.record(
                "edit", REJECTED, "start_after_end",
                _state(start=start, end=end, base_version=base_version),
                request_id,
            )
            return Verdict(REJECTED, rec.reason, request_id, rec.state)
        try:
            record = self._service.get(doc_id)
        except ServiceError as exc:
            rec = self._diag.record(
                "edit", REJECTED, exc.category,
                _state(doc_id=doc_id), request_id,
            )
            return Verdict(REJECTED, exc.category, request_id, rec.state)
        if record.version != base_version:
            rec = self._diag.record(
                "edit", REJECTED, VERSION_CONFLICT,
                _state(
                    doc_id=doc_id,
                    base_version=base_version,
                    current_version=record.version,
                ),
                request_id,
            )
            return Verdict(REJECTED, VERSION_CONFLICT, request_id, rec.state)
        if not (0 <= start <= end <= record.length):
            rec = self._diag.record(
                "edit", REJECTED, OFFSET_OUT_OF_RANGE,
                _state(start=start, end=end, length=record.length),
                request_id,
            )
            return Verdict(REJECTED, OFFSET_OUT_OF_RANGE, request_id, rec.state)
        rec = self._diag.record(
            "edit", ACCEPTED, "range_and_version_current",
            _state(
                doc_id=doc_id,
                version=record.version,
                start=start,
                end=end,
                replacement_len=len(replacement),
                length=record.length,
            ),
            request_id,
        )
        return Verdict(ACCEPTED, rec.reason, request_id, rec.state)

    def validate_match(
        self, doc_id: int, offset: int, request_id: str
    ) -> tuple[Verdict, dict | None]:
        """Validate then execute a match query. Returns (verdict, payload)."""
        try:
            record = self._service.get(doc_id)
        except ServiceError as exc:
            rec = self._diag.record(
                "match", REJECTED, exc.category,
                _state(doc_id=doc_id, offset=offset), request_id,
            )
            return Verdict(REJECTED, exc.category, request_id, rec.state), None
        if not (0 <= offset < record.length):
            rec = self._diag.record(
                "match", REJECTED, OFFSET_OUT_OF_RANGE,
                _state(offset=offset, length=record.length), request_id,
            )
            return (
                Verdict(REJECTED, OFFSET_OUT_OF_RANGE, request_id, rec.state),
                None,
            )
        text = self._service.text_of(doc_id)
        tokens, _ = self._lexer.scan(text)
        structural = {t.offset for t in tokens}
        if offset not in structural:
            # A bracket-shaped character at this offset was masked (inside a
            # quote/comment) or the offset is ordinary content.
            rec = self._diag.record(
                "match", REJECTED, NOT_STRUCTURAL,
                _state(
                    offset=offset,
                    char_is_bracket=text[offset]
                    in (self._lexer.lexicon.open_map()
                        | self._lexer.lexicon.close_map()),
                    context=redact_snippet(text, offset),
                    version=record.version,
                ),
                request_id,
            )
            return (
                Verdict(REJECTED, NOT_STRUCTURAL, request_id, rec.state),
                None,
            )
        payload = self._service.match_at(doc_id, offset)
        _, spans = self._lexer.scan(text)
        unterminated = any(not s.terminated for s in spans)
        # An unterminated tail makes the document lexically incomplete; the
        # computed answer is returned but the verdict stays undetermined.
        outcome = UNDETERMINED if unterminated else ACCEPTED
        reason = (
            "document_ends_inside_unterminated_span"
            if unterminated else "match_computed"
        )
        rec = self._diag.record(
            "match", outcome, reason,
            _state(
                doc_id=doc_id,
                offset=offset,
                version=record.version,
                matched=payload["matched"],
                partner=payload.get("partner_offset"),
            ),
            request_id,
        )
        return Verdict(outcome, reason, request_id, rec.state), payload

    def analyze_text(self, content: str, offset: int | None,
                     request_id: str) -> tuple[Verdict, StructureResult, dict | None]:
        """Stateless analysis path (full scan), optionally a match payload."""
        tokens, spans = self._lexer.scan(content)
        result = scan_tokens(tokens, len(content))
        unterminated = any(not s.terminated for s in spans)
        structural = {t.offset for t in tokens}
        match_payload = None
        outcome = ACCEPTED
        reason = "full_scan_complete"
        if offset is not None:
            if not (0 <= offset < len(content)):
                rec = self._diag.record(
                    "analyze", REJECTED, OFFSET_OUT_OF_RANGE,
                    _state(offset=offset, length=len(content)), request_id,
                )
                return (
                    Verdict(REJECTED, OFFSET_OUT_OF_RANGE, request_id, rec.state),
                    result,
                    None,
                )
            if offset not in structural:
                rec = self._diag.record(
                    "analyze", REJECTED, NOT_STRUCTURAL,
                    _state(offset=offset,
                           char_is_bracket=content[offset]
                           in (self._lexer.lexicon.open_map()
                               | self._lexer.lexicon.close_map()),
                           context=redact_snippet(content, offset)),
                    request_id,
                )
                return (
                    Verdict(REJECTED, NOT_STRUCTURAL, request_id, rec.state),
                    result,
                    None,
                )
            pair = result.match_for(offset)
            defect = result.defect_for(offset)
            if pair is None:
                match_payload = {
                    "matched": False,
                    "offset": offset,
                    "partner_offset": None,
                    "open_offset": None,
                    "close_offset": None,
                    "type": None,
                    "version": 0,
                    "defect": defect.as_state() if defect else None,
                }
            else:
                opener, closer = pair
                match_payload = {
                    "matched": True,
                    "offset": offset,
                    "partner_offset": closer.offset
                    if opener.offset == offset else opener.offset,
                    "open_offset": opener.offset,
                    "close_offset": closer.offset,
                    "type": opener.type,
                    "version": 0,
                    "defect": None,
                }
        if unterminated:
            outcome = UNDETERMINED
            reason = "document_ends_inside_unterminated_span"
        rec = self._diag.record(
            "analyze", outcome, reason,
            _state(
                length=len(content),
                tokens=len(tokens),
                defects=len(result.defects()),
                offset=offset,
            ),
            request_id,
        )
        verdict = Verdict(outcome, reason, request_id, rec.state)
        return verdict, result, match_payload
