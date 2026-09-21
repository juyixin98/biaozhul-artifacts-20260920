-- SynapticGo initial schema.

CREATE TABLE users (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 128),
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content-addressed physical files. A blob row is created inside the same
-- transaction as the first referencing row; refcount must therefore always
-- match the number of referencing rows (dataset_chunks + datasets).
-- Physical file is fs_path (blobs/<ab>/<sha256>). Blobs in state 'deleting'
-- have already been unlinked; GC keeps the row until the file is gone so that
-- concurrent publishers cannot resurrect a file that is being removed.
CREATE TABLE blobs (
    sha256    TEXT PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size      BIGINT NOT NULL CHECK (size >= 0),
    refcount  INTEGER NOT NULL DEFAULT 0 CHECK (refcount >= 0),
    state     TEXT NOT NULL DEFAULT 'ready' CHECK (state IN ('ready', 'deleting')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE datasets (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id          BIGINT NOT NULL REFERENCES users(id),
    name              TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    status            TEXT NOT NULL DEFAULT 'uploading'
                      CHECK (status IN ('uploading', 'publishing', 'ready')),
    total_size        BIGINT NOT NULL CHECK (total_size >= 0),
    chunk_size        BIGINT NOT NULL CHECK (chunk_size > 0),
    want_whole_sha256 TEXT NOT NULL CHECK (want_whole_sha256 ~ '^[0-9a-f]{64}$'),
    whole_sha256      TEXT REFERENCES blobs(sha256),
    ready_at          TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name)
);

-- Declared chunks, one row per expected position. A row is "received" when
-- blob_sha256 is set; re-uploading the same blob for the same index is
-- idempotent, a different blob is a 409 content conflict.
CREATE TABLE dataset_chunks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id   BIGINT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL CHECK (idx >= 0),
    size         BIGINT NOT NULL CHECK (size >= 0),
    chunk_offset       BIGINT NOT NULL CHECK (chunk_offset >= 0),
    want_sha256  TEXT NOT NULL CHECK (want_sha256 ~ '^[0-9a-f]{64}$'),
    blob_sha256  TEXT REFERENCES blobs(sha256),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, idx)
);
CREATE INDEX idx_dataset_chunks_blob ON dataset_chunks(blob_sha256);

-- Immutable model versions. Weights are kept inline as canonical JSON because
-- the only supported model is a single linear layer; the weight hash still
-- binds the version to an exact byte sequence. Datasets are referenced by
-- content hash (datasets may be deleted after registration; training and eval
-- snapshots remain reproducible through the recorded hashes).
CREATE TABLE model_versions (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id           BIGINT NOT NULL REFERENCES users(id),
    name               TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    version            TEXT NOT NULL CHECK (length(version) BETWEEN 1 AND 64),
    input_dim          INTEGER NOT NULL CHECK (input_dim > 0),
    classes            JSONB NOT NULL,
    classes_hash       TEXT NOT NULL CHECK (classes_hash ~ '^[0-9a-f]{64}$'),
    weights            JSONB NOT NULL,
    weight_sha256      TEXT NOT NULL CHECK (weight_sha256 ~ '^[0-9a-f]{64}$'),
    train_dataset_sha  TEXT CHECK (train_dataset_sha ~ '^[0-9a-f]{64}$'),
    eval_dataset_sha   TEXT CHECK (eval_dataset_sha ~ '^[0-9a-f]{64}$'),
    metrics            JSONB NOT NULL DEFAULT '{}',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name, version)
);
CREATE INDEX idx_models_owner_name ON model_versions(owner_id, name);

-- One row per inference call.
CREATE TABLE experiments (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id          BIGINT NOT NULL REFERENCES users(id),
    model_version_id  BIGINT NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    input             JSONB NOT NULL,
    probs             JSONB NOT NULL,
    predicted_class   INTEGER NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_experiments_owner_model ON experiments(owner_id, model_version_id, id);
