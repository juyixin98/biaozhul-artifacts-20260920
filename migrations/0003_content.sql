-- 0003_content.sql: content threads and their immutable revisions
CREATE TABLE contents (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_id      BIGINT NOT NULL REFERENCES categories(id),
    author_id        BIGINT NOT NULL REFERENCES users(id),
    title            TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('draft','pending','published','withdrawn')),
    -- Pointer columns reference content_revisions by value (no FK: the two
    -- tables reference each other, and revisions always outlive content).
    current_revision BIGINT,
    published_revision BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_contents_feed ON contents (status, id) WHERE status = 'published';
CREATE INDEX idx_contents_author ON contents (author_id);
CREATE INDEX idx_contents_status ON contents (status);

CREATE TABLE content_revisions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    body       TEXT NOT NULL,
    edit_reason TEXT NOT NULL DEFAULT '',
    -- origin: original edit | rollback to an older revision
    origin     TEXT NOT NULL CHECK (origin IN ('edit','rollback')),
    source_revision_id BIGINT REFERENCES content_revisions(id),
    author_id  BIGINT NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (content_id, revision_no)
);
CREATE INDEX idx_revisions_content ON content_revisions (content_id, revision_no);
