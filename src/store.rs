use rusqlite::{params, Connection};
use serde::Serialize;
use std::path::Path;

/// One persisted task row.
pub struct TaskRow {
    pub name: String,
    pub counter: u64,
    pub last_progress_ms: u64,
    pub last_heartbeat_ms: u64,
}

/// Persisted device-level state.
pub struct DeviceRow {
    pub safe_mode: bool,
    pub fault_generation: u64,
    pub consecutive_resets: u32,
    pub last_reset_reason: Option<String>,
}

/// One entry in the durable reset journal.
#[derive(Debug, Clone, Serialize)]
pub struct ResetRecord {
    pub id: i64,
    pub at_ms: u64,
    pub fault_generation: u64,
    pub consecutive_resets: u32,
    pub reason: String,
    /// JSON snapshot of every task's last progress counters.
    pub task_snapshot: String,
}

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    safe_mode           INTEGER NOT NULL,
    fault_generation    INTEGER NOT NULL,
    consecutive_resets  INTEGER NOT NULL,
    last_reset_reason   TEXT
);
CREATE TABLE IF NOT EXISTS task_state (
    name                TEXT PRIMARY KEY,
    counter             INTEGER NOT NULL,
    last_progress_ms    INTEGER NOT NULL,
    last_heartbeat_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reset_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms               INTEGER NOT NULL,
    fault_generation    INTEGER NOT NULL,
    consecutive_resets  INTEGER NOT NULL,
    reason              TEXT NOT NULL,
    task_snapshot       TEXT NOT NULL
);
"#;

/// SQLite-backed durable store. All watchdog state survives process
/// restart; every schema version and write is real SQL.
pub struct Store {
    conn: Connection,
}

impl Store {
    pub fn open<P: AsRef<Path>>(path: P) -> crate::error::Result<Store> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(SCHEMA)?;
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '1')",
            [],
        )?;
        Ok(Store { conn })
    }

    /// Test/helper constructor using an in-memory database.
    pub fn open_memory() -> crate::error::Result<Store> {
        let conn = Connection::open_in_memory()?;
        conn.execute_batch(SCHEMA)?;
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('version', '1')",
            [],
        )?;
        Ok(Store { conn })
    }

    pub fn load_device(&self) -> crate::error::Result<Option<DeviceRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT safe_mode, fault_generation, consecutive_resets, last_reset_reason \
             FROM device_state WHERE id = 1",
        )?;
        let mut rows = stmt.query([])?;
        match rows.next()? {
            Some(r) => Ok(Some(DeviceRow {
                safe_mode: r.get::<_, i64>(0)? != 0,
                fault_generation: r.get::<_, i64>(1)? as u64,
                consecutive_resets: r.get::<_, i64>(2)? as u32,
                last_reset_reason: r.get(3)?,
            })),
            None => Ok(None),
        }
    }

    pub fn save_device(
        &self,
        safe_mode: bool,
        fault_generation: u64,
        consecutive_resets: u32,
        last_reset_reason: Option<&str>,
    ) -> crate::error::Result<()> {
        self.conn.execute(
            "INSERT INTO device_state \
               (id, safe_mode, fault_generation, consecutive_resets, last_reset_reason) \
             VALUES (1, ?1, ?2, ?3, ?4) \
             ON CONFLICT(id) DO UPDATE SET \
               safe_mode = excluded.safe_mode, \
               fault_generation = excluded.fault_generation, \
               consecutive_resets = excluded.consecutive_resets, \
               last_reset_reason = excluded.last_reset_reason",
            params![
                safe_mode as i64,
                fault_generation as i64,
                consecutive_resets as i64,
                last_reset_reason
            ],
        )?;
        Ok(())
    }

    pub fn load_tasks(&self) -> crate::error::Result<Vec<TaskRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT name, counter, last_progress_ms, last_heartbeat_ms FROM task_state",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(TaskRow {
                name: r.get(0)?,
                counter: r.get::<_, i64>(1)? as u64,
                last_progress_ms: r.get::<_, i64>(2)? as u64,
                last_heartbeat_ms: r.get::<_, i64>(3)? as u64,
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }

    pub fn save_tasks(&self, tasks: &[TaskRow]) -> crate::error::Result<()> {
        let tx = self.conn.unchecked_transaction()?;
        {
            let mut stmt = tx.prepare(
                "INSERT INTO task_state \
                   (name, counter, last_progress_ms, last_heartbeat_ms) \
                 VALUES (?1, ?2, ?3, ?4) \
                 ON CONFLICT(name) DO UPDATE SET \
                   counter = excluded.counter, \
                   last_progress_ms = excluded.last_progress_ms, \
                   last_heartbeat_ms = excluded.last_heartbeat_ms",
            )?;
            for t in tasks {
                stmt.execute(params![
                    t.name,
                    t.counter as i64,
                    t.last_progress_ms as i64,
                    t.last_heartbeat_ms as i64
                ])?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn append_reset_log(
        &self,
        at_ms: u64,
        fault_generation: u64,
        consecutive_resets: u32,
        reason: &str,
        task_snapshot: &str,
    ) -> crate::error::Result<()> {
        self.conn.execute(
            "INSERT INTO reset_log \
               (at_ms, fault_generation, consecutive_resets, reason, task_snapshot) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                at_ms as i64,
                fault_generation as i64,
                consecutive_resets as i64,
                reason,
                task_snapshot
            ],
        )?;
        Ok(())
    }

    pub fn load_reset_log(&self) -> crate::error::Result<Vec<ResetRecord>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, at_ms, fault_generation, consecutive_resets, reason, task_snapshot \
             FROM reset_log ORDER BY id",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok(ResetRecord {
                id: r.get(0)?,
                at_ms: r.get::<_, i64>(1)? as u64,
                fault_generation: r.get::<_, i64>(2)? as u64,
                consecutive_resets: r.get::<_, i64>(3)? as u32,
                reason: r.get(4)?,
                task_snapshot: r.get(5)?,
            })
        })?;
        Ok(rows.collect::<Result<Vec<_>, _>>()?)
    }
}
