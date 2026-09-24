//! SQLite 持久化：全部可变状态写穿到数据库，重启后完整水合，用于验证重启行为。
//!
//! 写入在单连接上以事务进行（调用方持有仲裁器互斥锁），开启 WAL。

use rusqlite::{params, Connection};

use crate::model::{ClockMode, EstopState, Source, SourceState, StoredCommand};

pub struct Store {
    conn: Connection,
}

pub struct PersistedState {
    pub clock_mode: ClockMode,
    pub sim_now: i64,
    pub estop: EstopState,
    pub autonomous: SourceState,
    pub remote: SourceState,
    pub freshness_gate: i64,
    pub rc_takeover_start: i64,
}

impl Store {
    pub fn open(path: &str) -> rusqlite::Result<Store> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.pragma_update(None, "synchronous", "FULL")?;
        let mut store = Store { conn };
        store.init_schema()?;
        Ok(store)
    }

    /// 测试用内存库。
    #[cfg(test)]
    pub fn open_in_memory() -> rusqlite::Result<Store> {
        let conn = Connection::open_in_memory()?;
        let mut store = Store { conn };
        store.init_schema()?;
        Ok(store)
    }

    fn init_schema(&mut self) -> rusqlite::Result<()> {
        self.conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                clock_mode TEXT NOT NULL,
                sim_now INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS estop_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                latched INTEGER NOT NULL,
                seq INTEGER NOT NULL,
                issued_at INTEGER,
                latched_at INTEGER,
                reason TEXT
            );

            CREATE TABLE IF NOT EXISTS source_state (
                source TEXT PRIMARY KEY CHECK (source IN ('autonomous','remote')),
                last_seq INTEGER NOT NULL,
                command_json TEXT
            );

            CREATE TABLE IF NOT EXISTS gates (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                freshness_gate INTEGER NOT NULL,
                rc_takeover_start INTEGER NOT NULL
            );

            -- 决策记录：每次写入类请求（命令/急停/释放/显式 tick）产生一条。
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,           -- 服务器处理该请求的时刻
                kind TEXT NOT NULL,                    -- autonomous|remote|estop|release|evaluate
                accepted INTEGER NOT NULL,             -- 触发消息是否通过校验
                source TEXT,                           -- 触发来源（适用时）
                trigger_seq INTEGER,                   -- 触发消息序号
                effective INTEGER,                     -- 该消息本身是否生效（急停/释放/门控）
                decision_json TEXT NOT NULL            -- 完整 Decision
            );

            -- 被硬性拒绝的消息审计（旧序号、过期、签名错误等）。
            CREATE TABLE IF NOT EXISTS ingest_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                source TEXT,
                seq INTEGER,
                accepted INTEGER NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT
            );
            "#,
        )
    }

    pub fn conn(&self) -> &Connection {
        &self.conn
    }

    pub fn conn_mut(&mut self) -> &mut Connection {
        &mut self.conn
    }

    /// 首次启动写入初始状态；已存在时什么都不做。
    pub fn ensure_initial(&mut self, clock_mode: ClockMode, initial_sim_now: i64) -> rusqlite::Result<()> {
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(id, clock_mode, sim_now) VALUES (1, ?1, ?2)",
            params![clock_mode.as_str(), initial_sim_now],
        )?;
        self.conn.execute(
            "INSERT OR IGNORE INTO estop_state(id, latched, seq, issued_at, latched_at, reason)
             VALUES (1, 0, 0, NULL, NULL, NULL)",
            [],
        )?;
        for s in ["autonomous", "remote"] {
            self.conn.execute(
                "INSERT OR IGNORE INTO source_state(source, last_seq, command_json) VALUES (?1, 0, NULL)",
                params![s],
            )?;
        }
        self.conn.execute(
            "INSERT OR IGNORE INTO gates(id, freshness_gate, rc_takeover_start) VALUES (1, 0, 0)",
            [],
        )?;
        Ok(())
    }

    pub fn hydrate(&self) -> rusqlite::Result<PersistedState> {
        let (clock_mode, sim_now): (String, i64) = self.conn.query_row(
            "SELECT clock_mode, sim_now FROM meta WHERE id = 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        let clock_mode = match clock_mode.as_str() {
            "sim" => ClockMode::Sim,
            "wall" => ClockMode::Wall,
            other => panic!("unknown clock_mode in db: {other}"),
        };

        let estop = self
            .conn
            .query_row(
                "SELECT latched, seq, issued_at, latched_at, reason FROM estop_state WHERE id = 1",
                [],
                |row| {
                    Ok(EstopState {
                        latched: row.get::<_, i64>(0)? != 0,
                        seq: row.get(1)?,
                        issued_at: row.get(2)?,
                        latched_at: row.get(3)?,
                        reason: row.get(4)?,
                    })
                },
            )
            .unwrap_or_default();

        let load_source = |source: Source| -> rusqlite::Result<SourceState> {
            let (last_seq, json): (i64, Option<String>) = self.conn.query_row(
                "SELECT last_seq, command_json FROM source_state WHERE source = ?1",
                params![source.as_str()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )?;
            let command = json
                .map(|j| serde_json::from_str::<StoredCommand>(&j).expect("stored json valid"));
            Ok(SourceState { last_seq, command })
        };

        let (freshness_gate, rc_takeover_start) = self.conn.query_row(
            "SELECT freshness_gate, rc_takeover_start FROM gates WHERE id = 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;

        Ok(PersistedState {
            clock_mode,
            sim_now,
            estop,
            autonomous: load_source(Source::Autonomous)?,
            remote: load_source(Source::Remote)?,
            freshness_gate,
            rc_takeover_start,
        })
    }

    pub fn update_meta(tx: &Connection, sim_now: i64) -> rusqlite::Result<()> {
        tx.execute("UPDATE meta SET sim_now = ?1 WHERE id = 1", params![sim_now])?;
        Ok(())
    }

    pub fn update_estop(tx: &Connection, e: &EstopState) -> rusqlite::Result<()> {
        tx.execute(
            "UPDATE estop_state SET latched=?1, seq=?2, issued_at=?3, latched_at=?4, reason=?5 WHERE id=1",
            params![e.latched as i64, e.seq, e.issued_at, e.latched_at, e.reason],
        )?;
        Ok(())
    }

    pub fn update_source(tx: &Connection, source: Source, st: &SourceState) -> rusqlite::Result<()> {
        let json = st
            .command
            .as_ref()
            .map(|c| serde_json::to_string(c).expect("command serializes"));
        tx.execute(
            "UPDATE source_state SET last_seq=?1, command_json=?2 WHERE source=?3",
            params![st.last_seq, json, source.as_str()],
        )?;
        Ok(())
    }

    pub fn update_gates(tx: &Connection, freshness_gate: i64, rc_takeover_start: i64) -> rusqlite::Result<()> {
        tx.execute(
            "UPDATE gates SET freshness_gate=?1, rc_takeover_start=?2 WHERE id = 1",
            params![freshness_gate, rc_takeover_start],
        )?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn insert_decision(
        tx: &Connection,
        created_at: i64,
        kind: &str,
        accepted: bool,
        source: Option<&str>,
        trigger_seq: Option<i64>,
        effective: Option<bool>,
        decision_json: &str,
    ) -> rusqlite::Result<i64> {
        tx.execute(
            "INSERT INTO decisions(created_at, kind, accepted, source, trigger_seq, effective, decision_json)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![
                created_at,
                kind,
                accepted as i64,
                source,
                trigger_seq,
                effective.map(|b| b as i64),
                decision_json
            ],
        )?;
        Ok(tx.last_insert_rowid())
    }
}
