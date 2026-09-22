-- VFX render queue schema.
-- All timestamps are UTC. Status strings are CHECK constrained.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Users & projects
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    -- sha256 hex of the API key (keys are secrets; only the digest is stored)
    api_key_hash  TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    owner_id    UUID NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE project_members (
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);

-- The owner is implicitly a member, but an explicit row keeps authz checks
-- to a single table.
INSERT INTO project_members (project_id, user_id)
SELECT id, owner_id FROM projects;

CREATE OR REPLACE FUNCTION add_owner_as_member() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO project_members (project_id, user_id)
    VALUES (NEW.id, NEW.owner_id)
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_projects_owner_member
    AFTER INSERT ON projects
    FOR EACH ROW EXECUTE FUNCTION add_owner_as_member();

-- ---------------------------------------------------------------------------
-- Assets: PNG layers, content-addressed by sha256 within a project
-- ---------------------------------------------------------------------------

CREATE TABLE assets (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,
    width        INT  NOT NULL CHECK (width  > 0),
    height       INT  NOT NULL CHECK (height > 0),
    size_bytes   BIGINT NOT NULL CHECK (size_bytes >= 0),
    sha256       TEXT NOT NULL,
    uploaded_by  UUID NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Reloading the same path supersedes the prior file; versions pin the
    -- digest, so frozen renders are unaffected.
    UNIQUE (project_id, rel_path)
);

-- ---------------------------------------------------------------------------
-- Compositions and their immutable versions
-- ---------------------------------------------------------------------------

CREATE TABLE compositions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    width       INT NOT NULL CHECK (width  > 0),
    height      INT NOT NULL CHECK (height > 0),
    created_by  UUID NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

-- Every version is frozen at creation: the manifest JSON plus the resolved
-- asset digests are copied here and never mutated afterwards. Jobs bind to
-- a version id + digest, so later edits to assets/compositions cannot change
-- a task that is queued or running.
CREATE TABLE versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    composition_id  UUID NOT NULL REFERENCES compositions(id) ON DELETE CASCADE,
    version_no      BIGINT NOT NULL,
    manifest        JSONB NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (composition_id, version_no)
);

-- Resolved resource snapshot: one row per layer reference. The worker loads
-- layers only through these (id, sha256) pairs and verifies bytes on disk.
CREATE TABLE version_resources (
    version_id  UUID NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    layer_id    TEXT NOT NULL,
    asset_id    UUID NOT NULL REFERENCES assets(id),
    sha256      TEXT NOT NULL,
    PRIMARY KEY (version_id, layer_id)
);

-- ---------------------------------------------------------------------------
-- Jobs and per-frame state
-- ---------------------------------------------------------------------------

CREATE TABLE jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id     UUID NOT NULL REFERENCES versions(id),
    project_id     UUID NOT NULL REFERENCES projects(id),
    created_by     UUID NOT NULL REFERENCES users(id),
    -- 1 = highest, 10 = lowest
    priority       INT NOT NULL CHECK (priority BETWEEN 1 AND 10),
    frame_start    INT NOT NULL CHECK (frame_start >= 0),
    frame_end      INT NOT NULL CHECK (frame_end >= frame_start),
    status         TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    error          TEXT NOT NULL DEFAULT '',
    -- FIFO tie-breaker inside a priority
    enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ,
    canceled_by    UUID REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_jobs_dispatch
    ON jobs (priority, enqueued_at)
    WHERE status IN ('queued', 'running');

CREATE TABLE frames (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    frame_no      INT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'leased', 'succeeded', 'failed')),
    attempts      INT NOT NULL DEFAULT 0,
    max_attempts  INT NOT NULL DEFAULT 4, -- 1 initial try + 3 retries
    generation    BIGINT NOT NULL DEFAULT 0,
    output_sha256 TEXT NOT NULL DEFAULT '',
    output_size   BIGINT NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    leased_by     TEXT,
    leased_until  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, frame_no)
);

-- Dispatch index: runnable frames of the highest-priority oldest job first.
CREATE INDEX idx_frames_dispatch
    ON frames (status, leased_until);

CREATE INDEX idx_frames_job_status
    ON frames (job_id, status);
