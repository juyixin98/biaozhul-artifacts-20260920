//! SQLite storage: canonical chain, UTXO set and per-block undo records.
//!
//! All chain mutations (`connect_block`, `disconnect_blocks`) run inside a
//! single SQL transaction, so UTXO writes, undo records, tip movement and the
//! state root are all atomic: a crash (or a failed validation) never leaves a
//! half-applied block behind (WAL journal restores the previous snapshot).

use std::sync::Mutex;

use rusqlite::types::Value as SqlValue;
use rusqlite::Connection;

use crate::error::Result;
use crate::hash::Hash32;

pub const GENESIS_HEIGHT: i64 = 0;

/// One unspent output (the only state of a UTXO chain besides the tip).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UtxoEntry {
    pub txid: Hash32,
    pub vout: u32,
    pub value: i64,
    pub address: String,
    /// Height at which the output was created.
    pub created_height: i64,
}

pub struct Db {
    pub conn: Mutex<Connection>,
}

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    tip_height  INTEGER NOT NULL,
    tip_hash    BLOB    NOT NULL,
    state_root  BLOB    NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    height       INTEGER PRIMARY KEY,
    hash         BLOB NOT NULL UNIQUE,
    prev_hash    BLOB NOT NULL,
    merkle_root  BLOB NOT NULL,
    timestamp    INTEGER NOT NULL,
    block_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utxos (
    txid           BLOB NOT NULL,
    vout           INTEGER NOT NULL,
    value          INTEGER NOT NULL,
    address        TEXT NOT NULL,
    created_height INTEGER NOT NULL,
    spent_height   INTEGER,
    PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS idx_utxos_live
    ON utxos(spent_height, created_height, txid, vout);

CREATE TABLE IF NOT EXISTS undo_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    height          INTEGER NOT NULL,
    undo_type       INTEGER NOT NULL,   -- 1 = spend (revive), 2 = create (delete)
    txid            BLOB NOT NULL,
    vout            INTEGER NOT NULL,
    value           INTEGER,
    address         TEXT,
    created_height  INTEGER,
    prev_spent      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_undo_height ON undo_records(height, id);
"#;

impl Db {
    /// Open (creating if needed) and seed the empty genesis chain.
    pub fn open(path: &str) -> Result<Self> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.pragma_update(None, "synchronous", "FULL")?;
        conn.execute_batch(SCHEMA)?;

        let exists: i64 =
            conn.query_row("SELECT COUNT(*) FROM meta", [], |r| r.get(0))?;
        if exists == 0 {
            let root = state_root_at_locked(&conn, GENESIS_HEIGHT)?;
            conn.execute(
                "INSERT INTO blocks(height, hash, prev_hash, merkle_root, timestamp, block_json)
                 VALUES (0, ?1, ?2, ?3, 0, ?4)",
                rusqlite::params![
                    Hash32::ZERO.as_bytes().to_vec(),
                    Hash32::ZERO.as_bytes().to_vec(),
                    Hash32::ZERO.as_bytes().to_vec(),
                    "{\"genesis\":true}",
                ],
            )?;
            conn.execute(
                "INSERT INTO meta(id, tip_height, tip_hash, state_root)
                 VALUES (1, 0, ?1, ?2)",
                rusqlite::params![
                    Hash32::ZERO.as_bytes().to_vec(),
                    root.as_bytes().to_vec()
                ],
            )?;
        }
        Ok(Db {
            conn: Mutex::new(conn),
        })
    }

    pub fn in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.execute_batch(SCHEMA)?;
        let root = state_root_at_locked(&conn, GENESIS_HEIGHT)?;
        conn.execute(
            "INSERT INTO blocks(height, hash, prev_hash, merkle_root, timestamp, block_json)
             VALUES (0, ?1, ?2, ?3, 0, ?4)",
            rusqlite::params![
                Hash32::ZERO.as_bytes().to_vec(),
                Hash32::ZERO.as_bytes().to_vec(),
                Hash32::ZERO.as_bytes().to_vec(),
                "{\"genesis\":true}",
            ],
        )?;
        conn.execute(
            "INSERT INTO meta(id, tip_height, tip_hash, state_root)
             VALUES (1, 0, ?1, ?2)",
            rusqlite::params![
                Hash32::ZERO.as_bytes().to_vec(),
                root.as_bytes().to_vec()
            ],
        )?;
        Ok(Db {
            conn: Mutex::new(conn),
        })
    }

    pub fn tip(&self) -> Result<(i64, Hash32)> {
        let conn = self.conn.lock().unwrap();
        let (h, hash): (i64, Vec<u8>) =
            conn.query_row("SELECT tip_height, tip_hash FROM meta", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?;
        Ok((h, to_hash(&hash)))
    }

    pub fn state_root_now(&self) -> Result<Hash32> {
        let conn = self.conn.lock().unwrap();
        let root: Vec<u8> =
            conn.query_row("SELECT state_root FROM meta", [], |r| r.get(0))?;
        Ok(to_hash(&root))
    }

    pub fn block_hash_at(&self, height: i64) -> Result<Option<Hash32>> {
        let conn = self.conn.lock().unwrap();
        block_hash_at_locked(&conn, height)
    }

    pub fn block_json_at(&self, height: i64) -> Result<Option<String>> {
        let conn = self.conn.lock().unwrap();
        let mut stmt =
            conn.prepare("SELECT block_json FROM blocks WHERE height = ?1")?;
        let mut rows = stmt.query(rusqlite::params![height])?;
        match rows.next()? {
            Some(r) => Ok(Some(r.get(0)?)),
            None => Ok(None),
        }
    }
}

pub(crate) fn to_hash(v: &[u8]) -> Hash32 {
    let mut a = [0u8; 32];
    assert_eq!(v.len(), 32, "hash column must be 32 bytes");
    a.copy_from_slice(v);
    Hash32(a)
}

pub(crate) fn hash_param(h: &Hash32) -> Vec<u8> {
    h.as_bytes().to_vec()
}

/// Read one UTXO as it existed *at* `height`:
/// - live now (`spent_height IS NULL`) and created at or before `height`, or
/// - spent later (`spent_height > height`) and created at or before `height`.
///
/// The height predicate is mandatory at every call site — historical queries
/// are answered from historical state, never by echoing the current set.
pub fn utxo_at_locked(
    conn: &Connection,
    txid: &Hash32,
    vout: u32,
    height: i64,
) -> Result<Option<UtxoEntry>> {
    let mut stmt = conn.prepare(
        "SELECT value, address, created_height
           FROM utxos
          WHERE txid = ?1 AND vout = ?2
            AND created_height <= ?3
            AND (spent_height IS NULL OR spent_height > ?3)",
    )?;
    let mut rows = stmt.query(rusqlite::params![
        txid.as_bytes().to_vec(),
        vout,
        height
    ])?;
    match rows.next()? {
        Some(r) => Ok(Some(UtxoEntry {
            txid: *txid,
            vout,
            value: r.get(0)?,
            address: r.get(1)?,
            created_height: r.get(2)?,
        })),
        None => Ok(None),
    }
}

pub fn block_hash_at_locked(conn: &Connection, height: i64) -> Result<Option<Hash32>> {
    let mut stmt = conn.prepare("SELECT hash FROM blocks WHERE height = ?1")?;
    let mut rows = stmt.query(rusqlite::params![height])?;
    match rows.next()? {
        Some(r) => {
            let v: Vec<u8> = r.get(0)?;
            Ok(Some(to_hash(&v)))
        }
        None => Ok(None),
    }
}

/// Canonical encoding of one UTXO set entry. This byte layout is part of the
/// state-root protocol and is shared by the SQL implementation and the naive
/// in-memory replay so they can be compared bit-for-bit.
pub fn encode_state_entry(
    buf: &mut Vec<u8>,
    txid: &Hash32,
    vout: u32,
    value: i64,
    address: &str,
) {
    buf.extend_from_slice(txid.as_bytes());
    buf.extend_from_slice(&vout.to_be_bytes());
    buf.extend_from_slice(&value.to_be_bytes());
    let ab = address.as_bytes();
    let len = u16::try_from(ab.len()).expect("address <= 256 bytes");
    buf.extend_from_slice(&len.to_be_bytes());
    buf.extend_from_slice(ab);
}

/// State root over entries already sorted by (txid, vout).
/// `prefix || encode(e1) || …` fed through double-SHA256.
pub fn state_root_from_sorted<'a, I>(entries: I) -> Hash32
where
    I: IntoIterator<Item = (Hash32, u32, i64, &'a str)>,
{
    use sha2::Digest;
    let mut hasher = sha2::Sha256::new();
    hasher.update(b"utxo-state:");
    let mut buf = Vec::new();
    for (txid, vout, value, address) in entries {
        buf.clear();
        encode_state_entry(&mut buf, &txid, vout, value, address);
        hasher.update(&buf);
    }
    let first = hasher.finalize();
    let second = sha2::Sha256::digest(first);
    let mut out = [0u8; 32];
    out.copy_from_slice(&second);
    Hash32(out)
}

/// Compute the UTXO state root at an explicit historical height.
///
/// SQLite returns the qualifying rows in PRIMARY KEY order (txid bytes, vout)
/// after applying the created/spent height predicates — bytewise BLOB order
/// matches `Hash32`'s bytewise `Ord`.
pub fn state_root_at_locked(conn: &Connection, height: i64) -> Result<Hash32> {
    let mut stmt = conn.prepare(
        "SELECT txid, vout, value, address
           FROM utxos
          WHERE created_height <= ?1
            AND (spent_height IS NULL OR spent_height > ?1)
          ORDER BY txid, vout",
    )?;
    let rows: Vec<(Vec<u8>, i64, i64, String)> = stmt
        .query_map(rusqlite::params![height], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
        })?
        .collect::<std::result::Result<_, _>>()?;
    let entries = rows
        .iter()
        .map(|(t, v, val, a)| (to_hash(t), *v as u32, *val, a.as_str()));
    Ok(state_root_from_sorted(entries))
}

/// Convenience wrapper used by the fixture generator and tests.
pub fn state_root_at(db: &Db, height: i64) -> Result<Hash32> {
    let conn = db.conn.lock().unwrap();
    state_root_at_locked(&conn, height)
}

/// Bind a 32-byte hash column from a stored SQL value (used in replay code).
pub fn sql_value_to_hash(v: SqlValue) -> Hash32 {
    if let SqlValue::Blob(b) = v {
        to_hash(&b)
    } else {
        panic!("expected BLOB hash column")
    }
}
