-- SynapticGo schema: users, content-addressed objects, datasets/chunks,
-- model versions and experiment records.

CREATE TABLE IF NOT EXISTS users (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    key_hash    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Content-addressed file store shared by every owner. Access is enforced at
-- the dataset / model layer; an object may only be removed when refcount = 0.
CREATE TABLE IF NOT EXISTS objects (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    digest       TEXT NOT NULL UNIQUE,         -- hex SHA-256
    size_bytes   BIGINT NOT NULL CHECK (size_bytes >= 0),
    refcount     INTEGER NOT NULL DEFAULT 1 CHECK (refcount >= 0),
    status       TEXT NOT NULL DEFAULT 'present'
                 CHECK (status IN ('present','garbage')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS datasets (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id        BIGINT NOT NULL REFERENCES users(id),
    name            TEXT NOT NULL,
    total_size      BIGINT NOT NULL CHECK (total_size >= 0),
    chunk_size      INTEGER NOT NULL CHECK (chunk_size > 0),
    chunk_count     INTEGER NOT NULL CHECK (chunk_count >= 1),
    whole_digest    TEXT,                      -- declared expected SHA-256
    whole_object_id BIGINT REFERENCES objects(id),
    status          TEXT NOT NULL DEFAULT 'uploading'
                    CHECK (status IN ('uploading','ready','failed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_datasets_owner ON datasets(owner_id);

-- (dataset_id, chunk_index) uniquely identifies one slot. digest + length tie
-- a slot to a content-addressed object; duplicates are idempotent, mismatches
-- against either the slot or the stored object are rejected.
CREATE TABLE IF NOT EXISTS dataset_chunks (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id    BIGINT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL CHECK (chunk_index >= 0),
    offset_bytes  BIGINT NOT NULL CHECK (offset_bytes >= 0),
    length        INTEGER NOT NULL CHECK (length > 0),
    digest        TEXT NOT NULL,
    object_id     BIGINT NOT NULL REFERENCES objects(id),
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_object ON dataset_chunks(object_id);

-- Immutable once released. class_table_json is the canonical JSON text used to
-- derive class_table_digest; comparisons are only valid for equal digests.
CREATE TABLE IF NOT EXISTS model_versions (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id            BIGINT NOT NULL REFERENCES users(id),
    model_name          TEXT NOT NULL,
    version             INTEGER NOT NULL,
    dataset_id          BIGINT NOT NULL REFERENCES datasets(id),
    dataset_digest      TEXT NOT NULL,        -- assembled dataset SHA-256
    input_dim           INTEGER NOT NULL CHECK (input_dim > 0),
    num_classes         INTEGER NOT NULL CHECK (num_classes >= 2),
    classes             JSONB NOT NULL,        -- canonical ordered list
    class_table_digest  TEXT NOT NULL,
    weights             BYTEA NOT NULL,        -- little-endian float32 matrix
    weight_digest       TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, model_name, version)
);
CREATE INDEX IF NOT EXISTS idx_models_dataset ON model_versions(dataset_id);

CREATE TABLE IF NOT EXISTS experiment_records (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_version_id  BIGINT NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    caller_id         BIGINT NOT NULL REFERENCES users(id),
    input_digest      TEXT NOT NULL,
    input_dim         INTEGER NOT NULL,
    predicted_class   TEXT NOT NULL,
    predicted_index   INTEGER NOT NULL,
    confidence        DOUBLE PRECISION NOT NULL,
    latency_ms        DOUBLE PRECISION NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_exp_model ON experiment_records(model_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_exp_caller ON experiment_records(caller_id, created_at);
