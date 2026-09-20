CREATE TABLE files (
    id         BIGSERIAL PRIMARY KEY,
    sha256     TEXT NOT NULL UNIQUE,
    size       BIGINT NOT NULL,
    path       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE datasets (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading','merging','published')),
    total_size   BIGINT NOT NULL,
    chunk_size   BIGINT NOT NULL,
    chunk_count  INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    file_id      BIGINT REFERENCES files(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);
CREATE INDEX datasets_owner_idx ON datasets(owner_id);
CREATE INDEX datasets_file_idx ON datasets(file_id);

CREATE TABLE dataset_chunks (
    dataset_id  BIGINT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    size        BIGINT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, chunk_index)
);

CREATE TABLE models (
    id         BIGSERIAL PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX models_owner_idx ON models(owner_id);

-- Model versions are immutable once created: no UPDATE path exists.
CREATE TABLE model_versions (
    id             BIGSERIAL PRIMARY KEY,
    model_id       BIGINT NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    version        INTEGER NOT NULL,
    dataset_id     BIGINT NOT NULL REFERENCES datasets(id),
    dataset_sha256 TEXT NOT NULL,
    input_dim      INTEGER NOT NULL,
    labels         JSONB NOT NULL,
    weights        JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model_id, version)
);

CREATE TABLE experiments (
    id               BIGSERIAL PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    name             TEXT NOT NULL,
    model_version_id BIGINT NOT NULL REFERENCES model_versions(id),
    dataset_id       BIGINT NOT NULL REFERENCES datasets(id),
    metrics          JSONB NOT NULL DEFAULT '{}',
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX experiments_owner_idx ON experiments(owner_id);
CREATE INDEX experiments_mv_idx ON experiments(model_version_id);
