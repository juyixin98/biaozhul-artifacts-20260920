-- Schema for the local content-addressable image registry with safe,
-- reference-aware garbage collection.
--
-- The model mirrors the OCI Distribution v2 data model:
--   * blobs    - content-addressable layers / configs, shared globally
--   * manifests - per-repository manifest documents, themselves CAS digested
--   * manifest_refs - edges manifest -> child (manifest or blob)
--   * tags     - mutable named references to a manifest
--   * uploads  - resumable chunked-upload sessions (staging area)
--   * leases   - read leases that pin objects while a pull is in flight
--   * gc_runs / gc_items / gc_audit_events - auditable GC bookkeeping

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Blobs are globally shared content. `size` and digest are authoritative:
-- content is verified (sha256 streaming hash) before a row is published.
CREATE TABLE IF NOT EXISTS blobs (
    digest         TEXT PRIMARY KEY,
    size           BIGINT NOT NULL CHECK (size >= 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS manifests (
    repo        TEXT NOT NULL,
    digest      TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size        BIGINT NOT NULL CHECK (size >= 0),
    content     BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, digest)
);

-- Edges of the manifest DAG. A child is either another manifest in the same
-- repository (child_kind='manifest') or a global blob (child_kind='blob').
CREATE TABLE IF NOT EXISTS manifest_refs (
    repo          TEXT NOT NULL,
    parent_digest TEXT NOT NULL,
    child_kind    TEXT NOT NULL CHECK (child_kind IN ('manifest','blob')),
    child_digest  TEXT NOT NULL,
    PRIMARY KEY (repo, parent_digest, child_kind, child_digest),
    FOREIGN KEY (repo, parent_digest)
        REFERENCES manifests(repo, digest) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_manifest_refs_child_blob
    ON manifest_refs(child_digest) WHERE child_kind = 'blob';
CREATE INDEX IF NOT EXISTS idx_manifest_refs_child_manifest
    ON manifest_refs(repo, child_digest) WHERE child_kind = 'manifest';

CREATE TABLE IF NOT EXISTS tags (
    repo        TEXT NOT NULL,
    name        TEXT NOT NULL,
    digest      TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, name),
    FOREIGN KEY (repo, digest) REFERENCES manifests(repo, digest) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_repo ON tags(repo);

-- Resumable upload sessions. started_at drives orphan (abandoned upload) GC.
CREATE TABLE IF NOT EXISTS uploads (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    name        TEXT NOT NULL,           -- temp file name inside staging dir
    offset_bytes BIGINT NOT NULL DEFAULT 0,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Read leases.
--   kind='manifest' -> pins (repo, digest) manifest
--   kind='blob'     -> pins the global blob `digest`
-- A pull takes a lease before streaming bytes and releases it afterwards;
-- the GC sweeper treats any active (unexpired) lease as a live root.
CREATE TABLE IF NOT EXISTS leases (
    id          TEXT PRIMARY KEY,
    repo        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('manifest','blob')),
    digest      TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leases_blob ON leases(digest, expires_at) WHERE kind = 'blob';
CREATE INDEX IF NOT EXISTS idx_leases_manifest ON leases(repo, digest, expires_at) WHERE kind = 'manifest';

-- One row per garbage-collection run. `state` lifecycle:
--   marking -> sweeping -> completed
--   marking/sweeping + crashed_at set on recovery -> recovered (then a fresh
--   run may start). status='failed' for fatal errors.
CREATE TABLE IF NOT EXISTS gc_runs (
    id              TEXT PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    state           TEXT NOT NULL DEFAULT 'marking'
                    CHECK (state IN ('marking','sweeping','completed','recovered','failed')),
    mark_snapshot   BIGINT,              -- pg_current_xact_id() at mark time
    marked_at       TIMESTAMPTZ,
    grace_seconds   INTEGER NOT NULL,
    blobs_total     INTEGER NOT NULL DEFAULT 0,
    manifests_total INTEGER NOT NULL DEFAULT 0,
    deleted_blobs   INTEGER NOT NULL DEFAULT 0,
    deleted_manifests INTEGER NOT NULL DEFAULT 0,
    retained_blobs  INTEGER NOT NULL DEFAULT 0,
    retained_manifests INTEGER NOT NULL DEFAULT 0,
    crashed_at      TIMESTAMPTZ,
    crash_reason    TEXT,
    note            TEXT
);

-- Per-object audit row. Every object considered by a run gets a row with the
-- final decision and the exact human-auditable reason.
CREATE TABLE IF NOT EXISTS gc_items (
    run_id        TEXT NOT NULL REFERENCES gc_runs(id),
    kind          TEXT NOT NULL CHECK (kind IN ('blob','manifest')),
    repo          TEXT NOT NULL DEFAULT '',
    digest        TEXT NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('retain','delete')),
    reason        TEXT NOT NULL,
    decided_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, kind, repo, digest)
);
CREATE INDEX IF NOT EXISTS idx_gc_items_digest ON gc_items(digest);

-- Append-only event log: deletes, recoveries, and API deletions land here so
-- the audit trail survives GC rows themselves being archived.
CREATE TABLE IF NOT EXISTS gc_audit_events (
    id         BIGSERIAL PRIMARY KEY,
    run_id     TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    action     TEXT NOT NULL,           -- gc.run_start / gc.delete / gc.recover ...
    kind       TEXT,
    repo       TEXT,
    digest     TEXT,
    detail     TEXT
);
CREATE INDEX IF NOT EXISTS idx_gc_audit_events_run ON gc_audit_events(run_id);

INSERT INTO schema_version(version) VALUES (1)
ON CONFLICT (version) DO NOTHING;
