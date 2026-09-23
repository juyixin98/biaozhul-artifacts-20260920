//! Engine-level integration tests covering every required scenario:
//! in-block double spend, consecutive disconnects, illegal alternative
//! blocks, restart persistence, atomicity on rejection, fixed coinbase rule,
//! historical queries (never the current set), and the naive-replay
//! cross-check.

mod common;

use common::*;
use tempfile::TempDir;

use utxo_rollback::builder::UtxoRef;
use utxo_rollback::crypto;
use utxo_rollback::types::block_subsidy;
use utxo_rollback::Storage;

fn temp_db() -> (TempDir, String) {
    let dir = TempDir::new().expect("tempdir");
    let path = dir
        .path()
        .join("chain.db")
        .to_string_lossy()
        .into_owned();
    (dir, path)
}

fn open(path: &str) -> Storage {
    Storage::open(path).expect("open storage")
}

fn err_msg<T>(r: utxo_rollback::AppResult<T>) -> String {
    match r {
        Err(utxo_rollback::AppError::Conflict(m)) => m,
        Err(other) => panic!("expected Conflict, got {other:?}"),
        Ok(_) => panic!("expected error"),
    }
}

// --------------------------------------------------------------------------
// Happy path + fixed coinbase reward rule
// --------------------------------------------------------------------------

#[test]
fn accepts_well_formed_chain_and_applies_subsidy_rule() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, _bob) = wallets();

    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    let r = s.connect_block(b0).expect("genesis");
    assert_eq!(r.height, 0);
    assert_eq!(r.fee_total, 0);

    let (b1, pay_id) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let r = s.connect_block(b1).expect("block1");
    assert_eq!(r.height, 1);
    assert_eq!(r.fee_total, 5);

    // Live UTXOs: alice 30, miner change 15, miner coinbase 55.
    let (_, utxos) = s.current_utxos(None).unwrap();
    assert_eq!(utxos.len(), 3);
    let total: u64 = utxos.iter().map(|u| u.value).sum();
    assert_eq!(total, 100); // 2 * subsidy, fees recycled to miner

    // alice's output exists.
    let (_, alice_utxos) = s.current_utxos(Some(&alice.address_hex())).unwrap();
    assert_eq!(alice_utxos.len(), 1);
    assert_eq!(alice_utxos[0].txid, hex::encode(pay_id));
}

#[test]
fn coinbase_reward_is_exact_subsidy_plus_fees() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    // Build a valid spend with fee 5, but coinbase claims 56 (one too many).
    let (mut bad, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    bad.txs[0].outputs[0].value = 56;
    // recompute merkle root honestly: the coinbase output changed the txid.
    let txids = crypto::block_txids(&bad).unwrap();
    bad.merkle_root = hex::encode(crypto::merkle_root(&txids));

    let m = err_msg(s.connect_block(bad));
    assert!(m.contains("coinbase creates"), "got: {m}");

    // Subsidy schedule sanity: 50 at 0..99, 25 at 100..199, 0 eventually.
    assert_eq!(block_subsidy(0), 50);
    assert_eq!(block_subsidy(99), 50);
    assert_eq!(block_subsidy(100), 25);
    for _ in 0..64 {
        // reaches zero
    }
    // 64 halvings later it is 0 (50 >> 63 is 0 in practice well before that).
    assert_eq!(block_subsidy(6400), 0);
}

// --------------------------------------------------------------------------
// In-block double spend
// --------------------------------------------------------------------------

#[test]
fn rejects_in_block_double_spend_without_touching_state() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, _alice, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    let root_before = s.tip().unwrap().unwrap().state_root;
    let bad = double_spend_block1(h0, &miner.address_hex(), crypto::txid(&block0(&miner).txs[0]).unwrap(), &miner);
    let m = err_msg(s.connect_block(bad));
    assert!(m.contains("double spend"), "got: {m}");

    // State untouched: tip still height 0, same root.
    let tip = s.tip().unwrap().unwrap();
    assert_eq!(tip.height, 0);
    assert_eq!(tip.state_root, root_before);

    // A legal block1 still connects afterwards.
    let recipient = Wallet::generate();
    let (good, _) = block1(&miner, h0, &recipient.address_hex(), 10, 5);
    s.connect_block(good).expect("legal block after rejected one");
    assert_eq!(s.tip().unwrap().unwrap().height, 1);
}

#[test]
fn rejects_cross_transaction_double_spend_in_same_block() {
    use utxo_rollback::builder::{sign_spend, unsigned_spend};
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, _a, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    let gtx = crypto::txid(&block0(&miner).txs[0]).unwrap();
    let coin = UtxoRef {
        txid: gtx,
        vout: 0,
        value: 50,
    };
    // Two separately-constructed txs spending the same outpoint; distinct
    // output values give them different (individually valid) txids.
    let tx_a = sign_spend(
        unsigned_spend(
            &[coin.clone()],
            vec![utxo_rollback::types::TxOut {
                value: 25,
                address: miner.address_hex(),
            }],
        ),
        &miner,
    );
    let tx_b = sign_spend(
        unsigned_spend(
            &[coin],
            vec![utxo_rollback::types::TxOut {
                value: 24,
                address: miner.address_hex(),
            }],
        ),
        &miner,
    );
    let block = common::build_block(
        1,
        h0,
        1,
        vec![utxo_rollback::types::TxOut {
            value: 50,
            address: miner.address_hex(),
        }],
        vec![tx_a, tx_b],
    );
    let m = err_msg(s.connect_block(block));
    assert!(m.contains("double spend"), "got: {m}");
}

// --------------------------------------------------------------------------
// Missing / spent inputs, insufficient inputs, bad signatures, ownership
// --------------------------------------------------------------------------

#[test]
fn rejects_spending_nonexistent_and_already_spent_inputs() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    // Unknown outpoint: fabricated txid.
    use utxo_rollback::builder::{sign_spend, unsigned_spend};
    use utxo_rollback::types::{OutPoint, TxIn};
    let bogus = UtxoRef {
        txid: [0x42u8; 32],
        vout: 0,
        value: 10,
    };
    let tx = sign_spend(
        unsigned_spend(
            &[bogus],
            vec![utxo_rollback::types::TxOut {
                value: 10,
                address: miner.address_hex(),
            }],
        ),
        &miner,
    );
    let block = common::build_block(
        1,
        h0,
        1,
        vec![utxo_rollback::types::TxOut {
            value: 50,
            address: miner.address_hex(),
        }],
        vec![tx],
    );
    let m = err_msg(s.connect_block(block));
    assert!(m.contains("does not exist or is already spent"), "got: {m}");

    // Spend a real one, then try to spend it again in a later block.
    let (good, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let h1 = block_hash(&good);
    s.connect_block(good).unwrap();
    let replay_input = TxIn {
        prev: Some(OutPoint {
            txid: hex::encode(crypto::txid(&block0(&miner).txs[0]).unwrap()),
            vout: 0,
        }),
        coinbase_tag: String::new(),
        pubkey: miner.address_hex(),
        signature: String::new(),
    };
    let mut tx = utxo_rollback::types::Transaction {
        inputs: vec![replay_input],
        outputs: vec![utxo_rollback::types::TxOut {
            value: 1,
            address: miner.address_hex(),
        }],
    };
    let id = crypto::txid(&tx).unwrap();
    tx.inputs[0].signature = miner.sign_txid(&id);
    let block2b = common::build_block(
        2,
        h1,
        2,
        vec![utxo_rollback::types::TxOut {
            value: 50,
            address: miner.address_hex(),
        }],
        vec![tx],
    );
    let m = err_msg(s.connect_block(block2b));
    assert!(m.contains("already spent"), "got: {m}");
}

#[test]
fn rejects_insufficient_inputs_and_bad_signature_and_wrong_owner() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, _a, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    // 50 in, 60 out: insufficient; coinbase exactly 50 (no fee possible).
    use utxo_rollback::builder::{unsigned_spend};
    use utxo_rollback::types::TxOut;
    let coin = UtxoRef {
        txid: crypto::txid(&block0(&miner).txs[0]).unwrap(),
        vout: 0,
        value: 50,
    };
    let mut tx = unsigned_spend(
        &[coin.clone()],
        vec![TxOut {
            value: 60,
            address: miner.address_hex(),
        }],
    );
    let id = crypto::txid(&tx).unwrap();
    tx = utxo_rollback::builder::sign_spend(tx, &miner);
    let block = common::build_block(
        1, h0, 1,
        vec![TxOut { value: 50, address: miner.address_hex() }],
        vec![tx],
    );
    let m = err_msg(s.connect_block(block));
    assert!(m.contains("input insufficient"), "got: {m}");
    let _ = id;

    // Valid spend but signature from a different key.
    let thief = Wallet::generate();
    let mut tx = unsigned_spend(
        &[coin],
        vec![TxOut {
            value: 10,
            address: thief.address_hex(),
        }],
    );
    let id = crypto::txid(&tx).unwrap();
    let thief_sig = thief.sign_txid(&id);
    tx.inputs[0].pubkey = miner.address_hex();
    tx.inputs[0].signature = thief_sig; // signed by wrong key
    let block = common::build_block(
        1, h0, 1,
        vec![TxOut { value: 50, address: miner.address_hex() }],
        vec![tx],
    );
    let m = err_msg(s.connect_block(block));
    assert!(m.contains("invalid Ed25519 signature"), "got: {m}");
}

// --------------------------------------------------------------------------
// Negative amounts are rejected at the wire layer (u64 parse error)
// --------------------------------------------------------------------------

#[test]
fn negative_amount_is_a_parse_error_not_accepted() {
    let json = r#"{
        "height": 0,
        "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
        "timestamp": 0,
        "merkle_root": "0000000000000000000000000000000000000000000000000000000000000000",
        "txs": [{
            "inputs": [{"prev": null, "pubkey": "", "signature": ""}],
            "outputs": [{"value": -5, "address": "0000000000000000000000000000000000000000000000000000000000000000"}]
        }]
    }"#;
    let parse: Result<utxo_rollback::types::Block, _> = serde_json::from_str(json);
    assert!(parse.is_err(), "negative value must fail u64 deserialization");
}

// --------------------------------------------------------------------------
// Consecutive disconnects + reconnect reproduces identical roots
// --------------------------------------------------------------------------

#[test]
fn disconnects_blocks_consecutively_and_restores_utxos() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, bob) = wallets();

    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();
    let (b1, alice_coin_id) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let h1 = block_hash(&b1);
    let root1 = s.connect_block(b1).unwrap().state_root;

    let alice_coin = UtxoRef {
        txid: alice_coin_id,
        vout: 0,
        value: 30,
    };
    let (b2, _) = block2(&alice, alice_coin, h1, &miner, &bob.address_hex(), 10, 1);
    let root2 = s.connect_block(b2).unwrap().state_root;
    assert_ne!(root1, root2);

    // Roll back block 2: alice's 30 must be live again, bob's 10 gone.
    let r = s.disconnect_tip().unwrap();
    assert_eq!(r.height, 2);
    assert_eq!(r.new_tip_height, Some(1));
    assert_eq!(r.state_root, root1);
    let (_, bob_utxos) = s.current_utxos(Some(&bob.address_hex())).unwrap();
    assert!(bob_utxos.is_empty());
    let (_, alice_utxos) = s.current_utxos(Some(&alice.address_hex())).unwrap();
    assert_eq!(alice_utxos.len(), 1);
    assert_eq!(alice_utxos[0].value, 30);

    // Roll back block 1: genesis-only state, exactly the miner 50.
    let r = s.disconnect_tip().unwrap();
    assert_eq!(r.height, 1);
    assert_eq!(r.new_tip_height, Some(0));
    let (_, utxos) = s.current_utxos(None).unwrap();
    assert_eq!(utxos.len(), 1);
    assert_eq!(utxos[0].value, 50);

    // Roll back genesis: empty chain.
    let r = s.disconnect_tip().unwrap();
    assert_eq!(r.height, 0);
    assert!(r.new_tip_height.is_none());
    assert!(s.tip().unwrap().is_none());
    assert!(s.disconnect_tip().is_err());
}

#[test]
fn reconnecting_after_rollback_reproduces_identical_state_root() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0.clone()).unwrap();
    let (b1, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let root1_first = s.connect_block(b1.clone()).unwrap().state_root;

    s.disconnect_tip().unwrap();
    let root0 = s.tip().unwrap().unwrap().state_root;

    let root1_again = s.connect_block(b1).unwrap().state_root;
    assert_eq!(root1_first, root1_again, "undo + reapply must be deterministic");
    assert_ne!(root0, root1_first);

    // Disconnect block1 AND genesis, rebuilding the whole chain from an
    // empty store: root0 must be reproduced exactly.
    s.disconnect_tip().unwrap();
    s.disconnect_tip().unwrap();
    assert!(s.tip().unwrap().is_none());
    s.connect_block(b0).unwrap();
    assert_eq!(s.tip().unwrap().unwrap().state_root, root0);
}

// --------------------------------------------------------------------------
// Alternative blocks / reorgs
// --------------------------------------------------------------------------

#[test]
fn connects_alternative_same_height_block_and_removes_old_utxos() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, bob) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    let (canonical, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let root_canonical = s.connect_block(canonical).unwrap().state_root;

    // Alternative block1: pays bob instead, same parent h0, same height 1.
    let (alt, _) = block1(&miner, h0, &bob.address_hex(), 20, 5);
    let r = s.connect_block(alt).expect("alternative must connect");
    assert!(r.reorged);
    assert_eq!(r.disconnected_heights, vec![1]);

    let tip = s.tip().unwrap().unwrap();
    assert_eq!(tip.height, 1);
    assert_ne!(tip.state_root, root_canonical);

    // alice's old-chain output is gone; bob's exists.
    let (_, a) = s.current_utxos(Some(&alice.address_hex())).unwrap();
    assert!(a.is_empty());
    let (_, b) = s.current_utxos(Some(&bob.address_hex())).unwrap();
    assert_eq!(b.len(), 1);
    assert_eq!(b[0].value, 20);
}

#[test]
fn rejects_illegal_alternative_blocks() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();

    let (canonical, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    s.connect_block(canonical).unwrap();

    // (a) Unknown parent hash.
    let (unknown, _) = block1(&miner, [0x99u8; 32], &alice.address_hex(), 10, 5);
    let m = err_msg(s.connect_block(unknown));
    assert!(m.contains("parent block is unknown"), "got: {m}");

    // (b) Same-height alt whose transactions are invalid (double spend).
    let bad = double_spend_block1(h0, &miner.address_hex(), crypto::txid(&block0(&miner).txs[0]).unwrap(), &miner);
    let root_before = s.tip().unwrap().unwrap().state_root;
    let m = err_msg(s.connect_block(bad));
    assert!(m.contains("double spend"), "got: {m}");
    // Failed reorg must roll back the tentative disconnect: canonical tip intact.
    assert_eq!(s.tip().unwrap().unwrap().state_root, root_before);
    assert_eq!(s.tip().unwrap().unwrap().height, 1);

    // (c) Wrong height for a known parent (claims height 7 after genesis;
    //     coinbase tag recomputed so height attachment is the only error).
    let (mut wrong_height, _) = block1(&miner, h0, &alice.address_hex(), 10, 5);
    wrong_height.height = 7;
    wrong_height.txs[0].inputs[0].coinbase_tag = hex::encode(7u64.to_le_bytes());
    let ids = crypto::block_txids(&wrong_height).unwrap();
    wrong_height.merkle_root = hex::encode(crypto::merkle_root(&ids));
    let m = err_msg(s.connect_block(wrong_height));
    assert!(
        m.contains("height must follow parent") || m.contains("does not replace current tip"),
        "got: {m}"
    );

    // (d) Cannot replace a block once the chain has grown past it: tip is at
    //     height 2, a height-1 alternative rooted at h0 is refused (only
    //     same-height tip replacement is supported; deeper reorgs are driven
    //     by disconnecting first or submitting alternatives in order).
    let (_, alice_id) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let h1_real: [u8; 32] =
        hex::decode(&s.active_block_at_height(1).unwrap().unwrap().hash).unwrap().try_into().unwrap();
    let (b2, _) = common::block2(
        &alice,
        UtxoRef {
            txid: alice_id,
            vout: 0,
            value: 30,
        },
        h1_real,
        &miner,
        &alice.address_hex(),
        10,
        1,
    );
    s.connect_block(b2).unwrap();
    assert_eq!(s.tip().unwrap().unwrap().height, 2);
    let (deep_alt, _) = block1(&miner, h0, &alice.address_hex(), 5, 5);
    let m = err_msg(s.connect_block(deep_alt));
    assert!(m.contains("only same-height replacement"), "got: {m}");
}

// --------------------------------------------------------------------------
// Historical queries require explicit height and never masquerade as current
// --------------------------------------------------------------------------

#[test]
fn historical_utxos_are_scoped_to_explicit_height() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, _) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    let r0 = s.connect_block(b0).unwrap();
    let (b1, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let r1 = s.connect_block(b1).unwrap();

    // Height 0 history: only the miner 50.
    let (root_hist0, at0) = s.utxo_at_height(0, None).unwrap();
    assert_eq!(root_hist0, r0.state_root);
    assert_eq!(at0.len(), 1);
    assert_eq!(at0[0].value, 50);

    // Height 1 history equals the recorded root and has three outputs.
    let (root_hist1, at1) = s.utxo_at_height(1, None).unwrap();
    assert_eq!(root_hist1, r1.state_root);
    assert_eq!(at1.len(), 3);

    // Current == height 1 right now (tip at 1).
    let (root_cur, cur) = s.current_utxos(None).unwrap();
    assert_eq!(root_cur, root_hist1);
    assert_eq!(cur.len(), at1.len());

    // Asking an unknown height is 404-style, never "current UTXO".
    let missing = s.utxo_at_height(99, None);
    assert!(matches!(missing, Err(utxo_rollback::AppError::NotFound(_))));
}

// --------------------------------------------------------------------------
// Restart persistence
// --------------------------------------------------------------------------

#[test]
fn state_survives_restart_and_all_undo_records_remain_usable() {
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("r.db").to_string_lossy().into_owned();

    let roots;
    let (miner, alice, bob) = wallets();
    {
        let s = open(&path);
        let b0 = block0(&miner);
        let h0 = block_hash(&b0);
        let r0 = s.connect_block(b0).unwrap();
        let (b1, alice_id) = block1(&miner, h0, &alice.address_hex(), 30, 5);
        let h1 = block_hash(&b1);
        let r1 = s.connect_block(b1).unwrap();
        let (b2, _) = common::block2(
            &alice,
            UtxoRef {
                txid: alice_id,
                vout: 0,
                value: 30,
            },
            h1,
            &miner,
            &bob.address_hex(),
            10,
            1,
        );
        let r2 = s.connect_block(b2).unwrap();
        roots = (r0.state_root, r1.state_root, r2.state_root);
    } // storage dropped, database closed

    // Reopen: tip, roots and history are all still there.
    let s = open(&path);
    let tip = s.tip().unwrap().unwrap();
    assert_eq!(tip.height, 2);
    assert_eq!(tip.state_root, roots.2);
    let (h0root, _) = s.utxo_at_height(0, None).unwrap();
    assert_eq!(h0root, roots.0);
    let (h1root, _) = s.utxo_at_height(1, None).unwrap();
    assert_eq!(h1root, roots.1);

    // Undo records survived: roll back two blocks after restart.
    s.disconnect_tip().unwrap();
    s.disconnect_tip().unwrap();
    assert_eq!(s.tip().unwrap().unwrap().height, 0);
    assert_eq!(s.tip().unwrap().unwrap().state_root, roots.0);

    // Naive replay on the reopened database agrees at every remaining height.
    let report = s.run_naive_replay().unwrap();
    assert!(report.roots_match);
    assert_eq!(report.blocks_replayed, 1);
}

// --------------------------------------------------------------------------
// Naive replay cross-check
// --------------------------------------------------------------------------

#[test]
fn naive_replay_matches_storage_along_the_whole_chain() {
    let (_d, path) = temp_db();
    let s = open(&path);
    let (miner, alice, bob) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);
    s.connect_block(b0).unwrap();
    let (b1, alice_id) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let h1 = block_hash(&b1);
    s.connect_block(b1).unwrap();
    let (b2, _) = common::block2(
        &alice,
        UtxoRef {
            txid: alice_id,
            vout: 0,
            value: 30,
        },
        h1,
        &miner,
        &bob.address_hex(),
        10,
        1,
    );
    s.connect_block(b2).unwrap();

    let report = s.run_naive_replay().expect("replay");
    assert!(report.roots_match, "replay report: {report:?}");
    assert_eq!(report.blocks_replayed, 3);
    assert_eq!(report.per_height.len(), 3);
    // Total supply equals all subsidies paid (fees are redistributed, not
    // created): 50 * 3 = 150 at heights 0,1,2.
    assert_eq!(report.total_supply, 150);

    // After a rollback the replay still matches the shortened chain.
    s.disconnect_tip().unwrap();
    let report = s.run_naive_replay().unwrap();
    assert!(report.roots_match);
    assert_eq!(report.blocks_replayed, 2);
    assert_eq!(report.total_supply, 100);
}
