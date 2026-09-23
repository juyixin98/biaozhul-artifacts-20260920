//! Regenerate examples/*.json and examples/expected.json.
//!
//! Run from the repo root: `cargo run --bin gen-fixtures`.
//!
//! The generator executes every block against a real in-memory chain, so all
//! expected txids / block hashes / state roots are actually computed — never
//! hand-typed.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use utxo_rollback::chain;
use utxo_rollback::db::Db;
use utxo_rollback::error::COINBASE_SUBSIDY;
use utxo_rollback::hash::Hash32;
use utxo_rollback::model::Block;
use utxo_rollback::{block as make_block, coinbase_tx, spending_tx};

const ALICE: &str = "addr_alice";
const BOB: &str = "addr_bob";
const CAROL: &str = "addr_carol";
const MINER: &str = "addr_miner";

fn write_json(dir: &Path, name: &str, v: &serde_json::Value) {
    let s = serde_json::to_string_pretty(v).unwrap();
    fs::write(dir.join(name), format!("{s}\n")).unwrap();
    println!("wrote examples/{name}");
}

/// Expect an error with the given code when connecting.
fn assert_reject(db: &Db, block: &Block, expected_code: &'static str) {
    match chain::connect_block(db, block) {
        Ok(o) => panic!(
            "block {} unexpectedly accepted (expected {expected_code})",
            o.height
        ),
        Err(e) => {
            assert_eq!(
                e.code(),
                expected_code,
                "block rejected but with code {}: {e}",
                e.code()
            );
        }
    }
}

/// Connect a block and return (hash hex, state-root hex).
fn ok(db: &Db, block: &Block) -> (String, String, i64) {
    let o = chain::connect_block(db, block).unwrap();
    (o.hash.to_hex(), o.state_root.to_hex(), o.fees)
}

fn main() {
    let dir = PathBuf::from(std::env::args().nth(1).unwrap_or_else(|| "examples".into()));
    fs::create_dir_all(&dir).unwrap();

    let mut expected: BTreeMap<String, String> = BTreeMap::new();
    let db = Db::in_memory().unwrap();

    // ----- block 1: coinbase 5000 -> alice -----
    let b1 = make_block(
        1,
        Hash32::ZERO,
        vec![coinbase_tx(ALICE, COINBASE_SUBSIDY)],
        1_700_000_001,
    );
    let cb1_id = b1.txs[0].txid.unwrap().to_hex();
    let (h1, r1, _) = ok(&db, &b1);
    expected.insert("block1.hash".into(), h1.clone());
    expected.insert("block1.stateRoot".into(), r1.clone());
    expected.insert("block1.coinbaseTxid".into(), cb1_id.clone());
    expected.insert("block1.height".into(), "1".into());

    // ----- block 2: coinbase 5000 -> bob; alice pays carol 100 (fee 0) -----
    let tx2 = spending_tx(
        vec![(Hash32::from_hex(&cb1_id).unwrap(), 0, "sig-alice-0")],
        vec![(100, CAROL), (4900, ALICE)],
    );
    let tx2_id = tx2.txid.unwrap().to_hex();
    let b2 = make_block(
        2,
        Hash32::from_hex(&h1).unwrap(),
        vec![coinbase_tx(BOB, COINBASE_SUBSIDY), tx2],
        1_700_000_002,
    );
    let (h2, r2, _) = ok(&db, &b2);
    expected.insert("block2.hash".into(), h2.clone());
    expected.insert("block2.stateRoot".into(), r2.clone());
    expected.insert("block2.spendTxid".into(), tx2_id.clone());

    // Save blocks 1 and 2 (valid chain).
    write_json(&dir, "block1.json", &serde_json::to_value(&b1).unwrap());
    write_json(&dir, "block2.json", &serde_json::to_value(&b2).unwrap());

    // ----- rejected: intra-block double spend of alice's block2 change -----
    let ds1 = spending_tx(vec![(Hash32::from_hex(&tx2_id).unwrap(), 1, "ds-a")], vec![(4900, BOB)]);
    let ds2 = spending_tx(
        vec![(Hash32::from_hex(&tx2_id).unwrap(), 1, "ds-b")],
        vec![(4900, CAROL)],
    );
    let b3_double = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            ds1,
            ds2,
        ],
        1_700_000_003,
    );
    assert_reject(&db, &b3_double, "DOUBLE_SPEND");
    write_json(
        &dir,
        "block3-doublespend.json",
        &serde_json::to_value(&b3_double).unwrap(),
    );

    // ----- rejected: negative output amount (raw JSON, deserializer-level) ---
    let negative: serde_json::Value = serde_json::json!({
        "version": 1,
        "height": 3,
        "prevHash": h2,
        "timestamp": 1_700_000_003,
        "txs": [
            {"version": 1, "vin": [], "vout": [{"value": -1, "address": MINER}]},
            {
                "version": 1,
                "vin": [{"txid": tx2_id, "vout": 1, "scriptSig": hex::encode("sig")}],
                "vout": [{"value": 4900, "address": BOB}]
            }
        ]
    });
    let parsed_neg: Block = serde_json::from_value(negative.clone()).unwrap();
    assert_reject(&db, &parsed_neg, "NEGATIVE_AMOUNT");
    write_json(&dir, "block3-negative.json", &negative);
    expected.insert("invalid.negative.error".into(), "NEGATIVE_AMOUNT".into());

    // ----- rejected: input sum < output sum -----
    let overspend = spending_tx(
        vec![(Hash32::from_hex(&tx2_id).unwrap(), 1, "os")],
        vec![(5000, BOB)],
    );
    let b3_over = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY), overspend],
        1_700_000_003,
    );
    assert_reject(&db, &b3_over, "INSUFFICIENT_INPUTS");
    write_json(
        &dir,
        "block3-overspend.json",
        &serde_json::to_value(&b3_over).unwrap(),
    );

    // ----- rejected: unknown input -----
    let ghost = spending_tx(
        vec![(Hash32::ZERO, 7, "ghost")],
        vec![(1, BOB)],
    );
    let b3_ghost = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY), ghost],
        1_700_000_003,
    );
    assert_reject(&db, &b3_ghost, "UNKNOWN_INPUT");
    write_json(
        &dir,
        "block3-unknown-input.json",
        &serde_json::to_value(&b3_ghost).unwrap(),
    );

    // ----- rejected: coinbase pays too much -----
    let b3_badreward = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY + 1)],
        1_700_000_003,
    );
    assert_reject(&db, &b3_badreward, "BAD_COINBASE_AMOUNT");
    write_json(
        &dir,
        "block3-bad-reward.json",
        &serde_json::to_value(&b3_badreward).unwrap(),
    );

    // ----- rejected: height mismatch (pretends to be height 9) -----
    let mut b3_badheight = make_block(
        9,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY)],
        1_700_000_003,
    );
    assert_reject(&db, &b3_badheight, "HEIGHT_MISMATCH");
    b3_badheight.height = 3;
    // prevHash tampered: points at zero instead of block2
    let mut b3_badprev = b3_badheight.clone();
    b3_badprev.prev_hash = Hash32::ZERO;
    assert_reject(&db, &b3_badprev, "PREV_HASH_MISMATCH");
    write_json(
        &dir,
        "block3-bad-prev.json",
        &serde_json::to_value(&b3_badprev).unwrap(),
    );

    // ----- valid block 3 (canonical branch): miner collects 1 fee ----------
    let spend3 = spending_tx(
        vec![(Hash32::from_hex(&tx2_id).unwrap(), 1, "alice-split")],
        vec![(1000, BOB), (3899, ALICE)],
    );
    let spend3_id = spend3.txid.unwrap().to_hex();
    let b3 = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY + 1), spend3],
        1_700_000_003,
    );
    let (h3, r3, fee3) = ok(&db, &b3);
    assert_eq!(fee3, 1);
    expected.insert("block3.hash".into(), h3.clone());
    expected.insert("block3.stateRoot".into(), r3.clone());
    expected.insert("block3.spendTxid".into(), spend3_id);
    write_json(&dir, "block3.json", &serde_json::to_value(&b3).unwrap());

    // State root at historical height 2 must still match what we recorded.
    let r2_hist = utxo_rollback::db::state_root_at(&db, 2).unwrap().to_hex();
    assert_eq!(r2_hist, r2, "historical root at height 2 changed?!");
    expected.insert("history.height2.stateRoot".into(), r2_hist);

    // Replay from genesis through height 3 must match every stored root.
    let replay = chain::naive_replay(&db, 3).unwrap();
    assert!(replay.iter().all(|x| x.roots_match), "replay mismatch");
    expected.insert("replay.block3.allMatch".into(), "true".into());

    // ----- disconnect blocks 3 and 2 (two blocks at once) ------------------
    let disc = chain::disconnect_blocks(&db, 1).unwrap();
    assert_eq!(disc.disconnected, 2);
    assert_eq!(disc.tip_height, 1);
    assert_eq!(disc.tip_hash.to_hex(), h1);
    expected.insert("afterDisconnect2.tipHeight".into(), "1".into());
    expected.insert("afterDisconnect2.tipHash".into(), h1.clone());
    expected.insert(
        "afterDisconnect2.stateRoot".into(),
        disc.state_root.to_hex(),
    );
    assert_eq!(disc.state_root.to_hex(), r1);

    // ----- alternative block 2 (different tx) accepted on top of block 1 ---
    let alt_tx2 = spending_tx(
        vec![(Hash32::from_hex(&cb1_id).unwrap(), 0, "alt-alice")],
        vec![(2020, CAROL), (2020, ALICE)],
    ); // 5000 in, 4040 out -> 960 fee
    let alt_tx2_id = alt_tx2.txid.unwrap().to_hex();
    let alt_b2 = make_block(
        2,
        Hash32::from_hex(&h1).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY + 960), alt_tx2],
        1_700_000_102,
    );
    let (alt_h2, alt_r2, alt_fee2) = ok(&db, &alt_b2);
    assert_ne!(alt_h2, h2, "alternative must have a different hash");
    assert_eq!(alt_fee2, 960);
    expected.insert("alt.block2.hash".into(), alt_h2.clone());
    expected.insert("alt.block2.stateRoot".into(), alt_r2.clone());
    expected.insert("alt.block2.fee".into(), "960".into());
    expected.insert("alt.block2.spendTxid".into(), alt_tx2_id.clone());
    write_json(
        &dir,
        "alt-block2.json",
        &serde_json::to_value(&alt_b2).unwrap(),
    );

    // ----- illegal alternative block 3: spends an output only the ORIGINAL
    // block 2 created (carol's 100 from the disconnected tx2). The output no
    // longer exists, so this must be rejected with UNKNOWN_INPUT. ------------
    let alt_bad3 = make_block(
        3,
        Hash32::from_hex(&alt_h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            spending_tx(
                vec![(Hash32::from_hex(&tx2_id).unwrap(), 0, "stale-carol")],
                vec![(100, BOB)],
            ),
        ],
        1_700_000_103,
    );
    assert_reject(&db, &alt_bad3, "UNKNOWN_INPUT");
    write_json(
        &dir,
        "alt-block3-stale-input.json",
        &serde_json::to_value(&alt_bad3).unwrap(),
    );

    // ----- legal alternative block 3: spend carol's alt 2020, fee 20 -------
    let alt_b3 = make_block(
        3,
        Hash32::from_hex(&alt_h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY + 20),
            spending_tx(
                vec![(Hash32::from_hex(&alt_tx2_id).unwrap(), 0, "carol-alt")],
                vec![(2000, BOB)],
            ),
        ],
        1_700_000_103,
    );
    let (alt_h3, alt_r3, alt_fee3) = ok(&db, &alt_b3);
    assert_eq!(alt_fee3, 20);
    expected.insert("alt.block3.hash".into(), alt_h3);
    expected.insert("alt.block3.stateRoot".into(), alt_r3);
    expected.insert("alt.block3.fee".into(), "20".into());
    write_json(
        &dir,
        "alt-block3.json",
        &serde_json::to_value(&alt_b3).unwrap(),
    );

    // Replay must match the alternative chain too.
    let replay_alt = chain::naive_replay(&db, 3).unwrap();
    assert!(replay_alt.iter().all(|x| x.roots_match));
    expected.insert("replay.alt3.allMatch".into(), "true".into());

    // Error codes recorded for the acceptance script.
    expected.insert("invalid.doubleSpend.error".into(), "DOUBLE_SPEND".into());
    expected.insert("invalid.overspend.error".into(), "INSUFFICIENT_INPUTS".into());
    expected.insert("invalid.unknownInput.error".into(), "UNKNOWN_INPUT".into());
    expected.insert("invalid.badReward.error".into(), "BAD_COINBASE_AMOUNT".into());
    expected.insert("invalid.badPrev.error".into(), "PREV_HASH_MISMATCH".into());
    expected.insert("invalid.height.error".into(), "HEIGHT_MISMATCH".into());
    expected.insert("invalid.altStale.error".into(), "UNKNOWN_INPUT".into());

    // Quiet unused-variable guard if a case changes.
    let _ = r3;

    let exp_json = serde_json::to_string_pretty(&expected).unwrap();
    fs::write(dir.join("expected.json"), format!("{exp_json}\n")).unwrap();
    println!("wrote examples/expected.json ({} keys)", expected.len());
}
