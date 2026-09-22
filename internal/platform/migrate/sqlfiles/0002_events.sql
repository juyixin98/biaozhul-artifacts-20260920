-- DAMS schema: ingested audit events
CREATE TABLE events (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id              BIGINT NOT NULL REFERENCES organizations(id),
    source_id           BIGINT NOT NULL REFERENCES sources(id),
    source_event_id     TEXT NOT NULL,
    db_user             TEXT NOT NULL,
    occurred_at         TIMESTAMPTZ NOT NULL,   -- event time (source clock), used by every rule
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    action_category     TEXT NOT NULL CHECK (action_category IN
                            ('select','insert','update','delete','ddl','grant','login','other')),
    schema_name         TEXT NOT NULL DEFAULT '',
    table_name          TEXT NOT NULL DEFAULT '',
    row_count           BIGINT NOT NULL DEFAULT 0,
    client_ip           TEXT NOT NULL DEFAULT '',
    sql_text            TEXT NOT NULL DEFAULT '',
    -- sha256 over the canonical payload of the event; same id with different
    -- content is a conflict and rolls the whole batch back.
    content_hash        TEXT NOT NULL,
    batch_id            UUID
);
-- Dedup/idempotency key: (org, source, source event id).
CREATE UNIQUE INDEX uq_events_org_source_seq
    ON events(org_id, source_id, source_event_id);
CREATE INDEX idx_events_org_user_time ON events(org_id, db_user, occurred_at);
CREATE INDEX idx_events_org_time ON events(org_id, occurred_at);
CREATE INDEX idx_events_org_table_time ON events(org_id, schema_name, table_name, occurred_at);

CREATE TABLE ingest_batches (
    id              UUID PRIMARY KEY,
    org_id          BIGINT NOT NULL REFERENCES organizations(id),
    source_id       BIGINT NOT NULL REFERENCES sources(id),
    received_count  INT NOT NULL,
    inserted_count  INT NOT NULL,
    duplicate_count INT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('accepted','conflict','rejected')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
