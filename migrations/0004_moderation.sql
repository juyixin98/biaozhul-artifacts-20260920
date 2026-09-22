-- 0004_moderation.sql: reviews, audit trail, reports, claim queue
-- Every review decision is bound to a specific revision AND a specific
-- rule version. An old approval can never authorize a newer body.
CREATE TABLE reviews (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id      BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_id     BIGINT NOT NULL REFERENCES content_revisions(id) ON DELETE CASCADE,
    rule_version_id BIGINT NOT NULL REFERENCES rule_versions(id),
    reviewer_id     BIGINT NOT NULL REFERENCES users(id),
    decision        TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
    reason          TEXT NOT NULL DEFAULT '',
    matched_words   JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Review rows are evidence. Duplicate decisions on the same task/revision are
-- prevented by the claim guard (CompleteTaskWithGuard): a task completes at
-- most once. No unique index is placed on reviews because a revision can be
-- resubmitted after a rejection and reviewed again — the review trail must
-- retain every decision, each bound to its own rule version.
CREATE INDEX idx_reviews_revision ON reviews (revision_id);
CREATE INDEX idx_reviews_content ON reviews (content_id);

-- Append-only audit trail for every status change / moderator action.
CREATE TABLE status_events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    revision_id BIGINT REFERENCES content_revisions(id),
    actor_id   BIGINT REFERENCES users(id),
    event      TEXT NOT NULL CHECK (event IN (
        'created','submitted','approved','rejected','withdrawn','rolled_back','rule_checked')),
    from_status TEXT,
    to_status   TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_events_content ON status_events (content_id, id);

-- Reports are unique by (reporter, content): repeat reports by the same
-- user are ignored, never counted.
CREATE TABLE reports (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id BIGINT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    reporter_id BIGINT NOT NULL REFERENCES users(id),
    reason     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reporter_id, content_id)
);
CREATE INDEX idx_reports_content ON reports (content_id);

-- Queue items are derived when content enters 'pending'.
CREATE TABLE review_tasks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content_id  BIGINT NOT NULL UNIQUE REFERENCES contents(id) ON DELETE CASCADE,
    revision_id BIGINT NOT NULL REFERENCES content_revisions(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','claimed','done','cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tasks_open ON review_tasks (status, id) WHERE status IN ('open','claimed');

-- The live claim row. expires_at drives timeout recycling.
CREATE TABLE review_claims (
    task_id    BIGINT PRIMARY KEY REFERENCES review_tasks(id) ON DELETE CASCADE,
    claimant_id BIGINT NOT NULL REFERENCES users(id),
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_claims_expires ON review_claims (expires_at);
