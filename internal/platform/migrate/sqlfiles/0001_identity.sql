-- DAMS schema: organizations, principals, sources
CREATE TABLE organizations (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id          BIGINT NOT NULL REFERENCES organizations(id),
    username        TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, username)
);

-- API keys authenticate a principal. role is org-scoped:
-- admin: full control inside the org; analyst: handle alerts of the org only;
-- auditor: read-only; collector: may ingest events.
CREATE TABLE api_keys (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key_hash        TEXT NOT NULL UNIQUE,
    key_prefix      TEXT NOT NULL,
    org_id          BIGINT NOT NULL REFERENCES organizations(id),
    name            TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin','analyst','auditor','collector')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ
);
CREATE INDEX idx_api_keys_org ON api_keys(org_id);

-- Monitored databases whose activity is reported to DAMS. DAMS never connects
-- to these: rows are created from inbound events (auto-register) or by admins.
CREATE TABLE sources (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id          BIGINT NOT NULL REFERENCES organizations(id),
    source_name     TEXT NOT NULL,
    db_type         TEXT NOT NULL DEFAULT 'postgresql',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, source_name)
);
