//! SQLite persistence for snapshots.
//!
//! Snapshot JSON is stored verbatim in an `INSERT OR IGNORE`-style row keyed
//! by the content-derived sha256 id, so uploading the same liquidity twice is
//! idempotent. Bundled SQLite is used so no system library is required.

use std::sync::Mutex;

use rusqlite::{Connection, OptionalExtension};

use crate::model::Snapshot;

pub struct Store {
    conn: Mutex<Connection>,
}

#[derive(Debug)]
pub struct StoredSnapshot {
    pub id: String,
    pub snapshot: Snapshot,
}

impl Store {
    /// Open (creating tables if needed). `:memory:` is supported for tests.
    pub fn open(path: &str) -> rusqlite::Result<Self> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS snapshots (
                id          TEXT PRIMARY KEY,
                body        TEXT NOT NULL,
                created_at  INTEGER NOT NULL DEFAULT (unixepoch())
            );
            "#,
        )?;
        Ok(Store {
            conn: Mutex::new(conn),
        })
    }

    /// Insert a snapshot under its content id. Returns `false` (idempotently)
    /// when an identical snapshot was already present.
    pub fn put(&self, snap: &Snapshot) -> rusqlite::Result<(String, bool)> {
        let id = snap.id();
        let body = serde_json::to_string(snap).expect("snapshot serializes");
        let conn = self.conn.lock().expect("sqlite mutex poisoned");
        let inserted = conn.execute(
            "INSERT OR IGNORE INTO snapshots (id, body) VALUES (?1, ?2)",
            rusqlite::params![id, body],
        )?;
        Ok((id, inserted == 1))
    }

    pub fn get(&self, id: &str) -> rusqlite::Result<Option<StoredSnapshot>> {
        let conn = self.conn.lock().expect("sqlite mutex poisoned");
        let row: Option<String> = conn
            .query_row("SELECT body FROM snapshots WHERE id = ?1", rusqlite::params![id], |r| {
                r.get(0)
            })
            .optional()?;
        match row {
            None => Ok(None),
            Some(body) => {
                let snapshot: Snapshot =
                    serde_json::from_str(&body).expect("stored snapshot is valid JSON");
                Ok(Some(StoredSnapshot { id: id.to_string(), snapshot }))
            }
        }
    }

    pub fn list(&self) -> rusqlite::Result<Vec<String>> {
        let conn = self.conn.lock().expect("sqlite mutex poisoned");
        let mut stmt = conn.prepare("SELECT id FROM snapshots ORDER BY id")?;
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::amount::Amount;
    use crate::model::{Asset, Pool};

    fn sample() -> Snapshot {
        Snapshot {
            assets: vec![
                Asset { id: "A".into(), decimals: 18 },
                Asset { id: "B".into(), decimals: 6 },
            ],
            pools: vec![Pool {
                id: "p".into(),
                token0: "A".into(),
                token1: "B".into(),
                reserve0: Amount::from(1000u64),
                reserve1: Amount::from(2000u64),
                fee_bps: 30,
                cost_token0_out: Amount::ZERO,
                cost_token1_out: Amount::ZERO,
            }],
        }
    }

    #[test]
    fn put_get_and_idempotent() {
        let store = Store::open(":memory:").unwrap();
        let s = sample();
        let (id, created1) = store.put(&s).unwrap();
        assert!(created1);
        let (id2, created2) = store.put(&s).unwrap();
        assert_eq!(id, id2);
        assert!(!created2);

        let got = store.get(&id).unwrap().unwrap();
        assert_eq!(got.snapshot, s);
        assert!(store.get("deadbeef").unwrap().is_none());
        assert_eq!(store.list().unwrap(), vec![id]);
    }
}
