-- 0001_init.sql: community content governance schema
-- All money is stored as integer cents (BIGINT). Time is timestamptz (UTC).

CREATE TABLE communities (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Roles: admin (community owner), moderator (review/moderate), member (author/reader)
CREATE TABLE users (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    username        TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin','moderator','member')),
    token           TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (community_id, username)
);
CREATE INDEX idx_users_community ON users (community_id);

-- Membership tiers: level 1..10, at most 10 per community (enforced by trigger).
CREATE TABLE tiers (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    level           INT NOT NULL CHECK (level BETWEEN 1 AND 10),
    name            TEXT NOT NULL,
    price_cents     BIGINT NOT NULL CHECK (price_cents >= 0),
    duration_days   INT NOT NULL CHECK (duration_days > 0),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (community_id, level)
);

CREATE FUNCTION enforce_tier_cap() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT count(*) FROM tiers WHERE community_id = NEW.community_id) > 10 THEN
        RAISE EXCEPTION 'community % cannot have more than 10 tiers', NEW.community_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_tier_cap AFTER INSERT ON tiers
    FOR EACH ROW EXECUTE FUNCTION enforce_tier_cap();

-- One subscription per member. status: active | cancelled.
-- Access rule: status='active' AND period_end > now().
CREATE TABLE subscriptions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    user_id         BIGINT NOT NULL REFERENCES users(id),
    tier_id         BIGINT NOT NULL REFERENCES tiers(id),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
    period_end      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (community_id, user_id)
);
CREATE INDEX idx_subs_lookup ON subscriptions (community_id, user_id, status, period_end);

-- Idempotent offline payment records (admin confirms money received locally).
-- request_id is client supplied; same request_id with different body -> 409.
CREATE TABLE payments (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    request_id      TEXT NOT NULL,
    user_id         BIGINT NOT NULL REFERENCES users(id),
    tier_id         BIGINT NOT NULL REFERENCES tiers(id),
    amount_cents    BIGINT NOT NULL CHECK (amount_cents >= 0),
    days            INT NOT NULL CHECK (days > 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (community_id, request_id)
);

-- Contents (posts). Lifecycle in status: draft | pending | published | delisted.
-- current_version_id is the working/latest immutable version.
-- published_version_id is the frozen version members may read; NULL until first publish.
CREATE TABLE contents (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id            BIGINT NOT NULL REFERENCES communities(id),
    author_id               BIGINT NOT NULL REFERENCES users(id),
    title                   TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'draft'
                              CHECK (status IN ('draft','pending','published','delisted')),
    current_version_id      BIGINT,
    published_version_id    BIGINT,
    required_level          INT NOT NULL DEFAULT 1 CHECK (required_level BETWEEN 1 AND 10),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_contents_community ON contents (community_id, status);

-- Immutable versions. Each edit/restore inserts a new row; rows never change.
-- review_status: draft | pending | approved | rejected, set once per transition.
CREATE TABLE content_versions (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id      BIGINT NOT NULL REFERENCES contents(id),
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    version_no      INT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    review_status   TEXT NOT NULL DEFAULT 'draft'
                      CHECK (review_status IN ('draft','pending','approved','rejected')),
    created_by      BIGINT NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (content_id, version_no)
);
CREATE INDEX idx_versions_content ON content_versions (content_id);

-- Attachments are bound to an immutable version; the blob is frozen with it.
CREATE TABLE attachments (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    version_id      BIGINT NOT NULL REFERENCES content_versions(id),
    filename        TEXT NOT NULL,
    content_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    data            BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_att_version ON attachments (version_id);

-- Moderation/review audit trail. action: submit | approve | reject | delist | restore
-- Every row binds actor, version, action and optional reason.
CREATE TABLE content_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    content_id      BIGINT NOT NULL REFERENCES contents(id),
    version_id      BIGINT REFERENCES content_versions(id),
    actor_id        BIGINT NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL CHECK (action IN
                      ('submit','approve','reject','delist','restore')),
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_content_events ON content_events (content_id);

-- Reports target a specific immutable version.
-- status: accepted (受理) | upheld (裁决违规, taken down) | dismissed (裁决不违规)
--         | appeal_upheld (申诉后仍违规) | appeal_dismissed (申诉成功, 已恢复)
-- restore is permitted only when status = 'upheld' or 'appeal_upheld' and is a
-- separate content event; appeal is allowed at most once.
CREATE TABLE reports (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id        BIGINT NOT NULL REFERENCES communities(id),
    content_id          BIGINT NOT NULL REFERENCES contents(id),
    version_id          BIGINT NOT NULL REFERENCES content_versions(id),
    reporter_id         BIGINT NOT NULL REFERENCES users(id),
    category            TEXT NOT NULL,
    reason              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'accepted'
                          CHECK (status IN ('accepted','upheld','dismissed',
                                            'appeal_upheld','appeal_dismissed')),
    appealed            BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_reports_community ON reports (community_id, status);

CREATE TABLE report_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id       BIGINT NOT NULL REFERENCES reports(id),
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    actor_id        BIGINT NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL CHECK (action IN
                      ('create','accept','uphold','dismiss','appeal','appeal_uphold',
                       'appeal_dismiss','restore')),
    basis           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_report_events ON report_events (report_id);

-- Courses: draft structure lives in course_modules / course_lessons.
CREATE TABLE courses (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    author_id       BIGINT NOT NULL REFERENCES users(id),
    title           TEXT NOT NULL,
    required_level  INT NOT NULL DEFAULT 1 CHECK (required_level BETWEEN 1 AND 10),
    published       BOOLEAN NOT NULL DEFAULT FALSE,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_courses_community ON courses (community_id, published);

CREATE TABLE course_modules (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    course_id       BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    position        INT NOT NULL,
    title           TEXT NOT NULL,
    UNIQUE (course_id, position)
);
CREATE TABLE course_lessons (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    module_id       BIGINT NOT NULL REFERENCES course_modules(id) ON DELETE CASCADE,
    position        INT NOT NULL,
    title           TEXT NOT NULL,
    -- draft reference; NULL for a placeholder lesson
    content_version_id BIGINT REFERENCES content_versions(id),
    UNIQUE (module_id, position)
);

-- Frozen snapshots taken at publish time. Republishing replaces these rows.
CREATE TABLE course_publishes (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    course_id       BIGINT NOT NULL REFERENCES courses(id),
    community_id    BIGINT NOT NULL REFERENCES communities(id),
    published_by    BIGINT NOT NULL REFERENCES users(id),
    published_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE course_publish_modules (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publish_id      BIGINT NOT NULL REFERENCES course_publishes(id) ON DELETE CASCADE,
    position        INT NOT NULL,
    title           TEXT NOT NULL
);
CREATE TABLE course_publish_lessons (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    publish_module_id BIGINT NOT NULL REFERENCES course_publish_modules(id) ON DELETE CASCADE,
    position        INT NOT NULL,
    title           TEXT NOT NULL,
    -- Frozen content version. No FK ON UPDATE: versions are immutable.
    -- If the referenced version/content is later deleted it stays intact;
    -- access is still gated by the version's current published/delisted state.
    content_version_id BIGINT NOT NULL REFERENCES content_versions(id),
    content_id      BIGINT NOT NULL REFERENCES contents(id),
    required_level  INT NOT NULL
);
CREATE INDEX idx_publish_lessons_content ON course_publish_lessons (content_version_id);

ALTER TABLE contents
    ADD CONSTRAINT fk_contents_current_version
        FOREIGN KEY (current_version_id) REFERENCES content_versions(id),
    ADD CONSTRAINT fk_contents_published_version
        FOREIGN KEY (published_version_id) REFERENCES content_versions(id);

CREATE FUNCTION touch_updated_at() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END $$;
CREATE TRIGGER trg_subscriptions_touch BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_contents_touch BEFORE UPDATE ON contents
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_reports_touch BEFORE UPDATE ON reports
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER trg_courses_touch BEFORE UPDATE ON courses
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
