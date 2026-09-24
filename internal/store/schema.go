package store

const schema = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_configs (
    device_type TEXT PRIMARY KEY,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    epoch       INTEGER NOT NULL,
    seq         INTEGER,                 -- NULL for heartbeats
    value       TEXT,                    -- textual value; NULL for heartbeats
    sampled_at  TEXT NOT NULL,
    received_at TEXT NOT NULL,
    is_heartbeat INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_obs_dev_epoch_seq
    ON observations(device_id, epoch, seq);
CREATE INDEX IF NOT EXISTS idx_obs_received
    ON observations(received_at);

-- One row per device: mutable per-rule state, reconstructed after restart.
CREATE TABLE IF NOT EXISTS device_state (
    device_id              TEXT PRIMARY KEY REFERENCES devices(id),
    epoch                  INTEGER NOT NULL DEFAULT 0,
    high_seq               INTEGER NOT NULL DEFAULT 0,
    last_sample_sampled_at TEXT,
    last_sample_received_at TEXT,
    last_any_received_at   TEXT NOT NULL,
    stale_state  TEXT NOT NULL DEFAULT '{}',
    frozen_state TEXT NOT NULL DEFAULT '{}',
    gap_state    TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      TEXT NOT NULL REFERENCES devices(id),
    device_type    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    status         TEXT NOT NULL,
    config_version INTEGER NOT NULL,
    epoch          INTEGER NOT NULL,
    trig_seq_start INTEGER NOT NULL,
    trig_seq_end   INTEGER NOT NULL,
    trig_first_at  TEXT NOT NULL,
    trig_last_at   TEXT NOT NULL,
    rec_seq_start  INTEGER,
    rec_seq_end    INTEGER,
    rec_first_at   TEXT,
    rec_last_at    TEXT,
    opened_at      TEXT NOT NULL,
    recovered_at   TEXT,
    detail         TEXT NOT NULL DEFAULT ''
);
-- At most one OPEN alert per (device, kind). Recovered alerts remain as
-- history and are not constrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_one_open
    ON alerts(device_id, kind) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status);
`
