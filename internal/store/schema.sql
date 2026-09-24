-- MQTT 会话重投递 — PostgreSQL schema
-- All tables live in one schema so the whole pipeline can be reasoned about
-- transactionally and inspected during acceptance testing.

BEGIN;

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    hmac_secret TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw transport-layer audit log: one row per successfully committed processing
-- attempt of an inbound PUBLISH. A redelivered QoS 1 message that arrives
-- *before* any commit (because ACK never went out) is NOT here; a duplicate
-- that arrives after a commit IS here with kind='dup'.
CREATE TABLE IF NOT EXISTS raw_messages (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id     TEXT NOT NULL,
    mqtt_topic    TEXT NOT NULL,
    packet_id     INTEGER,                  -- MQTT packet id; changes across redeliveries
    dup_flag      BOOLEAN NOT NULL,         -- MQTT DUP bit of this delivery
    retained_flag BOOLEAN NOT NULL,         -- MQTT RETAIN flag
    payload       JSONB,                    -- NULL for unparseable bytes
    payload_raw   BYTEA NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind          TEXT NOT NULL CHECK (kind IN ('snapshot','event','dup','quarantine'))
);

-- Business facts. The dedup key is the *business* identity of the event,
-- not anything from the MQTT transport:
--   (device_id, boot_gen, seq)
-- A device restart uses a fresh boot_gen, so seq counters never collide or
-- get confused across reboots.
CREATE TABLE IF NOT EXISTS events (
    device_id   TEXT NOT NULL,
    boot_gen    BIGINT NOT NULL,
    seq         BIGINT NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, boot_gen, seq)
);

-- One row per observed device boot generation.
CREATE TABLE IF NOT EXISTS device_boots (
    device_id   TEXT NOT NULL,
    boot_gen    BIGINT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seq    BIGINT NOT NULL DEFAULT 0,
    event_count BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, boot_gen)
);

-- Current online state of each device. Only updated for genuine business
-- events; retained snapshots and duplicate deliveries do NOT touch it.
CREATE TABLE IF NOT EXISTS device_state (
    device_id        TEXT PRIMARY KEY,
    current_boot_gen BIGINT NOT NULL,
    current_seq      BIGINT NOT NULL,
    last_value       DOUBLE PRECISION NOT NULL,
    last_event_at    TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Invalid payloads that were accepted from MQTT but rejected by the protocol.
-- They are ACKed (so the broker queue drains) and isolated here.
CREATE TABLE IF NOT EXISTS quarantine (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_hint   TEXT,
    mqtt_topic    TEXT NOT NULL,
    payload_raw   BYTEA NOT NULL,
    reason        TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Online-state advancement guard lives in the INSERT ... ON CONFLICT DO
-- UPDATE ... WHERE clause in ProcessEvent: an event whose boot_gen is
-- strictly older than the current boot is a no-op against device_state
-- (it is still stored in events), so a late packet after a device restart
-- never rewinds current_boot_gen / current_seq.

CREATE INDEX IF NOT EXISTS events_device_time_idx
    ON events (device_id, measured_at);

COMMIT;
