//! Chain logic: connect / disconnect blocks with undo records, plus an
//! independent naive replay used to cross-check results.

use std::collections::{HashMap, HashSet};

use rusqlite::Connection;

use crate::db::{
    block_hash_at_locked, hash_param, state_root_at_locked, state_root_from_sorted, to_hash,
    GENESIS_HEIGHT,
};
use crate::error::{Error, Result, COINBASE_SUBSIDY};
use crate::hash::Hash32;
use crate::model::{Block, Transaction};

type Op = (Hash32, u32);

#[derive(Debug)]
pub struct ConnectOutcome {
    pub height: i64,
    pub hash: Hash32,
    pub state_root: Hash32,
    pub fees: i64,
    pub tx_count: usize,
}

#[derive(Debug)]
pub struct DisconnectOutcome {
    pub tip_height: i64,
    pub tip_hash: Hash32,
    pub state_root: Hash32,
    pub disconnected: usize,
}

#[derive(Debug)]
enum UndoOp {
    /// Output spent while applying this block; revive it on rollback.
    Spend { op: Op, prev_spent: Option<i64> },
    /// Output created while applying this block; delete it on rollback.
    Create { op: Op },
}

fn i64_add(a: i64, b: i64) -> Result<i64> {
    a.checked_add(b).ok_or(Error::Overflow)
}

/// Look up an output that must currently be spendable: first among outputs
/// created earlier in *this* block, then in the committed UTXO set.
fn resolve_input(
    conn: &Connection,
    op: &Op,
    created_map: &HashMap<Op, (i64, i64, String)>,
    parent_height: i64,
    later_in_block: bool,
) -> Result<i64> {
    if let Some((_, value, _)) = created_map.get(op) {
        return Ok(*value);
    }
    let value: Option<i64> = conn
        .query_row(
            "SELECT value FROM utxos
              WHERE txid = ?1 AND vout = ?2
                AND created_height <= ?3
                AND (spent_height IS NULL OR spent_height > ?3)",
            rusqlite::params![hash_param(&op.0), op.1, parent_height],
            |r| r.get(0),
        )
        .ok();
    match value {
        Some(v) => Ok(v),
        None if later_in_block => Err(Error::InputInFuture {
            txid: op.0.to_hex(),
            vout: op.1,
        }),
        None => Err(Error::UnknownInput {
            txid: op.0.to_hex(),
            vout: op.1,
        }),
    }
}

/// Validate and append one block. Everything happens in one SQL transaction.
pub fn connect_block(db: &crate::db::Db, block: &Block) -> Result<ConnectOutcome> {
    let mut conn = db.conn.lock().unwrap();
    let sql_tx = conn.transaction()?;

    let (tip_height, tip_hash): (i64, Hash32) = sql_tx.query_row(
        "SELECT tip_height, tip_hash FROM meta",
        [],
        |r| {
            let h: i64 = r.get(0)?;
            let b: Vec<u8> = r.get(1)?;
            Ok((h, to_hash(&b)))
        },
    )?;

    // 1. Chain linkage.
    if block.height != tip_height + 1 {
        return Err(Error::HeightMismatch {
            expected: tip_height + 1,
            got: block.height,
        });
    }
    if block.prev_hash != tip_hash {
        return Err(Error::PrevHashMismatch);
    }

    // 2. Structural checks + computed txids.
    let ids = crate::model::static_check(block)?;

    // 3. Reject transactions already committed (any output row existing).
    for id in &ids {
        let exists: i64 = sql_tx.query_row(
            "SELECT COUNT(*) FROM utxos WHERE txid = ?1",
            rusqlite::params![hash_param(id)],
            |r| r.get(0),
        )?;
        if exists > 0 {
            return Err(Error::TxAlreadyExists(id.to_hex()));
        }
    }

    // Index of every outpoint created later in this block (for error codes).
    let mut block_outpoints: HashSet<Op> = HashSet::new();
    for (id, t) in ids.iter().zip(block.txs.iter()) {
        for v in 0..t.vout.len() {
            block_outpoints.insert((*id, v as u32));
        }
    }

    // 4. Execute transactions in order.
    let mut created_map: HashMap<Op, (i64, i64, String)> = HashMap::new();
    let mut spent_in_block: HashSet<Op> = HashSet::new();
    let mut undo: Vec<UndoOp> = Vec::new();
    let mut fees: i64 = 0;
    // Fees are paid by the non-coinbase txs that follow the coinbase, so its
    // amount is verified only after the whole block has executed.
    let mut coinbase_paid: Option<i64> = None;

    for (idx, (id, t)) in ids.iter().zip(block.txs.iter()).enumerate() {
        if t.is_coinbase() {
            if idx != 0 {
                return Err(Error::MultipleCoinbase);
            }
            coinbase_paid = Some(tx_output_sum(t)?);
        } else {
            let mut in_sum: i128 = 0;
            for input in &t.vin {
                let op = (input.txid, input.vout);
                if !spent_in_block.insert(op) {
                    return Err(Error::DoubleSpend {
                        txid: op.0.to_hex(),
                        vout: op.1,
                    });
                }
                let later = block_outpoints.contains(&op) && !created_map.contains_key(&op);
                let value =
                    resolve_input(&sql_tx, &op, &created_map, tip_height, later)?;
                in_sum += value as i128;
                undo.push(UndoOp::Spend {
                    op,
                    prev_spent: None,
                });
            }

            let out_sum = tx_output_sum(t)? as i128;
            if in_sum < out_sum {
                return Err(Error::InsufficientInputs {
                    inputs: in_sum as i64,
                    outputs: out_sum as i64,
                });
            }
            let tx_fee = i64::try_from(in_sum - out_sum).map_err(|_| Error::Overflow)?;
            fees = i64_add(fees, tx_fee)?;

            // Mark committed inputs spent.
            for input in &t.vin {
                let op = (input.txid, input.vout);
                if created_map.contains_key(&op) {
                    // Spending an output created earlier in this same block:
                    // the row was just inserted; mark it spent now.
                    let n = sql_tx.execute(
                        "UPDATE utxos SET spent_height = ?1
                          WHERE txid = ?2 AND vout = ?3 AND spent_height IS NULL",
                        rusqlite::params![
                            block.height,
                            hash_param(&op.0),
                            op.1
                        ],
                    )?;
                    assert_eq!(n, 1, "in-block output disappeared");
                } else {
                    let n = sql_tx.execute(
                        "UPDATE utxos SET spent_height = ?1
                          WHERE txid = ?2 AND vout = ?3
                            AND created_height <= ?4
                            AND spent_height IS NULL",
                        rusqlite::params![
                            block.height,
                            hash_param(&op.0),
                            op.1,
                            tip_height
                        ],
                    )?;
                    if n != 1 {
                        // Defensive: validation above should have caught this.
                        return Err(Error::DoubleSpend {
                            txid: op.0.to_hex(),
                            vout: op.1,
                        });
                    }
                }
            }
        }

        // Create new outputs.
        for (v, out) in t.vout.iter().enumerate() {
            let op = (*id, v as u32);
            sql_tx.execute(
                "INSERT INTO utxos(txid, vout, value, address, created_height, spent_height)
                 VALUES (?1, ?2, ?3, ?4, ?5, NULL)",
                rusqlite::params![
                    hash_param(id),
                    v as u32,
                    out.value,
                    out.address,
                    block.height
                ],
            )?;
            created_map.insert(op, (block.height, out.value, out.address.clone()));
            undo.push(UndoOp::Create { op });
        }
    }

    // Coinbase reward = fixed subsidy + fees collected inside this block.
    let coinbase_paid = coinbase_paid.expect("static_check guarantees a coinbase");
    let expected_reward = i64_add(COINBASE_SUBSIDY, fees)?;
    if coinbase_paid != expected_reward {
        return Err(Error::BadCoinbaseAmount {
            paid: coinbase_paid,
            expected: expected_reward,
        });
    }

    // 5. Derive hashes for real and persist block + undo + tip atomically.
    let state_root = state_root_at_locked(&sql_tx, block.height)?;
    let block_hash = block.block_hash();
    let block_json = serde_json::to_string(&canonical_block_json(block))?;

    sql_tx.execute(
        "INSERT INTO blocks(height, hash, prev_hash, merkle_root, timestamp, block_json)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        rusqlite::params![
            block.height,
            hash_param(&block_hash),
            hash_param(&block.prev_hash),
            hash_param(&block.merkle_root()),
            block.timestamp,
            block_json,
        ],
    )?;

    for op in &undo {
        match op {
            UndoOp::Spend { op, prev_spent } => {
                sql_tx.execute(
                    "INSERT INTO undo_records
                        (height, undo_type, txid, vout, value, address, created_height, prev_spent)
                     VALUES (?1, 1, ?2, ?3, NULL, NULL, NULL, ?4)",
                    rusqlite::params![
                        block.height,
                        hash_param(&op.0),
                        op.1,
                        prev_spent
                    ],
                )?;
            }
            UndoOp::Create { op } => {
                sql_tx.execute(
                    "INSERT INTO undo_records
                        (height, undo_type, txid, vout, value, address, created_height, prev_spent)
                     VALUES (?1, 2, ?2, ?3, NULL, NULL, NULL, NULL)",
                    rusqlite::params![block.height, hash_param(&op.0), op.1],
                )?;
            }
        }
    }

    sql_tx.execute(
        "UPDATE meta SET tip_height = ?1, tip_hash = ?2, state_root = ?3 WHERE id = 1",
        rusqlite::params![
            block.height,
            hash_param(&block_hash),
            hash_param(&state_root)
        ],
    )?;

    sql_tx.commit()?;

    Ok(ConnectOutcome {
        height: block.height,
        hash: block_hash,
        state_root,
        fees,
        tx_count: block.txs.len(),
    })
}

fn tx_output_sum(t: &Transaction) -> Result<i64> {
    t.vout
        .iter()
        .try_fold(0i64, |acc, o| i64_add(acc, o.value))
}

/// Re-serialize without echoed txids so stored JSON is canonical.
fn canonical_block_json(block: &Block) -> serde_json::Value {
    let mut v = serde_json::to_value(block).expect("block serializes");
    if let Some(txs) = v.get_mut("txs").and_then(|t| t.as_array_mut()) {
        for t in txs {
            if t.get("txid").is_some() {
                t.as_object_mut().unwrap().remove("txid");
            }
        }
    }
    v
}

/// Roll back from the tip down to `target_height` (tip becomes that height).
/// Undo records are replayed in reverse inside one atomic SQL transaction.
pub fn disconnect_blocks(db: &crate::db::Db, target_height: i64) -> Result<DisconnectOutcome> {
    if target_height < GENESIS_HEIGHT {
        return Err(Error::CannotDisconnectGenesis);
    }

    let mut conn = db.conn.lock().unwrap();
    let sql_tx = conn.transaction()?;

    let (tip_height, _): (i64, Hash32) = sql_tx.query_row(
        "SELECT tip_height, tip_hash FROM meta",
        [],
        |r| {
            let h: i64 = r.get(0)?;
            let b: Vec<u8> = r.get(1)?;
            Ok((h, to_hash(&b)))
        },
    )?;

    if target_height > tip_height {
        return Err(Error::Malformed(format!(
            "target height {target_height} is above tip {tip_height}"
        )));
    }

    let disconnected = (tip_height - target_height) as usize;

    for h in (target_height + 1..=tip_height).rev() {
        if block_hash_at_locked(&sql_tx, h)?.is_none() {
            return Err(Error::UnknownHeight(h));
        }

        let mut stmt = sql_tx.prepare(
            "SELECT undo_type, txid, vout, prev_spent
               FROM undo_records
              WHERE height = ?1
              ORDER BY id DESC",
        )?;
        let rows: Vec<(i64, Vec<u8>, i64, Option<i64>)> = stmt
            .query_map(rusqlite::params![h], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
            })?
            .collect::<std::result::Result<_, _>>()?;
        drop(stmt);

        for (undo_type, txid, vout, prev_spent) in rows {
            let txid = to_hash(&txid);
            match undo_type {
                1 => {
                    // Revive a spent output.
                    let n = sql_tx.execute(
                        "UPDATE utxos SET spent_height = ?1
                          WHERE txid = ?2 AND vout = ?3 AND spent_height = ?4",
                        rusqlite::params![
                            prev_spent,
                            hash_param(&txid),
                            vout as u32,
                            h
                        ],
                    )?;
                    assert_eq!(n, 1, "undo: spend record matched no row at height {h}");
                }
                2 => {
                    // Remove an output this block created.
                    let n = sql_tx.execute(
                        "DELETE FROM utxos
                          WHERE txid = ?1 AND vout = ?2 AND created_height = ?3",
                        rusqlite::params![hash_param(&txid), vout as u32, h],
                    )?;
                    assert_eq!(n, 1, "undo: create record matched no row at height {h}");
                }
                other => panic!("unknown undo_type {other}"),
            }
        }

        sql_tx.execute("DELETE FROM undo_records WHERE height = ?1", [h])?;
        sql_tx.execute("DELETE FROM blocks WHERE height = ?1", [h])?;
    }

    let new_tip_hash = block_hash_at_locked(&sql_tx, target_height)?
        .expect("target block exists");
    let state_root = state_root_at_locked(&sql_tx, target_height)?;

    sql_tx.execute(
        "UPDATE meta SET tip_height = ?1, tip_hash = ?2, state_root = ?3 WHERE id = 1",
        rusqlite::params![
            target_height,
            hash_param(&new_tip_hash),
            hash_param(&state_root)
        ],
    )?;

    sql_tx.commit()?;

    Ok(DisconnectOutcome {
        tip_height: target_height,
        tip_hash: new_tip_hash,
        state_root,
        disconnected,
    })
}

// ---------------------------------------------------------------------------
// Naive replay: an independent, from-genesis reconstruction of UTXO state.
// ---------------------------------------------------------------------------

#[derive(Debug, serde::Serialize)]
pub struct ReplayHeight {
    pub height: i64,
    pub block_hash: String,
    pub replayed_state_root: String,
    pub stored_state_root: String,
    pub roots_match: bool,
}

/// Replay blocks 1..=`up_to` from genesis with a plain in-memory HashMap and
/// compare every height against the SQLite chain. This deliberately does not
/// touch undo information: it is a ground-truth re-execution.
pub fn naive_replay(db: &crate::db::Db, up_to: i64) -> Result<Vec<ReplayHeight>> {
    let conn = db.conn.lock().unwrap();
    let (tip, _): (i64, Hash32) = conn.query_row(
        "SELECT tip_height, tip_hash FROM meta",
        [],
        |r| Ok((r.get::<_, i64>(0)?, to_hash(&r.get::<_, Vec<u8>>(1)?))),
    )?;
    if up_to < 0 || up_to > tip {
        return Err(Error::UnknownHeight(up_to));
    }

    let mut out = Vec::new();

    // Genesis: empty set.
    let mut set: HashMap<Op, (i64, String)> = HashMap::new();
    let genesis_root = root_of_map(&set);
    let stored_genesis = state_root_at_locked(&conn, 0)?;
    out.push(ReplayHeight {
        height: 0,
        block_hash: Hash32::ZERO.to_hex(),
        replayed_state_root: genesis_root.to_hex(),
        stored_state_root: stored_genesis.to_hex(),
        roots_match: genesis_root == stored_genesis,
    });

    let mut prev_hash = Hash32::ZERO;

    for height in 1..=up_to {
        let json: String = conn.query_row(
            "SELECT block_json FROM blocks WHERE height = ?1",
            [height],
            |r| r.get(0),
        )?;
        let block: Block = serde_json::from_str(&json)?;
        crate::model::static_check(&block)?;

        if block.height != height
            || block.prev_hash != prev_hash
            || block_hash_at_locked(&conn, height)? != Some(block.block_hash())
        {
            return Err(Error::Malformed(format!(
                "replay: block {height} failed linkage check"
            )));
        }

        // Independent validation/execution.
        let mut fees: i64 = 0;
        let mut created_here: HashSet<Op> = HashSet::new();
        let mut coinbase_paid: Option<i64> = None;
        for (idx, tx) in block.txs.iter().enumerate() {
            if tx.is_coinbase() {
                assert_eq!(idx, 0, "static_check enforces coinbase position");
                coinbase_paid = Some(tx.vout.iter().map(|o| o.value).sum());
            } else {
                let mut in_sum: i128 = 0;
                let mut spent_here: HashSet<Op> = HashSet::new();
                for i in &tx.vin {
                    let op = (i.txid, i.vout);
                    if !spent_here.insert(op) {
                        return Err(Error::DoubleSpend {
                            txid: op.0.to_hex(),
                            vout: op.1,
                        });
                    }
                    let (value, _) = set.get(&op).ok_or_else(|| Error::UnknownInput {
                        txid: op.0.to_hex(),
                        vout: op.1,
                    })?;
                    in_sum += *value as i128;
                }
                let out_sum: i128 = tx.vout.iter().map(|o| o.value as i128).sum();
                if in_sum < out_sum {
                    return Err(Error::InsufficientInputs {
                        inputs: in_sum as i64,
                        outputs: out_sum as i64,
                    });
                }
                fees += (in_sum - out_sum) as i64;
                for i in &tx.vin {
                    set.remove(&(i.txid, i.vout));
                }
            }
            for (v, o) in tx.vout.iter().enumerate() {
                let op = (tx.compute_txid(), v as u32);
                if !created_here.insert(op) {
                    return Err(Error::DuplicateTx(op.0.to_hex()));
                }
                set.insert(op, (o.value, o.address.clone()));
            }
        }

        let coinbase_paid = coinbase_paid.expect("coinbase present");
        let expected_reward = COINBASE_SUBSIDY
            .checked_add(fees)
            .ok_or(Error::Overflow)?;
        if coinbase_paid != expected_reward {
            return Err(Error::BadCoinbaseAmount {
                paid: coinbase_paid,
                expected: expected_reward,
            });
        }

        let root = root_of_map(&set);
        let stored = state_root_at_locked(&conn, height)?;
        let bh = block.block_hash();
        out.push(ReplayHeight {
            height,
            block_hash: bh.to_hex(),
            replayed_state_root: root.to_hex(),
            stored_state_root: stored.to_hex(),
            roots_match: root == stored,
        });
        prev_hash = bh;
    }

    Ok(out)
}

/// State root over an in-memory set, using the *same* canonical encoding as
/// the SQL implementation (the encoding is part of the protocol, not logic
/// being cross-checked).
fn root_of_map(set: &HashMap<Op, (i64, String)>) -> Hash32 {
    let mut entries: Vec<(Hash32, u32, i64, &str)> = set
        .iter()
        .map(|((txid, vout), (value, addr))| (*txid, *vout, *value, addr.as_str()))
        .collect();
    entries.sort_unstable_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)));
    state_root_from_sorted(entries)
}
