"""Chunked index engine.

Responsibilities:
- build: split text into fixed-size chunks, lex and summarize each chunk;
- edit: splice a replacement into the affected chunk range only, re-lex and
  re-summarize just the new chunks, renumber the untouched tail (offsets are
  derived from chunk lengths, so the tail is never re-scanned);
- query: answer balance / match / shortest-unbalanced-interval by composing
  stored chunk summaries, never by re-scanning the full text.

Versioning: every document carries a version, bumped by each applied edit.
An edit must name the version it was computed against; a mismatch is
rejected as STALE_VERSION so that offsets from an old revision can never
misalign the document.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import accumulate

from ..corpus.spec import DEFAULT_SPEC, LexicalSpec
from ..kernel.lexer import CLOSE, OPEN, Token, lex_with_state
from ..kernel.oracle import (
    CATEGORY_BALANCED,
    CATEGORY_TYPE_MISMATCH,
    CATEGORY_UNMATCHED_CLOSE,
    CATEGORY_UNMATCHED_OPEN,
    Interval,
)
from ..kernel.summary import Summary, compose, summarize
from .errors import (
    DocumentNotFoundError,
    InvalidRangeError,
    NotABracketError,
    StaleVersionError,
)
from .store import ChunkRow, Store

# Match-query outcome categories (BALANCED/mismatch categories come from the
# oracle module so the API has a single vocabulary).
CATEGORY_MATCHED = "MATCHED"


@dataclass(frozen=True)
class EditResult:
    doc_id: int
    version: int
    length: int
    rescanned_chunks: int
    total_chunks: int


@dataclass(frozen=True)
class BalanceResult:
    doc_id: int
    version: int
    balanced: bool
    category: str
    interval: Interval | None
    unmatched_openers: int
    unmatched_closers: int
    mismatches: int


@dataclass(frozen=True)
class MatchResult:
    doc_id: int
    pos: int
    category: str  # MATCHED | UNMATCHED_OPEN | UNMATCHED_CLOSE | TYPE_MISMATCH
    match_pos: int | None


@dataclass(frozen=True)
class _ChunkView:
    """A stored chunk with its absolute start offset resolved."""

    seq: int
    start: int
    text: str
    entry_quote: str | None  # quote state entering the chunk
    escape_pending: bool  # first char is escaped by prior chunk's backslash
    summary: Summary  # absolute positions

    @property
    def end(self) -> int:
        return self.start + len(self.text)


def _absolutize(summary: Summary, base: int) -> Summary:
    def shift(tok: Token) -> Token:
        return Token(kind=tok.kind, btype=tok.btype, pos=tok.pos + base)

    return Summary(
        closers=tuple(shift(t) for t in summary.closers),
        openers=tuple(shift(t) for t in summary.openers),
        mismatches=tuple(
            type(m)(open=shift(m.open), close=shift(m.close))
            for m in summary.mismatches
        ),
    )


# (open quote or None, first-character-escape-pending) — the lexer state
# crossing a chunk boundary.
_LexerState = tuple[str | None, bool]


def _lex_region(
    chunk_texts: list[str],
    spec: LexicalSpec,
    entry_state: _LexerState,
) -> tuple[list[tuple[str, str | None, bool, Summary]], _LexerState]:
    """Lex/summarize chunk texts, threading lexer state across them.

    Returns one (text, entry_quote, escape_pending, summary) tuple per chunk
    plus the exit state. Positions in summaries are chunk-relative.
    """
    built: list[tuple[str, str | None, bool, Summary]] = []
    in_quote, escape_pending = entry_state
    for chunk_text in chunk_texts:
        chunk_entry = (in_quote, escape_pending)
        tokens, in_quote, escape_pending = lex_with_state(
            chunk_text,
            spec,
            base=0,
            in_quote=in_quote,
            escape_pending=escape_pending,
        )
        built.append((chunk_text, chunk_entry[0], chunk_entry[1], summarize(tokens)))
    return built, (in_quote, escape_pending)


class IndexEngine:
    def __init__(
        self,
        store: Store,
        spec: LexicalSpec = DEFAULT_SPEC,
        chunk_size: int = 1024,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._store = store
        self._spec = spec
        self._chunk_size = chunk_size

    # -- build ---------------------------------------------------------------

    def create_document(self, text: str) -> int:
        doc_id = self._store.create_document(length=len(text))
        built, _ = _lex_region(self._split(text), self._spec, (None, False))
        for seq, (chunk_text, entry_quote, escape_pending, summary) in enumerate(built):
            self._store.insert_chunk(
                doc_id, seq, chunk_text, entry_quote, escape_pending, summary
            )
        self._store.commit()
        return doc_id

    def _split(self, text: str) -> list[str]:
        if not text:
            return []
        return [
            text[i : i + self._chunk_size]
            for i in range(0, len(text), self._chunk_size)
        ]

    # -- loading ---------------------------------------------------------------

    def _document_or_raise(self, doc_id: int):
        doc = self._store.get_document(doc_id)
        if doc is None:
            raise DocumentNotFoundError(doc_id)
        return doc

    def length_of(self, doc_id: int) -> int:
        return self._document_or_raise(doc_id).length

    def _load_chunks(self, doc_id: int) -> list[_ChunkView]:
        rows: list[ChunkRow] = self._store.get_chunks(doc_id)
        starts = accumulate((row.length for row in rows), initial=0)
        return [
            _ChunkView(
                seq=row.seq,
                start=start,
                text=row.text,
                entry_quote=row.entry_quote,
                escape_pending=row.escape_pending,
                summary=_absolutize(row.summary, start),
            )
            for row, start in zip(rows, starts)
        ]

    # -- edit ------------------------------------------------------------------

    def apply_edit(
        self,
        doc_id: int,
        expected_version: int,
        start: int,
        end: int,
        replacement: str,
    ) -> EditResult:
        doc = self._document_or_raise(doc_id)
        if expected_version != doc.version:
            raise StaleVersionError(doc_id, expected_version, doc.version)
        if not (0 <= start <= end <= doc.length):
            raise InvalidRangeError(start, end, doc.length)

        chunks = self._load_chunks(doc_id)

        if chunks:
            first, last = self._affected_range(chunks, start, end, doc.length)
            entry_state: _LexerState = (
                chunks[first].entry_quote,
                chunks[first].escape_pending,
            )
            prefix = chunks[first].text[: start - chunks[first].start]
            suffix = chunks[last].text[end - chunks[last].start :]
        else:
            first, last = 0, -1
            entry_state = (None, False)
            prefix = suffix = ""
        new_region = prefix + replacement + suffix

        # Re-lex ONLY the affected region; lexer state is threaded across
        # its new chunks.
        region_chunks, exit_state = _lex_region(
            self._split(new_region), self._spec, entry_state
        )

        # If the region's exit lexer state changed, the tail's stored
        # summaries are stale: re-lex successive untouched chunks, recording
        # each chunk's exact propagated entry state, until the state
        # reconverges with a chunk's stored state. Bracket summaries are
        # independent across chunks after reconvergence, so the rest of the
        # tail is never re-scanned.
        rebuilt_tail: list[tuple[str, str | None, bool, Summary]] = []
        extended_last = last
        cur_quote, cur_pending = exit_state
        cursor = last + 1
        while (
            chunks
            and cursor < len(chunks)
            and (cur_quote, cur_pending)
            != (chunks[cursor].entry_quote, chunks[cursor].escape_pending)
        ):
            tokens, next_quote, next_pending = lex_with_state(
                chunks[cursor].text,
                self._spec,
                base=0,
                in_quote=cur_quote,
                escape_pending=cur_pending,
            )
            rebuilt_tail.append(
                (chunks[cursor].text, cur_quote, cur_pending, summarize(tokens))
            )
            cur_quote, cur_pending = next_quote, next_pending
            extended_last = cursor
            cursor += 1

        new_chunks = region_chunks + rebuilt_tail

        new_version = doc.version + 1
        new_length = doc.length - (end - start) + len(replacement)
        try:
            if chunks:
                replaced_count = extended_last - first + 1
                tail_shift = len(new_chunks) - replaced_count
                self._store.replace_chunk_range(
                    doc_id, first, extended_last, new_chunks, tail_shift
                )
            else:
                for seq, (chunk_text, entry, pending, summary) in enumerate(
                    new_chunks
                ):
                    self._store.insert_chunk(
                        doc_id, seq, chunk_text, entry, pending, summary
                    )
            self._store.update_document_state(doc_id, new_version, new_length)
            self._store.commit()
        except Exception:
            self._store.rollback()
            raise
        return EditResult(
            doc_id=doc_id,
            version=new_version,
            length=new_length,
            rescanned_chunks=len(new_chunks),
            total_chunks=len(chunks) + (len(new_chunks) - (extended_last - first + 1))
            if chunks
            else len(new_chunks),
        )

    @staticmethod
    def _affected_range(
        chunks: list[_ChunkView], start: int, end: int, length: int
    ) -> tuple[int, int]:
        """Indices of the first and last chunk overlapped by [start, end)."""
        if not chunks:
            return (0, -1)
        first = 0
        for i, chunk in enumerate(chunks):
            if chunk.end > start:
                first = i
                break
        else:
            first = len(chunks) - 1  # start == length: edit lands in last chunk
        last = first
        for i in range(first, len(chunks)):
            if chunks[i].start < end:
                last = i
            else:
                break
        return (first, last)

    # -- queries -----------------------------------------------------------------

    def balance(self, doc_id: int) -> BalanceResult:
        doc = self._document_or_raise(doc_id)
        chunks = self._load_chunks(doc_id)
        summary = Summary((), (), ())
        for chunk in chunks:
            summary = compose(summary, chunk.summary)
        interval = self._interval_from_summary(summary)
        return BalanceResult(
            doc_id=doc_id,
            version=doc.version,
            balanced=summary.balanced,
            category=interval.category if interval else CATEGORY_BALANCED,
            interval=interval,
            unmatched_openers=len(summary.openers),
            unmatched_closers=len(summary.closers),
            mismatches=len(summary.mismatches),
        )

    def unbalanced_interval(self, doc_id: int) -> Interval | None:
        return self.balance(doc_id).interval

    @staticmethod
    def _interval_from_summary(summary: Summary) -> Interval | None:
        if summary.mismatches:
            first = min(summary.mismatches, key=lambda m: m.close.pos)
            return Interval(
                start=first.open.pos,
                end=first.close.pos + 1,
                category=CATEGORY_TYPE_MISMATCH,
            )
        if summary.closers:
            pos = summary.closers[0].pos
            return Interval(start=pos, end=pos + 1, category=CATEGORY_UNMATCHED_CLOSE)
        if summary.openers:
            pos = summary.openers[0].pos
            return Interval(start=pos, end=pos + 1, category=CATEGORY_UNMATCHED_OPEN)
        return None

    def match(self, doc_id: int, pos: int) -> MatchResult:
        doc = self._document_or_raise(doc_id)
        if not (0 <= pos < doc.length):
            raise InvalidRangeError(pos, pos + 1, doc.length)
        chunks = self._load_chunks(doc_id)
        home_index = self._chunk_index_at(chunks, pos)
        home = chunks[home_index]
        tokens = self._lex_chunk(home)
        token = next((t for t in tokens if t.pos == pos), None)
        if token is None:
            raise NotABracketError(pos)
        if token.kind == OPEN:
            return self._match_open(doc_id, pos, token, home, chunks, home_index)
        return self._match_close(doc_id, pos, token, home, chunks, home_index)

    def _lex_chunk(self, chunk: _ChunkView) -> list[Token]:
        """Lex a chunk the same way it was indexed: from its stored state."""
        tokens, _, _ = lex_with_state(
            chunk.text,
            self._spec,
            base=chunk.start,
            in_quote=chunk.entry_quote,
            escape_pending=chunk.escape_pending,
        )
        return tokens

    @staticmethod
    def _chunk_index_at(chunks: list[_ChunkView], pos: int) -> int:
        for i, chunk in enumerate(chunks):
            if chunk.start <= pos < chunk.end:
                return i
        raise InvalidRangeError(pos, pos + 1, pos + 1)  # unreachable after checks

    def _match_open(
        self,
        doc_id: int,
        pos: int,
        token: Token,
        home: _ChunkView,
        chunks: list[_ChunkView],
        home_index: int,
    ) -> MatchResult:
        home_tokens = self._lex_chunk(home)
        local = summarize(home_tokens)
        local_pairs = _local_pairs(home_tokens)
        if pos in local_pairs:
            return MatchResult(doc_id, pos, CATEGORY_MATCHED, local_pairs[pos])
        # Ours is a trailing opener of its chunk; find which one.
        trailing = [t for t in local.openers]
        try:
            idx = next(
                i for i, t in enumerate(trailing) if t.pos == pos
            )
        except StopIteration:  # pragma: no cover - defensive
            raise NotABracketError(pos)
        # Simulate the real scan forward. Each later chunk first contributes
        # its leading closers (which cancel against the current stack, with
        # the mismatch-drop policy), then pushes its trailing openers — in a
        # chunk, all leading closers precede all trailing openers, because a
        # surviving opener keeps the local stack non-empty from then on.
        stack = list(trailing[idx:])  # ours at the bottom
        for chunk in chunks[home_index + 1 :]:
            for closer in chunk.summary.closers:
                if stack[-1].btype == closer.btype:
                    popped = stack.pop()
                    if popped.pos == pos:
                        return MatchResult(
                            doc_id, pos, CATEGORY_MATCHED, closer.pos
                        )
                # else: type mismatch — the closer is dropped (policy).
            stack.extend(chunk.summary.openers)
        return MatchResult(doc_id, pos, CATEGORY_UNMATCHED_OPEN, None)

    def _match_close(
        self,
        doc_id: int,
        pos: int,
        token: Token,
        home: _ChunkView,
        chunks: list[_ChunkView],
        home_index: int,
    ) -> MatchResult:
        home_tokens = self._lex_chunk(home)
        local_pairs = _local_pairs(home_tokens)
        if pos in local_pairs:
            return MatchResult(doc_id, pos, CATEGORY_MATCHED, local_pairs[pos])
        # Compose the summary of everything before pos; its trailing opener
        # stack top is exactly what the real scan would see at pos.
        prefix = Summary((), (), ())
        for chunk in chunks[:home_index]:
            prefix = compose(prefix, chunk.summary)
        prefix = compose(
            prefix, summarize([t for t in home_tokens if t.pos < pos])
        )
        if not prefix.openers:
            return MatchResult(doc_id, pos, CATEGORY_UNMATCHED_CLOSE, None)
        top = prefix.openers[-1]
        if top.btype == token.btype:
            return MatchResult(doc_id, pos, CATEGORY_MATCHED, top.pos)
        return MatchResult(doc_id, pos, CATEGORY_TYPE_MISMATCH, top.pos)


def _local_pairs(tokens: list[Token]) -> dict[int, int]:
    """Pairs formed within one chunk's token stream (chunk-local scan)."""
    stack: list[Token] = []
    pairs: dict[int, int] = {}
    for tok in tokens:
        if tok.kind == OPEN:
            stack.append(tok)
        elif tok.kind == CLOSE:
            if stack and stack[-1].btype == tok.btype:
                opener = stack.pop()
                pairs[opener.pos] = tok.pos
                pairs[tok.pos] = opener.pos
            elif stack:
                pass  # mismatch: closer dropped, same policy as summaries
    return pairs
