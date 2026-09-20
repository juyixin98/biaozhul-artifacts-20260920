-- Persistent due reminders. A reminder is bound to a specific due_version:
-- the same due version is reminded at most once, and a reschedule (new
-- due_version) can never produce a stale reminder for the old schedule.
CREATE TABLE reminders (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action_item_id  uuid NOT NULL REFERENCES action_items (id) ON DELETE CASCADE,
    incident_id     uuid NOT NULL REFERENCES incidents (id) ON DELETE CASCADE,
    due_version     integer NOT NULL,
    due_at          timestamptz NOT NULL,
    message         text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (action_item_id, due_version)
);
CREATE INDEX idx_reminders_incident ON reminders (incident_id, created_at);
CREATE INDEX idx_reminders_created ON reminders (created_at);
