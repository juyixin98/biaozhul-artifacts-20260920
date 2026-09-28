"""Load local synthetic fixtures from SQL files into the evidence store.

Fixture SQL is plain SQLite DDL/DML using business table and column names.
Two optional reserved columns attach provenance metadata:

* ``__row_id  TEXT`` - explicit witness id (must be unique within the table)
* ``__weight  REAL`` - numeric weight used to verify symbolic provenance

Both reserved columns are stripped from the witnessed business tuple.  Tables
without ``__row_id`` get deterministic synthetic ids ``<table>#<n>`` (n is the
SQLite rowid order).  Native SQL ``NULL`` values are preserved.

All SQL files in one directory load are executed, in sorted filename order,
against a single throwaway SQLite database and then reflected into one new
immutable input version in the evidence store.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .store import EvidenceStore, InputRow

ROW_ID_COLUMN = "__row_id"
WEIGHT_COLUMN = "__weight"
RESERVED_COLUMNS = frozenset({ROW_ID_COLUMN, WEIGHT_COLUMN})


class FixtureLoadError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.message = message


def load_sql_directory(
    directory: str | Path,
    glob_pattern: str,
    store: EvidenceStore,
    label: str,
) -> int:
    directory = Path(directory)
    files = sorted(directory.glob(glob_pattern))
    sql_files = [f for f in files if f.is_file()]
    if not sql_files:
        raise FixtureLoadError(
            "NO_FIXTURES",
            f"no fixture files matching {glob_pattern!r} found in {directory}",
        )
    texts = [(path.name, path.read_text(encoding="utf-8")) for path in sql_files]
    return load_sql_texts(texts, store, label=label)


def load_sql_text(sql: str, store: EvidenceStore, label: str) -> int:
    return load_sql_texts([("inline.sql", sql)], store, label=label)


def load_sql_texts(
    named_sql: Iterable[tuple[str, str]], store: EvidenceStore, label: str
) -> int:
    # Each fixture source is executed in its own throwaway database, so the set
    # of tables a file introduces is unambiguous. A business relation may only
    # be introduced once; re-declaring a name (even with CREATE TABLE IF NOT
    # EXISTS) is rejected to prevent silent replacement.
    scratch_conns: list[tuple[str, sqlite3.Connection]] = []
    seen_tables: set[str] = set()
    try:
        for source_name, sql in named_sql:
            conn = sqlite3.connect(":memory:")
            try:
                conn.executescript(sql)
            except sqlite3.Error as exc:
                conn.close()
                raise FixtureLoadError(
                    "INVALID_SQL", f"{source_name}: {exc}"
                ) from exc
            created = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%'"
            )}
            duplicate = seen_tables & created
            if duplicate:
                conn.close()
                raise FixtureLoadError(
                    "DUPLICATE_RELATION",
                    f"relation(s) {sorted(duplicate)} already introduced by an "
                    f"earlier fixture file (latest source: {source_name})",
                )
            seen_tables |= created
            scratch_conns.append((source_name, conn))

        if not seen_tables:
            raise FixtureLoadError("NO_RELATIONS", "fixture SQL created no tables")

        version_id = store.create_version(label)
        for source_name, conn in scratch_conns:
            for table in sorted(
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ):
                columns, has_row_id, has_weight = _reflect_columns(conn, table)
                if not columns:
                    raise FixtureLoadError(
                        "EMPTY_RELATION",
                        f"relation {table!r} in {source_name} has no business "
                        "columns (only reserved metadata columns)",
                    )
                rows = _read_rows(conn, table, columns, has_row_id, has_weight)
                store.add_relation(version_id, table, columns, rows)
        return version_id
    finally:
        for _source_name, conn in scratch_conns:
            conn.close()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _reflect_columns(
    conn: sqlite3.Connection, table: str
) -> tuple[tuple[str, ...], bool, bool]:
    info = conn.execute(
        f"PRAGMA table_info({_quote(table)})"
    ).fetchall()
    business: list[str] = []
    has_row_id = False
    has_weight = False
    for _cid, name, _type, _notnull, _dflt, _pk in info:
        if name == ROW_ID_COLUMN:
            has_row_id = True
        elif name == WEIGHT_COLUMN:
            has_weight = True
        else:
            if name in business:
                raise FixtureLoadError(
                    "DUPLICATE_COLUMN",
                    f"relation {table!r} declares column {name!r} twice",
                )
            business.append(name)
    return tuple(business), has_row_id, has_weight


def _read_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    has_row_id: bool,
    has_weight: bool,
) -> list[InputRow]:
    select_cols = [_quote(c) for c in columns]
    if has_row_id:
        select_cols.append(ROW_ID_COLUMN)
    if has_weight:
        select_cols.append(WEIGHT_COLUMN)
    query = (
        f"SELECT {', '.join(select_cols)} FROM {_quote(table)} "
        "ORDER BY rowid"
    )
    rows: list[InputRow] = []
    seen_ids: set[str] = set()
    for ordinal, record in enumerate(conn.execute(query), start=1):
        values = tuple(record[: len(columns)])
        _validate_values(table, values)
        cursor = len(columns)
        if has_row_id:
            row_id = record[cursor]
            cursor += 1
            if not isinstance(row_id, str) or not row_id:
                raise FixtureLoadError(
                    "INVALID_ROW_ID",
                    f"{table} row #{ordinal}: __row_id must be a non-empty string",
                )
        else:
            row_id = f"{table}#{ordinal}"
        if has_weight:
            weight = record[cursor]
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                raise FixtureLoadError(
                    "INVALID_WEIGHT",
                    f"{table} row {row_id!r}: __weight must be a number",
                )
            weight = float(weight)
        else:
            weight = 1.0
        if row_id in seen_ids:
            raise FixtureLoadError(
                "DUPLICATE_ROW_ID",
                f"{table}: __row_id {row_id!r} is used by more than one row",
            )
        seen_ids.add(row_id)
        rows.append(InputRow(row_id=row_id, values=values, weight=weight))
    return rows


def _validate_values(table: str, values: tuple) -> None:
    for value in values:
        if isinstance(value, bool):
            # bool is an int subclass; store it but normalize defensively is
            # unnecessary for this scope. Only reject blobs and odd types.
            continue
        if value is not None and not isinstance(value, (str, int, float)):
            raise FixtureLoadError(
                "UNSUPPORTED_VALUE_TYPE",
                f"relation {table!r} contains a value of unsupported type "
                f"{type(value).__name__}; use TEXT/INTEGER/REAL/NULL",
            )
