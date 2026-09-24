use rusqlite::Connection;

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS devices (
    device_id            TEXT PRIMARY KEY,
    status               TEXT NOT NULL,
    fault_generation     INTEGER NOT NULL DEFAULT 0,
    consecutive_resets   INTEGER NOT NULL DEFAULT 0,
    window_ms            INTEGER NOT NULL,
    reset_threshold      INTEGER NOT NULL,
    challenge_ttl_ms     INTEGER NOT NULL,
    window_started_ms    INTEGER NOT NULL,
    last_feed_ms         INTEGER,
    last_reset_reason    TEXT,
    last_reset_source    TEXT,
    last_reset_at_ms     INTEGER,
    last_progress_json   TEXT,
    total_feeds          INTEGER NOT NULL DEFAULT 0,
    total_resets         INTEGER NOT NULL DEFAULT 0,
    created_ms           INTEGER NOT NULL,
    operator_key         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    device_id              TEXT NOT NULL,
    task_id                TEXT NOT NULL,
    last_counter           INTEGER,
    last_report_ms         INTEGER,
    advanced_this_window   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, task_id)
);

CREATE TABLE IF NOT EXISTS reset_records (
    device_id                 TEXT NOT NULL,
    seq                       INTEGER NOT NULL,
    at_ms                     INTEGER NOT NULL,
    source                    TEXT NOT NULL,
    reason                    TEXT NOT NULL,
    boot_count                INTEGER,
    counted                   INTEGER NOT NULL DEFAULT 1,
    consecutive_after         INTEGER NOT NULL,
    fault_generation_after    INTEGER NOT NULL,
    PRIMARY KEY (device_id, seq)
);

CREATE TABLE IF NOT EXISTS boot_counts (
    device_id   TEXT PRIMARY KEY,
    boot_count  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS challenges (
    device_id    TEXT PRIMARY KEY,
    generation   INTEGER NOT NULL,
    nonce        TEXT NOT NULL,
    created_ms   INTEGER NOT NULL,
    expires_ms   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clearance_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    at_ms       INTEGER NOT NULL,
    generation  INTEGER NOT NULL,
    accepted    INTEGER NOT NULL,
    detail      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    at_ms       INTEGER NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reset_records_device_seq
    ON reset_records (device_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_log_device_id
    ON audit_log (device_id, id);
"#;

pub fn open(path: &str) -> rusqlite::Result<Connection> {
    let conn = Connection::open(path)?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    conn.pragma_update(None, "busy_timeout", 5_000i64)?;
    init(&conn)?;
    Ok(conn)
}

pub fn init(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(SCHEMA)
}
