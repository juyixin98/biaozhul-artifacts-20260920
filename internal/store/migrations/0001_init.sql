-- Schema migrations for the compatibility gateway. Idempotent.

CREATE TABLE IF NOT EXISTS mappings (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    direction   TEXT NOT NULL CHECK (direction IN ('v1_v2', 'v2_v1')),
    version     INTEGER NOT NULL DEFAULT 1,
    is_active   BOOLEAN NOT NULL DEFAULT FALSE,
    spec_json   TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one active mapping per direction.
CREATE UNIQUE INDEX IF NOT EXISTS mappings_one_active_per_direction
    ON mappings (direction) WHERE is_active;

CREATE TABLE IF NOT EXISTS conversion_audit (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    direction       TEXT NOT NULL,
    mapping_name    TEXT NOT NULL,
    mapping_version INTEGER NOT NULL,
    record_id       TEXT NOT NULL,
    ok              BOOLEAN NOT NULL,
    error_code      TEXT NOT NULL DEFAULT '',
    error_field     TEXT NOT NULL DEFAULT '',
    carried_enum    INTEGER
);

-- Emit a notification whenever active mappings change; LISTENers reload.
CREATE OR REPLACE FUNCTION mapping_changed_notify() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('mappings_changed',
        json_build_object('direction', NEW.direction, 'name', NEW.name,
                          'version', NEW.version)::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_mapping_changed ON mappings;
CREATE TRIGGER trg_mapping_changed
    AFTER UPDATE OF is_active, version, spec_json OR INSERT ON mappings
    FOR EACH ROW
    WHEN (NEW.is_active)
    EXECUTE FUNCTION mapping_changed_notify();
