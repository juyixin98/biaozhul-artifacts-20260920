use crate::models::*;
use rusqlite::Connection;
use std::sync::Mutex;

pub struct Db {
    conn: Mutex<Connection>,
}

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    source_kind  TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    nonce        TEXT NOT NULL,
    vx           REAL NOT NULL,
    vy           REAL NOT NULL,
    omega        REAL NOT NULL,
    issue_ms     INTEGER NOT NULL,
    deadline_ms  INTEGER NOT NULL,
    received_ms  INTEGER NOT NULL,
    UNIQUE(source, nonce)
);

CREATE TABLE IF NOT EXISTS estop_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    nonce        TEXT NOT NULL,
    action       TEXT NOT NULL,
    received_ms  INTEGER NOT NULL,
    UNIQUE(source, nonce)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ms  INTEGER NOT NULL,
    endpoint     TEXT NOT NULL,
    source       TEXT,
    seq          INTEGER,
    nonce        TEXT,
    accepted     INTEGER NOT NULL,
    reject_reason TEXT,
    detail       TEXT,
    raw_body     TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms         INTEGER NOT NULL,
    trigger       TEXT NOT NULL,
    estop_latched INTEGER NOT NULL,
    chosen_source TEXT,
    chosen_reason TEXT,
    vx            REAL,
    vy            REAL,
    omega         REAL,
    json          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions(at_ms);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"#;

impl Db {
    pub fn open(path: &str) -> Result<Self, String> {
        let conn = Connection::open(path).map_err(|e| format!("打开数据库 {path} 失败: {e}"))?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;",
        )
        .map_err(|e| e.to_string())?;
        conn.execute_batch(SCHEMA).map_err(|e| e.to_string())?;
        Ok(Db {
            conn: Mutex::new(conn),
        })
    }

    pub fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().expect("db mutex poisoned")
    }

    // ---------- meta（手动时钟持久化，用于“重启”验收） ----------

    pub fn get_meta(&self, key: &str) -> Result<Option<String>, String> {
        let conn = self.lock();
        conn.query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0))
            .map(Some)
            .or_else(|e| match e {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other.to_string()),
            })
    }

    pub fn set_meta(&self, key: &str, value: &str) -> Result<(), String> {
        let conn = self.lock();
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?1, ?2)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            rusqlite::params![key, value],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    }

    // ---------- 序号 / nonce 防重放 ----------

    pub fn max_command_seq(&self, source: &str) -> Result<i64, String> {
        let conn = self.lock();
        conn.query_row(
            "SELECT COALESCE(MAX(seq), -1) FROM commands WHERE source = ?1",
            [source],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())
    }

    pub fn max_estop_seq(&self, source: &str) -> Result<i64, String> {
        let conn = self.lock();
        conn.query_row(
            "SELECT COALESCE(MAX(seq), -1) FROM estop_events WHERE source = ?1",
            [source],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())
    }

    pub fn command_nonce_exists(&self, source: &str, nonce: &str) -> Result<bool, String> {
        let conn = self.lock();
        conn.query_row(
            "SELECT 1 FROM commands WHERE source = ?1 AND nonce = ?2",
            rusqlite::params![source, nonce],
            |_| Ok(()),
        )
        .map(|_| true)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            other => Err(other.to_string()),
        })
    }

    pub fn estop_nonce_exists(&self, source: &str, nonce: &str) -> Result<bool, String> {
        let conn = self.lock();
        conn.query_row(
            "SELECT 1 FROM estop_events WHERE source = ?1 AND nonce = ?2",
            rusqlite::params![source, nonce],
            |_| Ok(()),
        )
        .map(|_| true)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(false),
            other => Err(other.to_string()),
        })
    }

    // ---------- 写入 ----------

    #[allow(clippy::too_many_arguments)]
    pub fn insert_command(&self, c: &Command) -> Result<(), String> {
        let conn = self.lock();
        conn.execute(
            "INSERT INTO commands
                (source, source_kind, seq, nonce, vx, vy, omega, issue_ms, deadline_ms, received_ms)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            rusqlite::params![
                c.source,
                c.source_kind,
                c.seq,
                c.nonce,
                c.vx,
                c.vy,
                c.omega,
                c.issue_ms,
                c.deadline_ms,
                c.received_ms,
            ],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    }

    pub fn insert_estop_event(&self, e: &EstopEvent, nonce: &str) -> Result<(), String> {
        let conn = self.lock();
        conn.execute(
            "INSERT INTO estop_events (source, seq, nonce, action, received_ms)
             VALUES (?1,?2,?3,?4,?5)",
            rusqlite::params![e.source, e.seq, nonce, e.action, e.received_ms],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn log_ingest(
        &self,
        received_ms: i64,
        endpoint: &str,
        source: Option<&str>,
        seq: Option<i64>,
        nonce: Option<&str>,
        accepted: bool,
        reject_reason: Option<&str>,
        detail: Option<&str>,
        raw_body: &str,
    ) -> Result<(), String> {
        let conn = self.lock();
        conn.execute(
            "INSERT INTO ingest_log
                (received_ms, endpoint, source, seq, nonce, accepted, reject_reason, detail, raw_body)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            rusqlite::params![
                received_ms,
                endpoint,
                source,
                seq,
                nonce,
                accepted as i64,
                reject_reason,
                detail,
                raw_body,
            ],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    }

    pub fn last_estop_action(&self) -> Result<Option<String>, String> {
        let conn = self.lock();
        conn.query_row(
            "SELECT action FROM estop_events ORDER BY id DESC LIMIT 1",
            [],
            |r| r.get(0),
        )
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other.to_string()),
        })
    }

    // ---------- 读取世界视图（仲裁输入） ----------

    pub fn load_world(&self) -> Result<WorldView, String> {
        let conn = self.lock();
        let mut commands = Vec::new();
        let mut stmt = conn
            .prepare(
                "SELECT source, source_kind, seq, nonce, vx, vy, omega, issue_ms, deadline_ms, received_ms
                 FROM commands ORDER BY id",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok(Command {
                    source: r.get(0)?,
                    source_kind: r.get(1)?,
                    seq: r.get(2)?,
                    nonce: r.get(3)?,
                    vx: r.get(4)?,
                    vy: r.get(5)?,
                    omega: r.get(6)?,
                    issue_ms: r.get(7)?,
                    deadline_ms: r.get(8)?,
                    received_ms: r.get(9)?,
                })
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            commands.push(row.map_err(|e| e.to_string())?);
        }

        let mut estop_events = Vec::new();
        let mut stmt = conn
            .prepare("SELECT source, seq, action, received_ms FROM estop_events ORDER BY id")
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([], |r| {
                Ok(EstopEvent {
                    source: r.get(0)?,
                    seq: r.get(1)?,
                    action: r.get(2)?,
                    received_ms: r.get(3)?,
                })
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            estop_events.push(row.map_err(|e| e.to_string())?);
        }
        Ok(WorldView {
            commands,
            estop_events,
        })
    }

    // ---------- 决策记录 ----------

    pub fn insert_decision(
        &self,
        at_ms: i64,
        trigger: &str,
        d: &DecisionOutcome,
    ) -> Result<i64, String> {
        let json = serde_json::to_string(d).map_err(|e| e.to_string())?;
        let (src, reason, vx, vy, omega) = match &d.chosen {
            Some(c) => (
                Some(c.source.clone()),
                Some(c.reason.clone()),
                Some(c.vx),
                Some(c.vy),
                Some(c.omega),
            ),
            None => (None, None, None, None, None),
        };
        let conn = self.lock();
        conn.execute(
            "INSERT INTO decisions
                (at_ms, trigger, estop_latched, chosen_source, chosen_reason, vx, vy, omega, json)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            rusqlite::params![
                at_ms,
                trigger,
                d.estop_latched as i64,
                src,
                reason,
                vx,
                vy,
                omega,
                json,
            ],
        )
        .map_err(|e| e.to_string())?;
        Ok(conn.last_insert_rowid())
    }

    pub fn latest_decision(&self) -> Result<Option<(i64, String)>, String> {
        let conn = self.lock();
        conn.query_row(
            "SELECT id, json FROM decisions ORDER BY id DESC LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other.to_string()),
        })
    }

    /// 历史决策（纯查询，绝不写库、绝不刷租约）。
    pub fn decision_history(&self, limit: i64) -> Result<Vec<serde_json::Value>, String> {
        let conn = self.lock();
        let mut stmt = conn
            .prepare(
                "SELECT json FROM (
                    SELECT id, json FROM decisions ORDER BY id DESC LIMIT ?1
                 ) ORDER BY id ASC",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([limit], |r| r.get::<_, String>(0))
            .map_err(|e| e.to_string())?;
        let mut out = Vec::new();
        for row in rows {
            let s = row.map_err(|e| e.to_string())?;
            out.push(serde_json::from_str(&s).map_err(|e| e.to_string())?);
        }
        Ok(out)
    }

    pub fn count_decisions(&self) -> Result<i64, String> {
        let conn = self.lock();
        conn.query_row("SELECT COUNT(*) FROM decisions", [], |r| r.get(0))
            .map_err(|e| e.to_string())
    }
}
