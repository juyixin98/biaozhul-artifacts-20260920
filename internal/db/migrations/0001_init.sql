-- VFX render queue core schema.

CREATE TABLE users (
    id          uuid PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    display_name text NOT NULL,
    role        text NOT NULL CHECK (role IN ('admin', 'member')),
    api_token   text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id          uuid PRIMARY KEY,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_by  uuid NOT NULL REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE project_members (
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        text NOT NULL CHECK (role IN ('owner', 'member')),
    added_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX idx_project_members_user ON project_members(user_id);

CREATE TABLE assets (
    id           uuid PRIMARY KEY,
    project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename     text NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
    content_type text NOT NULL,
    width        integer NOT NULL CHECK (width > 0 AND width <= 16384),
    height       integer NOT NULL CHECK (height > 0 AND height <= 16384),
    size_bytes   bigint NOT NULL CHECK (size_bytes > 0),
    sha256       text NOT NULL,
    storage_path text NOT NULL,
    uploaded_by  uuid NOT NULL REFERENCES users(id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, sha256)
);

CREATE TABLE compositions (
    id          uuid PRIMARY KEY,
    project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    canvas_width  integer NOT NULL CHECK (canvas_width > 0 AND canvas_width <= 16384),
    canvas_height integer NOT NULL CHECK (canvas_height > 0 AND canvas_height <= 16384),
    frame_count   integer NOT NULL CHECK (frame_count > 0 AND frame_count <= 100000),
    current_version_id uuid,
    created_by  uuid NOT NULL REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_compositions_project ON compositions(project_id);

CREATE TABLE composition_versions (
    id              uuid PRIMARY KEY,
    composition_id  uuid NOT NULL REFERENCES compositions(id) ON DELETE CASCADE,
    version_number  integer NOT NULL CHECK (version_number > 0),
    spec_json       jsonb NOT NULL,
    spec_sha256     text NOT NULL,
    created_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (composition_id, version_number)
);

ALTER TABLE compositions
    ADD CONSTRAINT fk_compositions_current_version
    FOREIGN KEY (current_version_id) REFERENCES composition_versions(id);

-- Frozen resource snapshot: render tasks bind to these exact asset digests.
CREATE TABLE composition_version_resources (
    version_id   uuid NOT NULL REFERENCES composition_versions(id) ON DELETE CASCADE,
    layer_id     text NOT NULL,
    asset_id     uuid NOT NULL REFERENCES assets(id),
    asset_sha256 text NOT NULL,
    PRIMARY KEY (version_id, layer_id)
);

CREATE TABLE render_tasks (
    id              uuid PRIMARY KEY,
    project_id      uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    composition_id  uuid NOT NULL REFERENCES compositions(id),
    version_id      uuid NOT NULL REFERENCES composition_versions(id),
    frame_start     integer NOT NULL CHECK (frame_start >= 0),
    frame_end       integer NOT NULL CHECK (frame_end >= frame_start),
    priority        smallint NOT NULL CHECK (priority BETWEEN 1 AND 10),
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    enqueued_seq    bigserial NOT NULL,
    error           text,
    created_by      uuid NOT NULL REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    output_dir      text NOT NULL
);
CREATE INDEX idx_render_tasks_dispatch
    ON render_tasks(priority DESC, enqueued_seq ASC)
    WHERE status IN ('queued', 'running');
CREATE INDEX idx_render_tasks_project ON render_tasks(project_id, created_at DESC);

CREATE TABLE frames (
    id           uuid PRIMARY KEY,
    task_id      uuid NOT NULL REFERENCES render_tasks(id) ON DELETE CASCADE,
    frame_index  integer NOT NULL CHECK (frame_index >= 0),
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'leased', 'succeeded', 'failed', 'cancelled')),
    attempts     smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token  uuid,
    leased_by    text,
    leased_at    timestamptz,
    lease_expires_at timestamptz,
    generation   bigint NOT NULL DEFAULT 0,
    output_path  text,
    output_sha256 text,
    output_size_bytes bigint,
    error        text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, frame_index)
);
-- Dispatch index: highest priority first, FIFO inside a priority, frame order inside a task.
CREATE INDEX idx_frames_dispatch ON frames(status, lease_expires_at);
CREATE INDEX idx_frames_task ON frames(task_id, frame_index);
