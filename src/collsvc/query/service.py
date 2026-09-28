"""Query service: sorted listing, value ranges, prefix retrieval.

All read paths:

1. verify the open index matches the options-derived version (refusing to mix
   keys from an older build);
2. order and bound using **ICU sort-key BLOBs**, never UTF-8 byte order;
3. accept/issue only cursors stamped with the current index version and the
   matching query mode;
4. record explainable steps and, for prefix queries, explicitly flag when a
   full scan was required (numeric collation / IDENTICAL strength).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..collation.versioning import InvalidCursor, VersionMismatchError
from ..index.cursor import (
    MODE_LIST,
    MODE_PREFIX,
    MODE_RANGE,
    Cursor,
)
from ..index.store import IndexStore, seek_supported
from ..telemetry import Trace
from .keys import bounded_interval

PREFIX_COLLATION = "collation"
PREFIX_TEXT = "text"


@dataclass(frozen=True)
class Page:
    rows: list[sqlite3.Row]
    next_cursor: str | None
    degraded: bool
    scanned: int
    matched: int


class QueryService:
    def __init__(self, store: IndexStore) -> None:
        self._store = store
        self._version = store.require_open()["index_version"]

    @property
    def index_version(self) -> str:
        return self._version

    # ---------- helpers ----------

    def _check_cursor(self, token: str | None, mode: str) -> tuple[bytes | None, int]:
        if token is None:
            return None, 0
        cursor = Cursor.decode(token)
        if cursor.index_version != self._version:
            raise VersionMismatchError(
                f"cursor was issued for index {cursor.index_version!r}, current "
                f"index is {self._version!r}; old cursors cannot be mixed into "
                "a rebuilt index — restart the query"
            )
        if cursor.mode != mode:
            raise InvalidCursor(
                f"cursor mode {cursor.mode!r} cannot resume a {mode!r} query"
            )
        return cursor.last_sort_key, cursor.last_seq

    def _paginate(
        self,
        ordered_rows: list[sqlite3.Row],
        mode: str,
        limit: int,
        scanned: int,
        degraded: bool,
    ) -> Page:
        page = ordered_rows[:limit]
        next_cursor: str | None = None
        if len(ordered_rows) > limit and page:
            last = page[-1]
            next_cursor = Cursor(
                index_version=self._version,
                mode=mode,
                last_sort_key=bytes(last["sort_key"]),
                last_seq=int(last["seq"]),
            ).encode()
        return Page(
            rows=page,
            next_cursor=next_cursor,
            degraded=degraded,
            scanned=scanned,
            matched=len(ordered_rows),
        )

    # ---------- sorted listing ----------

    def list_sorted(
        self, limit: int, cursor_token: str | None, trace: Trace
    ) -> Page:
        trace.bind_version(self._version)
        after_key, after_seq = self._check_cursor(cursor_token, MODE_LIST)
        trace.step(
            "seek_list",
            "query/service.list_sorted",
            after=cursor_token is not None,
            limit=limit,
        )
        rows = self._store.list_page(after_key, after_seq, limit + 1)
        page = self._paginate(rows, MODE_LIST, limit, scanned=len(rows), degraded=False)
        trace.step(
            "return_page",
            "query/service.list_sorted",
            returned=len(page.rows),
            has_next=page.next_cursor is not None,
        )
        return page

    # ---------- value range ----------

    def range_between(
        self,
        low: str,
        high: str,
        limit: int,
        cursor_token: str | None,
        trace: Trace,
    ) -> Page:
        trace.bind_version(self._version)
        after_key, after_seq = self._check_cursor(cursor_token, MODE_RANGE)
        low_key, high_key = bounded_interval(
            self._store.key_for(low), self._store.key_for(high)
        )
        trace.step(
            "compute_key_bounds",
            "query/service.range_between",
            low=low,
            high=high,
            low_key=low_key.hex(),
            high_key=high_key.hex(),
            swapped=(self._store.key_for(low) != low_key),
        )
        rows = self._store.range_page(
            low_key, high_key, after_key, after_seq, limit + 1
        )
        page = self._paginate(rows, MODE_RANGE, limit, scanned=len(rows), degraded=False)
        trace.step(
            "return_page",
            "query/service.range_between",
            returned=len(page.rows),
            has_next=page.next_cursor is not None,
        )
        return page

    # ---------- prefix retrieval ----------

    def prefix_search(
        self,
        prefix: str,
        match: str,
        limit: int,
        cursor_token: str | None,
        trace: Trace,
    ) -> Page:
        if match not in (PREFIX_COLLATION, PREFIX_TEXT):
            raise InvalidCursor(f"unknown prefix match mode {match!r}")
        trace.bind_version(self._version)
        self._check_cursor(cursor_token, MODE_PREFIX)

        use_seek = seek_supported(self._store.engine)
        prefix_key = self._store.key_for(prefix)

        if use_seek:
            trace.step(
                "primary_seek",
                "query/service.prefix_search",
                match=match,
                prefix_key=prefix_key.hex(),
                strategy="indexed_primary_interval",
            )
            candidates = self._collect_seek_candidates(prefix_key)
            strategy = "indexed_primary_interval+library_filter"
        else:
            if self._store.options.numeric:
                reason = "numeric_collation_merges_digit_weights"
            elif self._store.options.strength == 15:
                reason = "identical_strength_combining_boundary"
            else:
                reason = "locale_contraction_non_concatenating_primary"
            trace.uncertain(
                "prefix recall cannot be guaranteed by the primary-key seek; "
                "using a full ordered scan with an exact library predicate",
                "query/service.prefix_search",
                reason=reason,
                numeric=self._store.options.numeric,
                strength=self._store.options.strength,
            )
            trace.step(
                "full_scan",
                "query/service.prefix_search",
                match=match,
                strategy="full_scan+library_filter",
            )
            candidates = self._store.all_rows_ordered()

        matched_rows = [
            row
            for row in candidates
            if self._prefix_predicate(row["text"], prefix, match)
        ]
        trace.step(
            "filter_and_order",
            "query/service.prefix_search",
            strategy=strategy if use_seek else "full_scan+library_filter",
            scanned=len(candidates),
            matched=len(matched_rows),
        )

        # Apply keyset position after filtering.
        if cursor_token:
            cursor = Cursor.decode(cursor_token)
            matched_rows = [
                r
                for r in matched_rows
                if (bytes(r["sort_key"]), int(r["seq"]))
                > (cursor.last_sort_key, cursor.last_seq)
            ]
        page = self._paginate(
            matched_rows,
            MODE_PREFIX,
            limit,
            scanned=len(candidates),
            degraded=not use_seek,
        )
        trace.step(
            "return_page",
            "query/service.prefix_search",
            returned=len(page.rows),
            has_next=page.next_cursor is not None,
            degraded=page.degraded,
        )
        return page

    def _collect_seek_candidates(self, prefix_key: bytes) -> list[sqlite3.Row]:
        batch = 10_000
        collected: list[sqlite3.Row] = []
        offset = 0
        while True:
            chunk = self._store.primary_seek_candidates(prefix_key, batch, offset)
            collected.extend(chunk)
            if len(chunk) < batch:
                break
            offset += batch
        return collected

    def _prefix_predicate(self, text: str, prefix: str, match: str) -> bool:
        if match == PREFIX_TEXT:
            import unicodedata

            return unicodedata.normalize("NFC", text).startswith(
                unicodedata.normalize("NFC", prefix)
            )
        return self._store.engine.collation_prefix_match(text, prefix)
