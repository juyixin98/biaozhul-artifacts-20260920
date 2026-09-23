//! SQLite-backed chain state and the block connect/disconnect engine.
//!
//! Storage layout (WAL SQLite):
//! * `blocks`     — one row per block of the *active* chain; replaced blocks
//!                  are removed inside the same reorg transaction.
//! * `utxo`       — current UTXO set; `spend_height` is NULL while unspent and
//!                  is the spend height afterwards, which is what makes
//!                  historical ("state at height H") queries possible without
//!                  ever confusing them with the current UTXO set.
//! * `block_undo` — rollback record per block. It is written together with the
//!                  UTXO changes and the state root in one SQLite transaction
//!                  (and deleted in the same transaction on rollback), so the
//!                  undo information and the state root can never diverge.

use std::collections::{HashMap, HashSet};
use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension};

use crate::crypto;
use crate::error::{AppError, AppResult};
use crate::types::{Block, BlockInfo, ConnectResult, DisconnectResult, TipInfo, Transaction, UtxoJson};

pub const ZERO_HASH: [u8; 32] = [0u8; 32];

/// A spendable output as seen while validating a block.
#[derive(Debug, Clone)]
struct Coin {
    value: u64,
    address: [u8; 32],
    created_in: u64,
}

/// JSON-serializable undo record of everything a block changed. Only outputs
/// that existed *before* the block are recorded here; outputs created and
/// spent within the same block never touch the table.
#[derive(serde::Serialize, serde::Deserialize, Default)]
struct UndoRecord {
    /// Outputs the block spent, in full, so they can be restored.
    spent: Vec<SpentCoin>,
    /// Outpoints the block created, so they can be deleted.
    created: Vec<(String, u32)>,
}

#[derive(serde::Serialize, serde::Deserialize, Clone)]
struct SpentCoin {
    txid: String,
    vout: u32,
    value: u64,
    address: String,
    created_in: u64,
}

pub struct Storage {
    conn: Mutex<Connection>,
}

/// Header-level checks shared by connect validation.
struct HeaderContext {
    block_hash: [u8; 32],
    prev_hash: [u8; 32],
    merkle_root: [u8; 32],
    txids: Vec<[u8; 32]>,
    replace_tip: bool,
}

struct Validated {
    fee_total: u64,
    undo: UndoRecord,
    /// `(txid, vout, value, address)` for every output that survives the block.
    created_coins: Vec<(Vec<u8>, u32, u64, Vec<u8>)>,
}

impl Storage {
    /// Open (or create) the database at `path` and ensure the schema exists.
    pub fn open(path: &str) -> AppResult<Self> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.pragma_update(None, "synchronous", "FULL")?;
        let storage = Storage {
            conn: Mutex::new(conn),
        };
        storage.init_schema()?;
        Ok(storage)
    }

    fn init_schema(&self) -> AppResult<()> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS blocks (
                hash        BLOB PRIMARY KEY,
                height      INTEGER NOT NULL,
                prev_hash   BLOB NOT NULL,
                timestamp   INTEGER NOT NULL,
                merkle_root BLOB NOT NULL,
                state_root  BLOB NOT NULL,
                fee_total   INTEGER NOT NULL,
                tx_count    INTEGER NOT NULL,
                data        TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_height ON blocks(height);

            CREATE TABLE IF NOT EXISTS utxo (
                txid         BLOB NOT NULL,
                vout         INTEGER NOT NULL,
                value        INTEGER NOT NULL,
                address      BLOB NOT NULL,
                created_in   INTEGER NOT NULL,
                spend_height INTEGER,
                PRIMARY KEY (txid, vout)
            );

            CREATE TABLE IF NOT EXISTS block_undo (
                block_hash   BLOB PRIMARY KEY,
                height       INTEGER NOT NULL,
                spent_json   TEXT NOT NULL,
                created_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            "#,
        )?;
        Ok(())
    }

    // -- read-only queries -------------------------------------------------

    fn tip_row(conn: &Connection) -> AppResult<Option<(Vec<u8>, u64, Vec<u8>)>> {
        let row = conn
            .query_row(
                "SELECT value FROM metadata WHERE key='tip'",
                [],
                |r| r.get::<_, String>(0),
            )
            .optional()?;
        match row {
            None => Ok(None),
            Some(tip_hex) => {
                let hash = hex::decode(&tip_hex)
                    .map_err(|e| AppError::internal(format!("stored tip hex: {e}")))?;
                let (height, state_root) = conn.query_row(
                    "SELECT height, state_root FROM blocks WHERE hash = ?1",
                    [&hash],
                    |r| Ok((r.get::<_, i64>(0)? as u64, r.get::<_, Vec<u8>>(1)?)),
                )?;
                Ok(Some((hash, height, state_root)))
            }
        }
    }

    pub fn tip(&self) -> AppResult<Option<TipInfo>> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        match Self::tip_row(&conn)? {
            None => Ok(None),
            Some((hash, height, state_root)) => {
                let utxo_count: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM utxo WHERE spend_height IS NULL",
                    [],
                    |r| r.get(0),
                )?;
                Ok(Some(TipInfo {
                    height,
                    hash: hex::encode(hash),
                    state_root: hex::encode(state_root),
                    utxo_count,
                }))
            }
        }
    }

    pub fn active_block_at_height(&self, height: u64) -> AppResult<Option<BlockInfo>> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        Self::block_info(&conn, "height", &(height as i64))
    }

    pub fn block_by_hash(&self, hash_hex: &str) -> AppResult<Option<BlockInfo>> {
        let hash = crypto::decode_hex("hash", hash_hex, 32)?;
        let conn = self.conn.lock().expect("conn mutex poisoned");
        Self::block_info(&conn, "hash", &hash)
    }

    fn block_info(
        conn: &Connection,
        key: &str,
        val: &dyn rusqlite::ToSql,
    ) -> AppResult<Option<BlockInfo>> {
        let sql = format!(
            "SELECT height, hash, prev_hash, timestamp, tx_count, merkle_root, state_root, fee_total
             FROM blocks WHERE {key} = ?1"
        );
        conn.query_row(&sql, [val], |r| {
            Ok(BlockInfo {
                height: r.get::<_, i64>(0)? as u64,
                hash: hex::encode(r.get::<_, Vec<u8>>(1)?),
                prev_hash: hex::encode(r.get::<_, Vec<u8>>(2)?),
                timestamp: r.get::<_, i64>(3)?,
                tx_count: r.get::<_, i64>(4)? as usize,
                merkle_root: hex::encode(r.get::<_, Vec<u8>>(5)?),
                state_root: hex::encode(r.get::<_, Vec<u8>>(6)?),
                fee_total: r.get::<_, i64>(7)? as u64,
            })
        })
        .optional()
        .map_err(Into::into)
    }

    pub fn block_json_at_height(&self, height: u64) -> AppResult<Option<String>> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        conn.query_row(
            "SELECT data FROM blocks WHERE height = ?1",
            [height as i64],
            |r| r.get::<_, String>(0),
        )
        .optional()
        .map_err(Into::into)
    }

    /// Current ("live") UTXO set. This endpoint is explicit about being the
    /// *current* state; past states must be requested via `utxo_at_height`.
    pub fn current_utxos(&self, address: Option<&str>) -> AppResult<(String, Vec<UtxoJson>)> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        let tip = Self::tip_row(&conn)?
            .ok_or_else(|| AppError::conflict("chain is empty (no genesis block)"))?;
        let addr_filter = match address {
            None => None,
            Some(a) => Some(crypto::decode_hex("address", a, 32)?),
        };
        let rows = query_utxos(
            &conn,
            "SELECT txid, vout, value, address, created_in FROM utxo
             WHERE spend_height IS NULL",
            addr_filter.as_deref(),
        )?;
        Ok((hex::encode(tip.2), rows))
    }

    /// UTXO set at an explicit active-chain height. Implemented from the
    /// immutable `created_in` / `spend_height` columns, never from the current
    /// live set: an output is unspent at H iff it existed at H and was spent
    /// only at a height greater than H. The recomputed historical root is
    /// checked against the root the block recorded.
    pub fn utxo_at_height(
        &self,
        height: u64,
        address: Option<&str>,
    ) -> AppResult<(String, Vec<UtxoJson>)> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        let state_root = conn
            .query_row(
                "SELECT state_root FROM blocks WHERE height = ?1",
                [height as i64],
                |r| r.get::<_, Vec<u8>>(0),
            )
            .optional()?
            .ok_or_else(|| {
                AppError::not_found(format!(
                    "no block at height {height} on the active chain"
                ))
            })?;

        let addr_filter = match address {
            None => None,
            Some(a) => Some(crypto::decode_hex("address", a, 32)?),
        };

        let mut leaf_rows: Vec<([u8; 32], u32, u64, [u8; 32], u64)> = Vec::new();
        let mut out = Vec::new();
        {
            let mut sql = String::from(
                "SELECT txid, vout, value, address, created_in FROM utxo
                 WHERE created_in <= ?1 AND (spend_height IS NULL OR spend_height > ?1)",
            );
            if addr_filter.is_some() {
                sql.push_str(" AND address = ?2");
            }
            sql.push_str(" ORDER BY txid, vout");
            let mut stmt = conn.prepare(&sql)?;
            let mut iter = match &addr_filter {
                None => stmt.query(params![height as i64])?,
                Some(a) => stmt.query(params![height as i64, a])?,
            };
            while let Some(r) = iter.next()? {
                let u = read_utxo_row(r)?;
                let txid = hex::decode(&u.txid).unwrap();
                let addr = hex::decode(&u.address).unwrap();
                leaf_rows.push((
                    txid.try_into().unwrap(),
                    u.vout,
                    u.value,
                    addr.try_into().unwrap(),
                    u.created_in,
                ));
                out.push(u);
            }
        }
        let recomputed = crypto::state_root_from_rows(leaf_rows);
        if recomputed.as_slice() != state_root.as_slice() {
            return Err(AppError::internal(format!(
                "history root mismatch at height {height}: stored {} recomputed {}",
                hex::encode(&state_root),
                hex::encode(recomputed)
            )));
        }
        Ok((hex::encode(state_root), out))
    }

    // -- block connect: validate + rollback-reorg + apply, all atomic -------

    pub fn connect_block(&self, block: Block) -> AppResult<ConnectResult> {
        let mut conn = self.conn.lock().expect("conn mutex poisoned");
        let tx = conn.transaction()?;

        // 1) Pure/static header validation against the current tip.
        let ctx = validate_header(&tx, &block)?;

        // 2) Same-height replacement: roll the old tip back *first*, inside
        //    this transaction, so the alternative block cannot spend coins the
        //    old block created. Any later failure rolls the transaction back
        //    and the old tip is fully restored.
        let mut disconnected_heights = Vec::new();
        if ctx.replace_tip {
            let old_tip_hash = Self::tip_row(&tx)?.expect("replace implies a tip").0;
            Self::apply_disconnect(&tx, &old_tip_hash)?;
            disconnected_heights.push(block.height);
        }

        // 3) Semantic validation against the (possibly post-rollback) image.
        let validated = validate_transactions(&tx, &block, &ctx)?;

        // 4) Apply spends.
        for spent in &validated.undo.spent {
            let txid = hex::decode(&spent.txid).unwrap();
            let affected = tx.execute(
                "UPDATE utxo SET spend_height = ?1
                 WHERE txid = ?2 AND vout = ?3 AND spend_height IS NULL",
                params![block.height as i64, txid, spent.vout],
            )?;
            if affected != 1 {
                return Err(AppError::internal(format!(
                    "missing live utxo {}:{} at apply time", spent.txid, spent.vout
                )));
            }
        }

        // 5) Apply surviving created outputs. Upsert makes reconnect
        //    idempotent: re-applying a previously-disconnected block can meet
        //    rows that remain in the table marked spent at a later height, in
        //    which case they are restored to live with the block's metadata.
        for (txid, vout, value, address) in &validated.created_coins {
            tx.execute(
                "INSERT INTO utxo (txid, vout, value, address, created_in, spend_height)
                 VALUES (?1, ?2, ?3, ?4, ?5, NULL)
                 ON CONFLICT(txid, vout) DO UPDATE SET
                    value = excluded.value,
                    address = excluded.address,
                    created_in = excluded.created_in,
                    spend_height = NULL",
                params![txid, vout, *value as i64, address, block.height as i64],
            )
            .map_err(|e| AppError::internal(format!("utxo upsert: {e}")))?;
        }

        // 6) State root from the physical post-image; undo + block + tip move
        //    in the same transaction.
        let root = recompute_state_root(&tx)?;
        tx.execute(
            "INSERT INTO blocks (hash, height, prev_hash, timestamp, merkle_root,
                                 state_root, fee_total, tx_count, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                ctx.block_hash,
                block.height as i64,
                ctx.prev_hash,
                block.timestamp,
                ctx.merkle_root,
                root,
                validated.fee_total as i64,
                block.txs.len() as i64,
                serde_json::to_string(&block).unwrap(),
            ],
        )
        .map_err(|e| AppError::internal(format!("block insert: {e}")))?;

        tx.execute(
            "INSERT INTO block_undo (block_hash, height, spent_json, created_json)
             VALUES (?1, ?2, ?3, ?4)",
            params![
                ctx.block_hash,
                block.height as i64,
                serde_json::to_string(&validated.undo.spent).unwrap(),
                serde_json::to_string(&validated.undo.created).unwrap(),
            ],
        )
        .map_err(|e| AppError::internal(format!("undo insert: {e}")))?;

        tx.execute("DELETE FROM metadata WHERE key = 'tip'", [])?;
        tx.execute(
            "INSERT INTO metadata(key, value) SELECT 'tip', ?1
             WHERE NOT EXISTS (SELECT 1 FROM metadata WHERE key = 'tip')",
            [hex::encode(ctx.block_hash)],
        )?;

        tx.commit()?;

        Ok(ConnectResult {
            accepted: true,
            height: block.height,
            hash: hex::encode(ctx.block_hash),
            state_root: hex::encode(root),
            reorged: ctx.replace_tip,
            disconnected_heights,
            fee_total: validated.fee_total,
        })
    }

    // -- naive replay cross-check ------------------------------------------

    pub fn run_naive_replay(&self) -> AppResult<crate::naive::ReplayReport> {
        let conn = self.conn.lock().expect("conn mutex poisoned");
        crate::naive::replay_active_chain(&conn)
    }

    /// Explicitly disconnect the chain tip, restoring its UTXO changes.
    pub fn disconnect_tip(&self) -> AppResult<DisconnectResult> {
        let mut conn = self.conn.lock().expect("conn mutex poisoned");
        let tx = conn.transaction()?;
        let tip_hash = match Self::tip_row(&tx)? {
            Some((h, _, _)) => h,
            None => return Err(AppError::conflict("cannot disconnect: chain is empty")),
        };
        let height = Self::apply_disconnect(&tx, &tip_hash)?;
        let new_tip = Self::tip_row(&tx)?;
        tx.commit()?;

        let (new_tip_height, new_tip_hash, state_root) = match new_tip {
            Some((h, ht, root)) => (Some(ht), hex::encode(h), hex::encode(root)),
            None => (
                None,
                hex::encode(ZERO_HASH),
                hex::encode(crypto::state_root_from_rows(vec![])),
            ),
        };
        Ok(DisconnectResult {
            disconnected: hex::encode(tip_hash),
            height,
            new_tip_height,
            new_tip_hash,
            state_root,
        })
    }

    /// Undo one block inside an open transaction and move the tip back.
    /// Returns the disconnected block's height.
    fn apply_disconnect(tx: &Connection, block_hash: &[u8]) -> AppResult<u64> {
        let (height, spent_json, created_json): (i64, String, String) = tx
            .query_row(
                "SELECT height, spent_json, created_json FROM block_undo WHERE block_hash = ?1",
                [block_hash],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .map_err(|e| AppError::internal(format!("undo record missing: {e}")))?;

        let spent: Vec<SpentCoin> = serde_json::from_str(&spent_json)
            .map_err(|e| AppError::internal(format!("undo spent decode: {e}")))?;
        let created: Vec<(String, u32)> = serde_json::from_str(&created_json)
            .map_err(|e| AppError::internal(format!("undo created decode: {e}")))?;

        // Delete outputs the block created.
        for (txid_hex, vout) in &created {
            let txid = hex::decode(txid_hex).unwrap();
            tx.execute(
                "DELETE FROM utxo WHERE txid = ?1 AND vout = ?2",
                params![txid, vout],
            )?;
        }

        // Restore every pre-existing output the block spent. The row still
        // exists (spends only set spend_height), so on conflict we clear the
        // spent marker rather than inserting a duplicate primary key.
        for c in &spent {
            let txid = hex::decode(&c.txid).unwrap();
            let addr = hex::decode(&c.address).unwrap();
            tx.execute(
                "INSERT INTO utxo (txid, vout, value, address, created_in, spend_height)
                 VALUES (?1, ?2, ?3, ?4, ?5, NULL)
                 ON CONFLICT(txid, vout) DO UPDATE SET
                    value = excluded.value,
                    address = excluded.address,
                    created_in = excluded.created_in,
                    spend_height = NULL",
                params![txid, c.vout, c.value as i64, addr, c.created_in as i64],
            )
            .map_err(|e| AppError::internal(format!("undo restore failed: {e}")))?;
        }

        tx.execute("DELETE FROM block_undo WHERE block_hash = ?1", [block_hash])?;
        tx.execute("DELETE FROM blocks WHERE hash = ?1", [block_hash])?;

        // Move the tip to the parent block's own hash. The parent row at
        // height-1 still exists in `blocks` (only the current block was just
        // deleted); its *hash* — not its prev_hash — is the new tip.
        if height > 0 {
            let parent_hash: Vec<u8> = tx.query_row(
                "SELECT hash FROM blocks WHERE height = ?1",
                [height - 1],
                |r| r.get(0),
            )?;
            tx.execute(
                "UPDATE metadata SET value = ?1 WHERE key = 'tip'",
                [hex::encode(parent_hash)],
            )?;
        } else {
            tx.execute("DELETE FROM metadata WHERE key = 'tip'", [])?;
        }
        Ok(height as u64)
    }
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

fn is_coinbase(t: &Transaction) -> bool {
    t.inputs.iter().all(|i| i.prev.is_none())
}

/// Header / shape checks, real hashing and attachment rules.
fn validate_header(conn: &Connection, block: &Block) -> AppResult<HeaderContext> {
    if block.txs.is_empty() {
        return Err(AppError::conflict("block contains no transactions"));
    }
    let prev_hash_vec = crypto::decode_hex("prev_hash", &block.prev_hash, 32)?;
    let prev_hash: [u8; 32] = prev_hash_vec.try_into().unwrap();

    // Exactly one coinbase, first, with exactly one empty input.
    let expected_tag = hex::encode(block.height.to_le_bytes());
    for (i, t) in block.txs.iter().enumerate() {
        if i == 0 {
            if t.inputs.len() != 1 || !is_coinbase(t) {
                return Err(AppError::conflict(
                    "first transaction must be the coinbase with exactly one coinbase input",
                ));
            }
            if t.inputs[0].coinbase_tag != expected_tag {
                return Err(AppError::conflict(format!(
                    "coinbase tag must be the 8-byte LE block height ({expected_tag})"
                )));
            }
            if !t.inputs[0].pubkey.is_empty() || !t.inputs[0].signature.is_empty() {
                return Err(AppError::conflict(
                    "coinbase input must not carry pubkey/signature",
                ));
            }
        } else {
            if is_coinbase(t) {
                return Err(AppError::conflict(format!(
                    "tx {i}: only the first transaction may be a coinbase"
                )));
            }
            for (ii, inp) in t.inputs.iter().enumerate() {
                if !inp.coinbase_tag.is_empty() {
                    return Err(AppError::conflict(format!(
                        "tx {i} input {ii}: non-coinbase inputs must not carry a coinbase_tag"
                    )));
                }
            }
        }
        if t.outputs.is_empty() {
            return Err(AppError::conflict(format!("tx {i}: has no outputs")));
        }
        for (v, o) in t.outputs.iter().enumerate() {
            crypto::decode_hex("address", &o.address, 32)?;
            if o.value > i64::MAX as u64 {
                return Err(AppError::conflict(format!(
                    "tx {i} output {v}: value {} exceeds storage bound {}",
                    o.value,
                    i64::MAX
                )));
            }
        }
    }

    // Real txids and in-block uniqueness.
    let mut txids = Vec::with_capacity(block.txs.len());
    let mut seen = HashSet::new();
    for t in &block.txs {
        let id = crypto::txid(t)?;
        if !seen.insert(id) {
            return Err(AppError::conflict(format!(
                "duplicate transaction {} in block",
                hex::encode(id)
            )));
        }
        txids.push(id);
    }

    // Real Merkle root.
    let merkle_root = crypto::merkle_root(&txids);
    if block.merkle_root.is_empty() {
        return Err(AppError::bad_request("merkle_root is required"));
    }
    let supplied = crypto::decode_hex("merkle_root", &block.merkle_root, 32)?;
    if supplied != merkle_root {
        return Err(AppError::conflict(format!(
            "merkle_root mismatch: supplied {} computed {}",
            hex::encode(&supplied),
            hex::encode(merkle_root)
        )));
    }

    let block_hash = crypto::hash256(&crypto::serialize_header(
        block.height,
        &prev_hash,
        block.timestamp,
        &merkle_root,
    ));

    // Attachment rules.
    let tip = Storage::tip_row(conn)?;
    let replace_tip = match &tip {
        None => {
            if block.height != 0 || prev_hash != ZERO_HASH {
                return Err(AppError::conflict(
                    "genesis must be height 0 with an all-zero prev_hash",
                ));
            }
            false
        }
        Some((tip_hash, tip_height, _)) => {
            if prev_hash.as_slice() == tip_hash.as_slice() {
                if block.height != tip_height + 1 {
                    return Err(AppError::conflict(format!(
                        "height must be {} after the current tip",
                        tip_height + 1
                    )));
                }
                false
            } else {
                // Same-height alternative block: its parent must be known.
                let p_height: i64 = conn
                    .query_row(
                        "SELECT height FROM blocks WHERE hash = ?1",
                        [&prev_hash],
                        |r| r.get(0),
                    )
                    .optional()?
                    .ok_or_else(|| {
                        AppError::conflict(
                            "parent block is unknown; cannot attach alternative block",
                        )
                    })?;
                if block.height != p_height as u64 + 1 {
                    return Err(AppError::conflict(format!(
                        "height must follow parent height {p_height}"
                    )));
                }
                if block.height != *tip_height {
                    return Err(AppError::conflict(format!(
                        "alternative block at height {} does not replace current tip at height {} \
                         (only same-height replacement; submit alternative blocks in order)",
                        block.height, tip_height
                    )));
                }
                true
            }
        }
    };

    Ok(HeaderContext {
        block_hash,
        prev_hash,
        merkle_root,
        txids,
        replace_tip,
    })
}

/// Sequential in-block validation: inputs exist and are unspent (against the
/// database image plus this block's own changes), no reuse, real ownership and
/// Ed25519 signature checks, sufficient sums, exact coinbase reward.
fn validate_transactions(
    conn: &Connection,
    block: &Block,
    ctx: &HeaderContext,
) -> AppResult<Validated> {
    let mut undo = UndoRecord::default();
    let mut added: HashMap<(Vec<u8>, u32), Coin> = HashMap::new();
    let mut removed: HashSet<(Vec<u8>, u32)> = HashSet::new();

    // Coinbase outputs are registered before validation of the other txs but
    // flagged as immature, so spending one inside this block always fails.
    let coinbase_txid = ctx.txids[0];
    let coinbase = &block.txs[0];
    for (vout, o) in coinbase.outputs.iter().enumerate() {
        let addr = hex::decode(&o.address).unwrap();
        added.insert(
            (coinbase_txid.to_vec(), vout as u32),
            Coin {
                value: o.value,
                address: addr.try_into().unwrap(),
                created_in: block.height,
            },
        );
    }
    let coinbase_outpoints: HashSet<(Vec<u8>, u32)> = coinbase
        .outputs
        .iter()
        .enumerate()
        .map(|(v, _)| (coinbase_txid.to_vec(), v as u32))
        .collect();

    /// Resolve an outpoint against (in order) this block's own additions and
    /// removals, then the database image.
    fn lookup_coin(
        conn: &Connection,
        added: &HashMap<(Vec<u8>, u32), Coin>,
        removed: &HashSet<(Vec<u8>, u32)>,
        op: &(Vec<u8>, u32),
    ) -> Option<Coin> {
        if removed.contains(op) {
            return None;
        }
        if let Some(c) = added.get(op) {
            return Some(c.clone());
        }
        conn.query_row(
            "SELECT value, address, created_in FROM utxo
             WHERE txid = ?1 AND vout = ?2 AND spend_height IS NULL",
            params![&op.0, op.1],
            |r| {
                let address: Vec<u8> = r.get(1)?;
                Ok(Coin {
                    value: r.get::<_, i64>(0)? as u64,
                    address: address.try_into().unwrap(),
                    created_in: r.get::<_, i64>(2)? as u64,
                })
            },
        )
        .ok()
    }

    let mut fee_total: u128 = 0;

    for (ti, tx) in block.txs.iter().enumerate().skip(1) {
        let txid_bytes = ctx.txids[ti];
        let mut in_sum: u128 = 0;
        let mut tx_seen: HashSet<(Vec<u8>, u32)> = HashSet::new();

        for (ii, input) in tx.inputs.iter().enumerate() {
            let op_wire = input.prev.as_ref().ok_or_else(|| {
                AppError::conflict(format!("tx {ti} input {ii}: missing prev outpoint"))
            })?;
            let prev_txid = crypto::decode_hex("input.txid", &op_wire.txid, 32)?;
            let op = (prev_txid, op_wire.vout);

            if coinbase_outpoints.contains(&op) {
                return Err(AppError::conflict(format!(
                    "tx {ti} input {ii}: coinbase outputs are not spendable in the same block"
                )));
            }
            if !tx_seen.insert(op.clone()) {
                return Err(AppError::conflict(format!(
                    "tx {ti} input {ii}: duplicate input {}:{} within transaction (double spend)",
                    hex::encode(&op.0), op.1
                )));
            }
            if removed.contains(&op) {
                return Err(AppError::conflict(format!(
                    "tx {ti} input {ii}: double spend of {}:{} within block",
                    hex::encode(&op.0), op.1
                )));
            }

            let coin = lookup_coin(conn, &added, &removed, &op).ok_or_else(|| {
                AppError::conflict(format!(
                    "tx {ti} input {ii}: input {}:{} does not exist or is already spent",
                    hex::encode(&op.0), op.1
                ))
            })?;

            let pubkey = crypto::decode_hex("input.pubkey", &input.pubkey, 32)?;
            if pubkey != coin.address.as_slice() {
                return Err(AppError::conflict(format!(
                    "tx {ti} input {ii}: pubkey does not own output {}:{}",
                    hex::encode(&op.0), op.1
                )));
            }
            let sig = crypto::decode_hex("input.signature", &input.signature, 64)?;
            if !crypto::verify_signature(&pubkey, &txid_bytes, &sig) {
                return Err(AppError::conflict(format!(
                    "tx {ti} input {ii}: invalid Ed25519 signature over txid {}",
                    hex::encode(txid_bytes)
                )));
            }

            in_sum = in_sum.checked_add(coin.value as u128).ok_or_else(|| {
                AppError::conflict(format!("tx {ti}: input amount overflow"))
            })?;

            // Only pre-existing coins need an undo entry.
            if coin.created_in != block.height {
                undo.spent.push(SpentCoin {
                    txid: hex::encode(&op.0),
                    vout: op.1,
                    value: coin.value,
                    address: hex::encode(coin.address),
                    created_in: coin.created_in,
                });
            }
            removed.insert(op);
        }

        let out_sum: u128 = tx.outputs.iter().map(|o| o.value as u128).sum();
        if in_sum < out_sum {
            return Err(AppError::conflict(format!(
                "tx {ti} ({}) spends {in_sum} but creates {out_sum}: input insufficient (whole block rejected)",
                hex::encode(txid_bytes)
            )));
        }
        fee_total = fee_total
            .checked_add(in_sum - out_sum)
            .ok_or_else(|| AppError::conflict(format!("tx {ti}: fee overflow")))?;

        for (v, o) in tx.outputs.iter().enumerate() {
            let op = (txid_bytes.to_vec(), v as u32);
            if added.contains_key(&op) {
                return Err(AppError::conflict(format!(
                    "duplicate outpoint in block: {}:{v}",
                    hex::encode(txid_bytes)
                )));
            }
            let addr = hex::decode(&o.address).unwrap();
            added.insert(
                op,
                Coin {
                    value: o.value,
                    address: addr.try_into().unwrap(),
                    created_in: block.height,
                },
            );
        }
    }

    // Fixed coinbase rule: subsidy + fees, no more, no less.
    let subsidy = crate::types::block_subsidy(block.height);
    let coinbase_sum: u128 = coinbase.outputs.iter().map(|o| o.value as u128).sum();
    let expected = subsidy as u128 + fee_total;
    if coinbase_sum != expected {
        return Err(AppError::conflict(format!(
            "coinbase creates {coinbase_sum} but subsidy({subsidy}) + fees({fee_total}) = {expected}"
        )));
    }
    if fee_total > i64::MAX as u128 {
        return Err(AppError::conflict("fee total overflow"));
    }

    // Surviving created outputs (created-and-spent-in-block ones are skipped).
    let mut created_coins = Vec::new();
    for ((txid_b, vout), coin) in &added {
        if removed.contains(&(txid_b.clone(), *vout)) {
            continue;
        }
        undo.created.push((hex::encode(txid_b), *vout));
        created_coins.push((
            txid_b.clone(),
            *vout,
            coin.value,
            coin.address.to_vec(),
        ));
    }

    Ok(Validated {
        fee_total: fee_total as u64,
        undo,
        created_coins,
    })
}

// ---------------------------------------------------------------------------
// Row helpers
// ---------------------------------------------------------------------------

fn read_utxo_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<UtxoJson> {
    Ok(UtxoJson {
        txid: hex::encode(r.get::<_, Vec<u8>>(0)?),
        vout: r.get::<_, i64>(1)? as u32,
        value: r.get::<_, i64>(2)? as u64,
        address: hex::encode(r.get::<_, Vec<u8>>(3)?),
        created_in: r.get::<_, i64>(4)? as u64,
    })
}

/// Current-set query.
fn query_utxos(
    conn: &Connection,
    sql: &str,
    address: Option<&[u8]>,
) -> AppResult<Vec<UtxoJson>> {
    let mut out = Vec::new();
    let mut sql = sql.to_string();
    if address.is_some() {
        sql.push_str(" AND address = ?1");
    }
    sql.push_str(" ORDER BY txid, vout");
    let mut stmt = conn.prepare(&sql)?;
    let mut iter = match address {
        None => stmt.query([])?,
        Some(a) => stmt.query([a])?,
    };
    while let Some(r) = iter.next()? {
        out.push(read_utxo_row(r)?);
    }
    Ok(out)
}

/// Recompute the state root directly from the live UTXO rows.
pub fn recompute_state_root(conn: &Connection) -> AppResult<[u8; 32]> {
    let mut stmt = conn.prepare(
        "SELECT txid, vout, value, address, created_in FROM utxo
         WHERE spend_height IS NULL ORDER BY txid, vout",
    )?;
    let mut rows: Vec<([u8; 32], u32, u64, [u8; 32], u64)> = Vec::new();
    let mut iter = stmt.query([])?;
    while let Some(r) = iter.next()? {
        let txid: Vec<u8> = r.get(0)?;
        let vout: i64 = r.get(1)?;
        let value: i64 = r.get(2)?;
        let addr: Vec<u8> = r.get(3)?;
        let created_in: i64 = r.get(4)?;
        rows.push((
            txid.try_into().unwrap(),
            vout as u32,
            value as u64,
            addr.try_into().unwrap(),
            created_in as u64,
        ));
    }
    Ok(crypto::state_root_from_rows(rows))
}
