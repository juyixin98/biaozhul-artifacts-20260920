//! Naive replay reference implementation.
//!
//! Deliberately *independent* re-validation of the whole active chain: reads
//! stored blocks in height order and rebuilds the UTXO set from nothing with
//! plain `HashMap`s, re-running every protocol rule with its own code paths
//! (no sharing with the connect-time validator). Its state root at every
//! height is compared against the root the database recorded at connect time —
//! the required cross-check against a naive implementation.

use std::collections::{HashMap, HashSet};

use rusqlite::Connection;

use crate::crypto;
use crate::error::{AppError, AppResult};
use crate::types::block_subsidy;

type Op = (Vec<u8>, u32);

#[derive(Clone)]
struct Coin {
    value: u64,
    address: [u8; 32],
    born: u64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct ReplayReport {
    pub blocks_replayed: u64,
    pub tip_hash: String,
    pub replayed_state_root: String,
    pub stored_state_root: String,
    pub roots_match: bool,
    pub utxo_count: usize,
    pub total_supply: u64,
    /// Per-height root comparisons: `(height, stored, replayed, equal)`.
    pub per_height: Vec<(u64, String, String, bool)>,
}

pub fn replay_active_chain(conn: &Connection) -> AppResult<ReplayReport> {
    let count: i64 = conn.query_row("SELECT COUNT(*) FROM blocks", [], |r| r.get(0))?;
    let mut per_height = Vec::new();
    let mut utxos: HashMap<Op, Coin> = HashMap::new();
    let mut prev = [0u8; 32];
    let mut tip_hash = vec![0u8; 32];

    for height in 0..count as u64 {
        let (hash, prev_hash, timestamp, merkle_db, root_db, fee_db, data): (
            Vec<u8>,
            Vec<u8>,
            i64,
            Vec<u8>,
            Vec<u8>,
            i64,
            String,
        ) = conn.query_row(
            "SELECT hash, prev_hash, timestamp, merkle_root, state_root, fee_total, data
             FROM blocks WHERE height = ?1",
            [height as i64],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                ))
            },
        )?;

        if prev_hash != prev {
            return Err(AppError::internal(format!(
                "replay: broken parent link at height {height}"
            )));
        }

        let block: crate::types::Block = serde_json::from_str(&data)
            .map_err(|e| AppError::internal(format!("replay: decode block {height}: {e}")))?;

        let txids: Vec<[u8; 32]> = block
            .txs
            .iter()
            .map(crypto::txid)
            .collect::<Result<_, _>>()
            .map_err(|e| AppError::internal(format!("replay: txid at {height}: {e}")))?;

        let merkle = crypto::merkle_root(&txids);
        if merkle.as_slice() != merkle_db.as_slice() {
            return Err(AppError::internal(format!(
                "replay: stored merkle root wrong at height {height}"
            )));
        }
        let prev_arr: [u8; 32] = prev_hash.clone().try_into().unwrap();
        let header_hash =
            crypto::hash256(&crypto::serialize_header(height, &prev_arr, timestamp, &merkle));
        if header_hash.as_slice() != hash.as_slice() {
            return Err(AppError::internal(format!(
                "replay: stored block hash wrong at height {height}"
            )));
        }

        // ---- independent in-order validation + application --------------
        let mut added: HashMap<Op, Coin> = HashMap::new();
        let mut spent: HashSet<Op> = HashSet::new();
        let coinbase_ops: HashSet<Op> = block.txs[0]
            .outputs
            .iter()
            .enumerate()
            .map(|(v, _)| (txids[0].to_vec(), v as u32))
            .collect();
        let mut fees: u128 = 0;

        for (ti, tx) in block.txs.iter().enumerate() {
            if ti == 0 {
                if tx.inputs.len() != 1 || tx.inputs[0].prev.is_some() {
                    return Err(fail(height, "bad coinbase shape"));
                }
                if tx.inputs[0].coinbase_tag != hex::encode(height.to_le_bytes()) {
                    return Err(fail(height, "bad coinbase height tag"));
                }
            } else {
                let mut in_sum: u128 = 0;
                let mut local: HashSet<Op> = HashSet::new();
                for input in &tx.inputs {
                    let p = input
                        .prev
                        .as_ref()
                        .ok_or_else(|| fail(height, "non-coinbase input without prev"))?;
                    let prev_txid = hex::decode(&p.txid)
                        .map_err(|_| fail(height, "bad outpoint hex"))?;
                    let op = (prev_txid, p.vout);

                    if coinbase_ops.contains(&op) {
                        return Err(fail(height, "spending coinbase in same block"));
                    }
                    if !local.insert(op.clone()) || spent.contains(&op) {
                        return Err(fail(height, "double spend in block"));
                    }
                    let coin = added
                        .get(&op)
                        .cloned()
                        .or_else(|| utxos.get(&op).cloned())
                        .ok_or_else(|| fail(height, "missing or already-spent input"))?;

                    let pk =
                        hex::decode(&input.pubkey).map_err(|_| fail(height, "bad pubkey hex"))?;
                    if pk.len() != 32 || pk != coin.address {
                        return Err(fail(height, "pubkey does not own output"));
                    }
                    let sig = hex::decode(&input.signature)
                        .map_err(|_| fail(height, "bad signature hex"))?;
                    if !crypto::verify_signature(&pk, &txids[ti], &sig) {
                        return Err(fail(height, "invalid signature"));
                    }

                    in_sum += coin.value as u128;
                    if coin.born != height {
                        utxos.remove(&op);
                    }
                    spent.insert(op);
                }
                let out_sum: u128 = tx.outputs.iter().map(|o| o.value as u128).sum();
                if in_sum < out_sum {
                    return Err(fail(height, "input value insufficient"));
                }
                fees += in_sum - out_sum;
            }

            for (v, o) in tx.outputs.iter().enumerate() {
                let addr =
                    hex::decode(&o.address).map_err(|_| fail(height, "bad address hex"))?;
                added.insert(
                    (txids[ti].to_vec(), v as u32),
                    Coin {
                        value: o.value,
                        address: addr.try_into().unwrap(),
                        born: height,
                    },
                );
            }
        }

        let coinbase_total: u128 = block.txs[0].outputs.iter().map(|o| o.value as u128).sum();
        let expected = block_subsidy(height) as u128 + fees;
        if coinbase_total != expected {
            return Err(fail(height, "coinbase reward rule violated"));
        }
        if fees as i64 != fee_db {
            return Err(AppError::internal(format!(
                "replay: fee mismatch at height {height}: naive {fees} stored {fee_db}"
            )));
        }

        for (op, coin) in added {
            if !spent.contains(&op) {
                utxos.insert(op, coin);
            }
        }

        // ---- root cross-check -------------------------------------------
        let rows = root_rows(&utxos);
        let naive_root = crypto::state_root_from_rows(rows);
        let equal = naive_root.as_slice() == root_db.as_slice();
        per_height.push((
            height,
            hex::encode(&root_db),
            hex::encode(naive_root),
            equal,
        ));
        if !equal {
            return Err(AppError::internal(format!(
                "replay: state root mismatch at height {height}"
            )));
        }

        prev = hash.clone().try_into().unwrap();
        tip_hash = hash;
    }

    let stored_tip = crate::storage::recompute_state_root(conn)?;
    let final_root = crypto::state_root_from_rows(root_rows(&utxos));
    let total_supply: u128 = utxos.values().map(|c| c.value as u128).sum();
    let all_equal = per_height.iter().all(|(_, _, _, eq)| *eq)
        && final_root.as_slice() == stored_tip.as_slice();

    Ok(ReplayReport {
        blocks_replayed: count as u64,
        tip_hash: hex::encode(&tip_hash),
        replayed_state_root: hex::encode(final_root),
        stored_state_root: hex::encode(stored_tip),
        roots_match: all_equal,
        utxo_count: utxos.len(),
        total_supply: total_supply.min(u64::MAX as u128) as u64,
        per_height,
    })
}

fn root_rows(utxos: &HashMap<Op, Coin>) -> Vec<([u8; 32], u32, u64, [u8; 32], u64)> {
    utxos
        .iter()
        .map(|((t, v), c)| (t.clone().try_into().unwrap(), *v, c.value, c.address, c.born))
        .collect()
}

fn fail(height: u64, msg: &str) -> AppError {
    AppError::internal(format!(
        "naive replay rejected active chain at height {height}: {msg}"
    ))
}
