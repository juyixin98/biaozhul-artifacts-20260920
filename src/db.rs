//! SQLite persistence for immutable pool snapshots.
//!
//! Snapshots are append-only: inserting a snapshot stores its assets and
//! pools verbatim (amounts as decimal text, so the full u128 range survives
//! SQLite's 64-bit integer affinity). A real SHA-256 content hash over the
//! canonical payload is stored alongside every snapshot and returned with
//! each quote, so a caller can prove which exact reserves/fees a quote was
//! computed against.

use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;

use crate::model::{Asset, Pool, Snapshot};

pub struct Store {
    conn: Mutex<Connection>,
}

#[derive(Debug)]
pub enum StoreError {
    Sql(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let StoreError::Sql(msg) = self;
        write!(f, "storage error: {msg}")
    }
}

impl std::error::Error for StoreError {}

fn map_err(e: rusqlite::Error) -> StoreError {
    StoreError::Sql(e.to_string())
}

impl Store {
    pub fn open(path: &str) -> Result<Self, StoreError> {
        let conn = Connection::open(path).map_err(map_err)?;
        conn.pragma_update(None, "journal_mode", "WAL").ok();
        conn.pragma_update(None, "foreign_keys", "ON").map_err(map_err)?;
        conn.execute_batch(SCHEMA).map_err(map_err)?;
        Ok(Store {
            conn: Mutex::new(conn),
        })
    }

    /// Compute the SHA-256 content hash of a validated payload.
    ///
    /// Canonical byte form (deterministic by construction):
    /// * assets and pools sorted ascending by id;
    /// * serialized directly from the structs below, so field order is the
    ///   declared order (not alphabetical — `serde_json::json!` would sort
    ///   object keys via BTreeMap);
    /// * compact JSON, no whitespace.
    pub fn content_hash(assets: &[Asset], pools: &[Pool]) -> String {
        #[derive(serde::Serialize)]
        struct AHash<'a> {
            id: &'a str,
            decimals: u8,
        }
        #[derive(serde::Serialize)]
        struct PHash<'a> {
            id: &'a str,
            token0: &'a str,
            token1: &'a str,
            reserve0: String,
            reserve1: String,
            fee_bps: u32,
        }
        #[derive(serde::Serialize)]
        struct Canonical<'a> {
            assets: Vec<AHash<'a>>,
            pools: Vec<PHash<'a>>,
        }
        let mut a: Vec<AHash> = assets
            .iter()
            .map(|x| AHash {
                id: &x.id,
                decimals: x.decimals,
            })
            .collect();
        a.sort_by(|x, y| x.id.cmp(y.id));
        let mut p: Vec<PHash> = pools
            .iter()
            .map(|x| PHash {
                id: &x.id,
                token0: &x.token0,
                token1: &x.token1,
                reserve0: x.reserve0.to_string(),
                reserve1: x.reserve1.to_string(),
                fee_bps: x.fee_bps,
            })
            .collect();
        p.sort_by(|x, y| x.id.cmp(y.id));

        let bytes =
            serde_json::to_vec(&Canonical { assets: a, pools: p }).expect("canonical serializes");
        let mut hasher = Sha256::new();
        hasher.update(b"swap-router-snapshot-v1\n");
        hasher.update(&bytes);
        hex::encode(hasher.finalize())
    }

    pub async fn insert_snapshot(
        &self,
        assets: &[Asset],
        pools: &[Pool],
        created_at: &str,
        content_hash: &str,
    ) -> Result<i64, StoreError> {
        let mut conn = self.conn.lock().await;
        let tx = conn.transaction().map_err(map_err)?;
        tx.execute(
            "INSERT INTO snapshots (created_at, content_hash) VALUES (?1, ?2)",
            params![created_at, content_hash],
        )
        .map_err(map_err)?;
        let id = tx.last_insert_rowid();

        {
            let mut stmt = tx
                .prepare(
                    "INSERT INTO assets (snapshot_id, id, decimals) VALUES (?1, ?2, ?3)",
                )
                .map_err(map_err)?;
            for a in assets {
                stmt.execute(params![id, a.id, a.decimals as u32])
                    .map_err(map_err)?;
            }
        }
        {
            let mut stmt = tx
                .prepare(
                    "INSERT INTO pools (snapshot_id, id, token0, token1, reserve0, reserve1, fee_bps)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                )
                .map_err(map_err)?;
            for p in pools {
                stmt.execute(params![
                    id,
                    p.id,
                    p.token0,
                    p.token1,
                    p.reserve0.to_string(),
                    p.reserve1.to_string(),
                    p.fee_bps
                ])
                .map_err(map_err)?;
            }
        }
        tx.commit().map_err(map_err)?;
        Ok(id)
    }

    pub async fn load_snapshot(
        &self,
        id: Option<i64>,
    ) -> Result<Option<Snapshot>, StoreError> {
        let conn = self.conn.lock().await;

        let snap_id = match id {
            Some(v) => v,
            None => {
                match conn
                    .query_row(
                        "SELECT id FROM snapshots ORDER BY id DESC LIMIT 1",
                        [],
                        |row| row.get::<_, i64>(0),
                    ) {
                    Ok(v) => v,
                    Err(rusqlite::Error::QueryReturnedNoRows) => return Ok(None),
                    Err(e) => return Err(map_err(e)),
                }
            }
        };

        let (created_at, content_hash) = match conn.query_row(
            "SELECT created_at, content_hash FROM snapshots WHERE id = ?1",
            params![snap_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        ) {
            Ok(v) => v,
            Err(rusqlite::Error::QueryReturnedNoRows) => return Ok(None),
            Err(e) => return Err(map_err(e)),
        };

        let mut assets = Vec::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT id, decimals FROM assets WHERE snapshot_id = ?1 ORDER BY id",
                )
                .map_err(map_err)?;
            let rows = stmt
                .query_map(params![snap_id], |row| {
                    Ok(Asset {
                        id: row.get(0)?,
                        decimals: row.get::<_, u32>(1)? as u8,
                    })
                })
                .map_err(map_err)?;
            for r in rows {
                assets.push(r.map_err(map_err)?);
            }
        }

        let mut pools = Vec::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT id, token0, token1, reserve0, reserve1, fee_bps
                     FROM pools WHERE snapshot_id = ?1 ORDER BY id",
                )
                .map_err(map_err)?;
            let rows = stmt
                .query_map(params![snap_id], |row| {
                    let r0s: String = row.get(3)?;
                    let r1s: String = row.get(4)?;
                    Ok(Pool {
                        id: row.get(0)?,
                        token0: row.get(1)?,
                        token1: row.get(2)?,
                        reserve0: r0s.parse().map_err(|_| {
                            rusqlite::Error::FromSqlConversionFailure(
                                3,
                                rusqlite::types::Type::Text,
                                Box::new(StoreError::Sql(format!("bad reserve: {r0s}"))),
                            )
                        })?,
                        reserve1: r1s.parse().map_err(|_| {
                            rusqlite::Error::FromSqlConversionFailure(
                                4,
                                rusqlite::types::Type::Text,
                                Box::new(StoreError::Sql(format!("bad reserve: {r1s}"))),
                            )
                        })?,
                        fee_bps: row.get(5)?,
                    })
                })
                .map_err(map_err)?;
            for r in rows {
                pools.push(r.map_err(map_err)?);
            }
        }

        Ok(Some(Snapshot {
            id: snap_id,
            content_hash,
            created_at,
            assets,
            pools,
        }))
    }
}

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    content_hash TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    id          TEXT    NOT NULL,
    decimals    INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, id)
);

CREATE TABLE IF NOT EXISTS pools (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    id          TEXT    NOT NULL,
    token0      TEXT    NOT NULL,
    token1      TEXT    NOT NULL,
    reserve0    TEXT    NOT NULL,
    reserve1    TEXT    NOT NULL,
    fee_bps     INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, id)
);
"#;

/// UTC timestamp in RFC3339 seconds, no extra dependency.
pub fn now_rfc3339() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format_rfc3339(secs)
}

fn format_rfc3339(secs: u64) -> String {
    let days = secs / 86_400;
    let rem = secs % 86_400;
    let hour = rem / 3600;
    let min = (rem % 3600) / 60;
    let sec = rem % 60;
    let (y, m, d) = civil_from_days(days as i64);
    format!("{y:04}-{m:02}-{d:02}T{hour:02}:{min:02}:{sec:02}Z")
}

/// Howard Hinnant's days-to-civil-date algorithm (proleptic Gregorian).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (y + if m <= 2 { 1 } else { 0 }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_epoch() {
        assert_eq!(format_rfc3339(0), "1970-01-01T00:00:00Z");
        assert_eq!(format_rfc3339(1_600_000_000), "2020-09-13T12:26:40Z");
    }

    #[tokio::test]
    async fn insert_and_load_roundtrip() {
        let dir = std::env::temp_dir().join(format!("swap-router-db-{}", std::process::id()));
        let path = dir.join("test.db");
        let _ = std::fs::remove_file(&path);
        std::fs::create_dir_all(&dir).unwrap();
        let store = Store::open(path.to_str().unwrap()).unwrap();

        let assets = vec![
            Asset { id: "A".into(), decimals: 18 },
            Asset { id: "B".into(), decimals: 6 },
        ];
        let pools = vec![Pool {
            id: "p1".into(),
            token0: "A".into(),
            token1: "B".into(),
            reserve0: 1_000_000_000_000_000_000,
            reserve1: 5_000_000,
            fee_bps: 30,
        }];
        let hash = Store::content_hash(&assets, &pools);
        let id = store
            .insert_snapshot(&assets, &pools, "2026-01-01T00:00:00Z", &hash)
            .await
            .unwrap();
        let snap = store.load_snapshot(Some(id)).await.unwrap().unwrap();
        assert_eq!(snap.id, id);
        assert_eq!(snap.content_hash, hash);
        assert_eq!(snap.assets.len(), 2);
        assert_eq!(snap.pools[0].reserve1, 5_000_000);

        // None selects the latest snapshot.
        let latest = store.load_snapshot(None).await.unwrap().unwrap();
        assert_eq!(latest.id, id);
        assert!(store.load_snapshot(Some(999)).await.unwrap().is_none());

        drop(store);
        let _ = std::fs::remove_dir_all(&dir);
    }
}

