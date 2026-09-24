-- Layer-reference garbage collection schema (PostgreSQL 13+).
-- All timestamps are timestamptz; digests are canonical "sha256:<hex>".

CREATE TABLE IF NOT EXISTS blobs (
    digest          TEXT PRIMARY KEY
                    CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    size_bytes      BIGINT NOT NULL CHECK (size_bytes >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS manifests (
    digest          TEXT PRIMARY KEY
                    CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    content_type    TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL CHECK (size_bytes >= 0),
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Edges: manifest -> referenced blob.  kind distinguishes layer blobs (the
-- focus of layer GC) from the image config blob; BOTH are GC roots for the
-- blobs they name, since deleting either would corrupt the manifest.
CREATE TABLE IF NOT EXISTS manifest_refs (
    manifest_digest TEXT NOT NULL REFERENCES manifests(digest) ON DELETE CASCADE,
    blob_digest     TEXT NOT NULL REFERENCES blobs(digest)   ON DELETE RESTRICT,
    kind            TEXT NOT NULL CHECK (kind IN ('layer','config')),
    ordinal         INT  NOT NULL,
    PRIMARY KEY (manifest_digest, blob_digest),
    UNIQUE (manifest_digest, kind, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_manifest_refs_layer ON manifest_refs(blob_digest);

CREATE TABLE IF NOT EXISTS tags (
    repo            TEXT NOT NULL,
    tag             TEXT NOT NULL,
    manifest_digest TEXT NOT NULL REFERENCES manifests(digest) ON DELETE RESTRICT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_manifest ON tags(manifest_digest);

-- Read leases acquired by pulls.  Expired leases are treated as inactive.
CREATE TABLE IF NOT EXISTS leases (
    id              BIGSERIAL PRIMARY KEY,
    blob_digest     TEXT NOT NULL REFERENCES blobs(digest) ON DELETE CASCADE,
    holder          TEXT NOT NULL,
    acquired_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_blob ON leases(blob_digest);
CREATE INDEX IF NOT EXISTS idx_leases_expiry ON leases(expires_at);

-- Streaming blob uploads (staged on disk under a temp dir until committed).
CREATE TABLE IF NOT EXISTS blob_uploads (
    upload_id       TEXT PRIMARY KEY,
    temp_path       TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed       BOOLEAN NOT NULL DEFAULT FALSE
);

-- Garbage-collection runs and per-item audit records.
CREATE TABLE IF NOT EXISTS gc_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL CHECK (status IN ('marking','sweeping','completed','crashed')),
    snapshot_xmin   BIGINT,
    marked_count    INT NOT NULL DEFAULT 0,
    candidate_count INT NOT NULL DEFAULT 0,
    deleted_count   INT NOT NULL DEFAULT 0,
    retained_count  INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gc_items (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES gc_runs(id) ON DELETE CASCADE,
    blob_digest     TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('retain','delete')),
    reason          TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    marked          BOOLEAN NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gc_items_run ON gc_items(run_id);

-- Separate audit trail for orphan-temp-file maintenance (never mixed with
-- layer GC: staged uploads are not published content).
CREATE TABLE IF NOT EXISTS orphan_cleanup_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    removed_count   INT NOT NULL DEFAULT 0,
    reclaimed_bytes BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orphan_cleanup_items (
    id              BIGSERIAL PRIMARY KEY,
    run_id          BIGINT NOT NULL REFERENCES orphan_cleanup_runs(id) ON DELETE CASCADE,
    path            TEXT NOT NULL,
    reason          TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    removed         BOOLEAN NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
