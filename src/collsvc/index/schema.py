"""SQLite DDL for the collation index."""
from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS index_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    doc_id       TEXT PRIMARY KEY,
    text         TEXT NOT NULL,           -- original spelling, never rewritten
    nfc_text     TEXT NOT NULL,
    sort_key     BLOB NOT NULL,           -- full ICU sort key
    primary_key  BLOB NOT NULL,           -- primary-level section only
    seq          INTEGER NOT NULL         -- stable input order (tie-break)
);

-- Main collation ordering / range index (BLOB byte order = ICU key order).
CREATE INDEX IF NOT EXISTS idx_entries_sort_key
    ON entries (sort_key, seq);

-- Covering index used by the primary-section prefix seek.
CREATE INDEX IF NOT EXISTS idx_entries_primary_key
    ON entries (primary_key, seq);

CREATE TABLE IF NOT EXISTS build_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event        TEXT NOT NULL,
    detail       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

META_KEYS = (
    "index_version",
    "schema_generation",
    "rules_fingerprint",
    "options_json",
    "icu_version",
    "unicode_version",
    "actual_locale",
    "corpus_name",
    "corpus_sha256",
    "row_count",
    "built_at",
)
