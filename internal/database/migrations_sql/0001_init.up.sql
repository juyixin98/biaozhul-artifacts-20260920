-- Community content governance: initial schema
-- All money is stored as integer cents (BIGINT). All timestamps TIMESTAMPTZ.

CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','reviewer','member')),
    password_hash TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE communities (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    created_by  BIGINT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Community staff (reviewers). Admins are implicit via users.role='admin'.
CREATE TABLE community_reviewers (
    community_id  BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (community_id, user_id)
);

CREATE TABLE tiers (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id  BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    level         INT  NOT NULL CHECK (level BETWEEN 1 AND 10),
    name          TEXT NOT NULL,
    price_cents   BIGINT NOT NULL CHECK (price_cents >= 0),
    duration_days INT  NOT NULL CHECK (duration_days > 0),
    UNIQUE (community_id, level)
);

-- A community may define at most 10 tiers (levels 1..10).
CREATE OR REPLACE FUNCTION tiers_max_10() RETURNS trigger AS $$
DECLARE
    cnt INT;
BEGIN
    SELECT count(*) INTO cnt FROM tiers WHERE community_id = NEW.community_id;
    IF (TG_OP = 'UPDATE') THEN
        -- level changes do not change the count; only enforce on INSERT
        cnt := 0;
    END IF;
    IF cnt >= 10 THEN
        RAISE EXCEPTION 'community % already has the maximum of 10 tiers', NEW.community_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tiers_max_10
    BEFORE INSERT ON tiers
    FOR EACH ROW EXECUTE FUNCTION tiers_max_10();

CREATE TABLE memberships (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id  BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    user_id       BIGINT NOT NULL REFERENCES users(id),
    tier_id       BIGINT NOT NULL REFERENCES tiers(id),
    starts_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (community_id, user_id)
);

-- Admin-recorded offline payments. Idempotent on (community, request_id):
-- the same request_id with a different payload is a conflict (409).
CREATE TABLE payments (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id      BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    request_id        TEXT NOT NULL,
    user_id           BIGINT NOT NULL REFERENCES users(id),
    tier_id           BIGINT NOT NULL REFERENCES tiers(id),
    amount_cents      BIGINT NOT NULL CHECK (amount_cents >= 0),
    extend_days       INT  NOT NULL CHECK (extend_days > 0),
    membership_id     BIGINT NOT NULL REFERENCES memberships(id),
    recorded_by      BIGINT NOT NULL REFERENCES users(id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (community_id, request_id)
);

-- Posts and immutable versions ------------------------------------------------

CREATE TABLE posts (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id          BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    author_id             BIGINT NOT NULL REFERENCES users(id),
    required_tier_level   INT NOT NULL DEFAULT 1 CHECK (required_tier_level BETWEEN 1 AND 10),
    title                 TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'draft'
                            CHECK (status IN ('draft','pending','published','removed')),
    current_version_id    BIGINT,
    published_version_id  BIGINT,
    -- moderation bookkeeping
    removed_reason        TEXT NOT NULL DEFAULT '',
    restored_from_version_id BIGINT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_posts_community_status ON posts(community_id, status);

CREATE TABLE post_versions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id         BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    version_number  INT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    required_tier_level INT NOT NULL CHECK (required_tier_level BETWEEN 1 AND 10),
    author_id       BIGINT NOT NULL REFERENCES users(id),
    review_status   TEXT NOT NULL DEFAULT 'draft'
                    CHECK (review_status IN ('draft','pending','approved','rejected')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (post_id, version_number)
);
CREATE INDEX idx_post_versions_post ON post_versions(post_id);

ALTER TABLE posts
    ADD CONSTRAINT fk_posts_current_version
    FOREIGN KEY (current_version_id) REFERENCES post_versions(id);
ALTER TABLE posts
    ADD CONSTRAINT fk_posts_published_version
    FOREIGN KEY (published_version_id) REFERENCES post_versions(id);

-- Attachments belong to an immutable version. Download/export reuse the same
-- access gate as the body, so isolation is consistent.
CREATE TABLE attachments (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id  BIGINT NOT NULL REFERENCES post_versions(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    data        BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attach_version ON attachments(version_id);

-- Moderation reviews. Every review row binds a specific version and the
-- expected post status, so an approve racing an edit cannot publish the new,
-- unreviewed content.
CREATE TABLE reviews (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id         BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    version_id      BIGINT NOT NULL REFERENCES post_versions(id),
    reviewer_id     BIGINT NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL CHECK (action IN ('approve','reject','takedown')),
    reason          TEXT NOT NULL DEFAULT '',
    expected_status TEXT NOT NULL,
    success         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reviews_post ON reviews(post_id);

-- Reports ---------------------------------------------------------------------

CREATE TABLE reports (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id      BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    reporter_id       BIGINT NOT NULL REFERENCES users(id),
    post_id           BIGINT NOT NULL REFERENCES posts(id),
    target_version_id BIGINT NOT NULL REFERENCES post_versions(id),
    category          TEXT NOT NULL,
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'filed'
                      CHECK (status IN ('filed','accepted','upheld','overturned',
                                        'rejected','appealed','restored')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reports_status ON reports(community_id, status);

CREATE TABLE report_decisions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id    BIGINT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    action       TEXT NOT NULL CHECK (action IN ('accept','uphold','overturn',
                                                 'reject','appeal','restore')),
    actor_id     BIGINT NOT NULL REFERENCES users(id),
    reason       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_report_decisions_report ON report_decisions(report_id);

-- Courses ---------------------------------------------------------------------
-- Draft structure is mutable rows; each publish freezes a snapshot that the
-- published course serves. Later edits only touch the draft rows.

CREATE TABLE courses (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id BIGINT NOT NULL REFERENCES communities(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
    published_snapshot_id BIGINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_courses_community ON courses(community_id);

CREATE TABLE course_modules (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    course_id   BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    position    INT NOT NULL,
    title       TEXT NOT NULL,
    UNIQUE (course_id, position)
);

CREATE TABLE course_lessons (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    module_id        BIGINT NOT NULL REFERENCES course_modules(id) ON DELETE CASCADE,
    position         INT NOT NULL,
    title            TEXT NOT NULL,
    content_version_id BIGINT REFERENCES post_versions(id),
    UNIQUE (module_id, position)
);

CREATE TABLE course_publish_snapshots (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    course_id    BIGINT NOT NULL REFERENCES courses(id),
    title        TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_by BIGINT NOT NULL REFERENCES users(id)
);

CREATE TABLE course_snapshot_modules (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES course_publish_snapshots(id) ON DELETE CASCADE,
    position    INT NOT NULL,
    title       TEXT NOT NULL
);
CREATE INDEX idx_snap_modules ON course_snapshot_modules(snapshot_id);

CREATE TABLE course_snapshot_lessons (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snap_module_id   BIGINT NOT NULL REFERENCES course_snapshot_modules(id) ON DELETE CASCADE,
    position         INT NOT NULL,
    title            TEXT NOT NULL,
    content_version_id BIGINT NOT NULL REFERENCES post_versions(id)
);
CREATE INDEX idx_snap_lessons ON course_snapshot_lessons(snap_module_id);

ALTER TABLE courses
    ADD CONSTRAINT fk_courses_snapshot
    FOREIGN KEY (published_snapshot_id)
    REFERENCES course_publish_snapshots(id);
