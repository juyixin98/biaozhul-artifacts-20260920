-- CommunityVault: content revision & moderation engine.

-- ---------- enums ----------
CREATE TYPE user_role AS ENUM ('member', 'moderator', 'admin');
CREATE TYPE content_status AS ENUM ('draft', 'pending', 'published', 'withdrawn');
CREATE TYPE moderation_state AS ENUM ('pending', 'claimed', 'approved', 'rejected', 'cancelled');

-- ---------- users ----------
CREATE TABLE users (
    id         BIGSERIAL PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    role       user_role NOT NULL DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A moderator only moderates the categories listed here.
CREATE TABLE moderator_scopes (
    user_id  BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT   NOT NULL,
    PRIMARY KEY (user_id, category)
);

-- ---------- versioned local sensitive-word rules ----------
CREATE TABLE rule_versions (
    id           BIGSERIAL PRIMARY KEY,
    version      INT NOT NULL UNIQUE,
    status       TEXT NOT NULL CHECK (status IN ('archived', 'active')),
    description  TEXT NOT NULL DEFAULT '',
    created_by   BIGINT REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one active rule version at any time.
CREATE UNIQUE INDEX rule_versions_single_active
    ON rule_versions ((1)) WHERE status = 'active';

CREATE TABLE rule_words (
    id              BIGSERIAL PRIMARY KEY,
    rule_version_id BIGINT NOT NULL REFERENCES rule_versions(id) ON DELETE CASCADE,
    word TEXT NOT NULL
);
CREATE UNIQUE INDEX rule_words_lower_idx
    ON rule_words (rule_version_id, lower(word));

-- ---------- content & immutable revisions ----------
CREATE TABLE contents (
    id                    BIGSERIAL PRIMARY KEY,
    category              TEXT NOT NULL,
    author_id             BIGINT NOT NULL REFERENCES users(id),
    title                 TEXT NOT NULL,
    status                content_status NOT NULL DEFAULT 'draft',
    current_revision_id   BIGINT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE content_revisions (
    id          BIGSERIAL PRIMARY KEY,
    content_id  BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_no INT    NOT NULL,
    body        TEXT   NOT NULL,
    edit_reason TEXT   NOT NULL DEFAULT '',
    created_by  BIGINT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (content_id, revision_no)
);

ALTER TABLE contents
    ADD CONSTRAINT contents_current_revision_fkey
    FOREIGN KEY (current_revision_id) REFERENCES content_revisions(id);

CREATE INDEX content_revisions_content_idx ON content_revisions (content_id, revision_no);
CREATE INDEX contents_author_idx ON contents (author_id);
CREATE INDEX contents_feed_idx ON contents (status, updated_at DESC, id DESC);

-- ---------- moderation evidence ----------
-- Every decision is bound to BOTH a concrete revision and a concrete rule version.
CREATE TABLE moderation_decisions (
    id              BIGSERIAL PRIMARY KEY,
    content_id      BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_id     BIGINT NOT NULL REFERENCES content_revisions(id),
    rule_version_id BIGINT NOT NULL REFERENCES rule_versions(id),
    state           moderation_state NOT NULL,
    moderator_id    BIGINT REFERENCES users(id),
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX moderation_decisions_content_idx ON moderation_decisions (content_id, revision_id);

-- Review queue tasks. One open task per content; a new revision invalidates the old one.
CREATE TABLE review_tasks (
    id               BIGSERIAL PRIMARY KEY,
    content_id       BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_id      BIGINT NOT NULL REFERENCES content_revisions(id),
    rule_version_id  BIGINT NOT NULL REFERENCES rule_versions(id),
    state            moderation_state NOT NULL DEFAULT 'pending',
    claimed_by       BIGINT REFERENCES users(id),
    claimed_at       TIMESTAMPTZ,
    claim_expires_at TIMESTAMPTZ,
    decided_by       BIGINT REFERENCES users(id),
    decision_reason  TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX review_tasks_open_idx
    ON review_tasks (state, claim_expires_at, created_at);
CREATE INDEX review_tasks_content_open_idx
    ON review_tasks (content_id) WHERE state IN ('pending', 'claimed');

-- Append-only status change audit log.
CREATE TABLE status_events (
    id              BIGSERIAL PRIMARY KEY,
    content_id      BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_id     BIGINT REFERENCES content_revisions(id),
    rule_version_id BIGINT REFERENCES rule_versions(id),
    from_status     content_status,
    to_status       content_status NOT NULL,
    actor_id        BIGINT REFERENCES users(id),
    actor_role      TEXT NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX status_events_content_idx ON status_events (content_id, id);

-- ---------- reports ----------
-- (reporter, content) is the natural key: duplicate reports never add a count.
CREATE TABLE reports (
    id          BIGSERIAL PRIMARY KEY,
    content_id  BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    reporter_id BIGINT NOT NULL REFERENCES users(id),
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (content_id, reporter_id)
);
CREATE INDEX reports_content_idx ON reports (content_id);

-- ---------- updated_at trigger ----------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_contents_updated     BEFORE UPDATE ON contents     FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_review_tasks_updated BEFORE UPDATE ON review_tasks FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_reports_updated      BEFORE UPDATE ON reports      FOR EACH ROW EXECUTE FUNCTION set_updated_at();
