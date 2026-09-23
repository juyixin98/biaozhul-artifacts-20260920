//! End-to-end tests against the real Axum router with an in-memory SQLite DB.
//!
//! Covers: in-block double spend, consecutive block disconnection, illegal
//! alternative block, and restart persistence — plus the naive-replay
//! cross-check and the mandatory-height historical queries.

use std::sync::Arc;

use axum::Router;
use serde_json::{json, Value};
use tower::ServiceExt;

use utxo_rollback::api;
use utxo_rollback::db::Db;
use utxo_rollback::error::COINBASE_SUBSIDY;
use utxo_rollback::hash::Hash32;
use utxo_rollback::model::Block;
use utxo_rollback::{block as make_block, coinbase_tx, spending_tx};

const ALICE: &str = "alice";
const BOB: &str = "bob";
const CAROL: &str = "carol";
const MINER: &str = "miner";

async fn request(
    app: &Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (u16, Value) {
    let builder = axum::http::Request::builder().method(method).uri(uri);
    let req = match body {
        Some(v) => builder
            .header("content-type", "application/json")
            .body(axum::body::Body::from(v.to_string()))
            .unwrap(),
        None => builder.body(axum::body::Body::empty()).unwrap(),
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status().as_u16();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let v: Value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap()
    };
    (status, v)
}

fn test_app() -> Router {
    let db = Arc::new(Db::in_memory().unwrap());
    api::router(db)
}

async fn submit(app: &Router, block: &Block) -> (u16, Value) {
    request(app, "POST", "/blocks", Some(serde_json::to_value(block).unwrap())).await
}

async fn submit_value(app: &Router, block: Value) -> (u16, Value) {
    request(app, "POST", "/blocks", Some(block)).await
}

async fn get_json(app: &Router, uri: &str) -> (u16, Value) {
    request(app, "GET", uri, None).await
}

async fn post_json(app: &Router, uri: &str, v: Value) -> (u16, Value) {
    request(app, "POST", uri, Some(v)).await
}

/// Build blocks 1 and 2 of the standard chain; returns their hashes/txids.
async fn seed_two_blocks(app: &Router) -> (String, String, String, String) {
    let b1 = make_block(
        1,
        Hash32::ZERO,
        vec![coinbase_tx(ALICE, COINBASE_SUBSIDY)],
        11,
    );
    let cb1 = b1.txs[0].txid.unwrap();
    let (s, r) = submit(app, &b1).await;
    assert_eq!(s, 201, "block1: {r}");
    let h1 = r["hash"].as_str().unwrap().to_string();

    let tx2 = spending_tx(
        vec![(cb1, 0, "sig-a")],
        vec![(100, CAROL), (4900, ALICE)],
    );
    let tx2id = tx2.txid.unwrap();
    let b2 = make_block(
        2,
        Hash32::from_hex(&h1).unwrap(),
        vec![coinbase_tx(BOB, COINBASE_SUBSIDY), tx2],
        12,
    );
    let (s, r) = submit(app, &b2).await;
    assert_eq!(s, 201, "block2: {r}");
    let h2 = r["hash"].as_str().unwrap().to_string();

    (h1, h2, cb1.to_hex(), tx2id.to_hex())
}

#[tokio::test]
async fn health_and_genesis() {
    let app = test_app();
    let (s, r) = get_json(&app, "/health").await;
    assert_eq!(s, 200);
    assert_eq!(r["status"], "ok");

    let (s, r) = get_json(&app, "/tip").await;
    assert_eq!(s, 200);
    assert_eq!(r["height"], 0);
    assert_eq!(
        r["hash"],
        "0000000000000000000000000000000000000000000000000000000000000000"
    );
}

#[tokio::test]
async fn accepts_valid_chain_and_fees_flow() {
    let app = test_app();
    let (_h1, _h2, _cb1, tx2id) = seed_two_blocks(&app).await;

    // Block 3: spend alice's 4900 change -> 4899 out, 1 fee.
    let tx3 = spending_tx(
        vec![(Hash32::from_hex(&tx2id).unwrap(), 1, "a2")],
        vec![(4899, BOB)],
    );
    let b3 = make_block(
        3,
        Hash32::from_hex(&_h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY + 1), tx3],
        13,
    );
    let (s, r) = submit(&app, &b3).await;
    assert_eq!(s, 201, "{r}");
    assert_eq!(r["fees"], 1);

    // Naive replay agrees at every height (including genesis).
    let (s, r) = get_json(&app, "/replay/verify").await;
    assert_eq!(s, 200);
    assert_eq!(r["all_match"], true, "{r}");
    assert_eq!(r["heights"].as_array().unwrap().len(), 4);
}

#[tokio::test]
async fn rejects_intra_block_double_spend_and_rolls_back() {
    let app = test_app();
    let (_h1, h2, _cb1, tx2id) = seed_two_blocks(&app).await;

    let txid = Hash32::from_hex(&tx2id).unwrap();
    let ds1 = spending_tx(vec![(txid, 1, "ds1")], vec![(4900, BOB)]);
    let ds2 = spending_tx(vec![(txid, 1, "ds2")], vec![(4900, CAROL)]);
    let bad = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY), ds1, ds2],
        13,
    );
    let (s, r) = submit(&app, &bad).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "DOUBLE_SPEND");

    // Tip unchanged; the output is still spendable; replay still clean.
    let (_, tip) = get_json(&app, "/tip").await;
    assert_eq!(tip["height"], 2);

    let (s, _) = get_json(
        &app,
        &format!("/utxos/{tx2id}/1?height=2"),
    )
    .await;
    assert_eq!(s, 200, "spent input must remain unspent after rejected block");

    let (s, r) = get_json(&app, "/replay/verify").await;
    assert_eq!(s, 200);
    assert_eq!(r["all_match"], true);
}

#[tokio::test]
async fn rejects_negative_amount_unknown_input_and_overspend() {
    let app = test_app();
    let (_h1, h2, _cb1, tx2id) = seed_two_blocks(&app).await;

    // Negative amount (wire-level JSON, rejected in static check).
    let neg = json!({
        "version": 1, "height": 3, "prevHash": h2, "timestamp": 13,
        "txs": [
            {"version": 1, "vin": [], "vout": [{"value": -5, "address": MINER}]},
            {"version": 1,
             "vin": [{"txid": tx2id, "vout": 1, "scriptSig": hex::encode("s")}],
             "vout": [{"value": 4900, "address": BOB}]}
        ]
    });
    let (s, r) = submit_value(&app, neg).await;
    assert_eq!(s, 400, "{r}");
    assert_eq!(r["error"], "NEGATIVE_AMOUNT");

    // Unknown / never-existing outpoint.
    let ghost = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            spending_tx(vec![(Hash32::ZERO, 99, "g")], vec![(1, BOB)]),
        ],
        13,
    );
    let (s, r) = submit(&app, &ghost).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "UNKNOWN_INPUT");

    // Outputs exceed inputs.
    let over = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            spending_tx(
                vec![(Hash32::from_hex(&tx2id).unwrap(), 1, "o")],
                vec![(4901, BOB)],
            ),
        ],
        13,
    );
    let (s, r) = submit(&app, &over).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "INSUFFICIENT_INPUTS");

    // Wrong coinbase amount (ignores fees).
    let badreward = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY + 999)],
        13,
    );
    let (s, r) = submit(&app, &badreward).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "BAD_COINBASE_AMOUNT");
}

#[tokio::test]
async fn rejects_linkage_errors() {
    let app = test_app();
    let (h1, h2, _cb1, _tx2id) = seed_two_blocks(&app).await;

    // Height mismatch.
    let wrong_h = make_block(4, Hash32::from_hex(&h2).unwrap(), vec![coinbase_tx(MINER, COINBASE_SUBSIDY)], 13);
    let (s, r) = submit(&app, &wrong_h).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "HEIGHT_MISMATCH");

    // Previous hash mismatch.
    let wrong_prev = make_block(3, Hash32::ZERO, vec![coinbase_tx(MINER, COINBASE_SUBSIDY)], 13);
    let (s, r) = submit(&app, &wrong_prev).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "PREV_HASH_MISMATCH");

    // Gap on disconnect target above tip.
    let (s, r) = post_json(&app, "/blocks/disconnect", json!({"targetHeight": 9})).await;
    assert_eq!(s, 400);
    assert!(r["message"].as_str().unwrap().contains("above tip"));

    // Cannot disconnect genesis.
    let (s, _r) = post_json(&app, "/blocks/disconnect", json!({"targetHeight": -1})).await;
    assert_eq!(s, 400);

    let _ = h1;
}

#[tokio::test]
async fn consecutive_disconnect_then_alternative_branch() {
    let app = test_app();
    let (h1, h2, cb1, tx2id) = seed_two_blocks(&app).await;

    // Canonical block 3.
    let b3 = make_block(
        3,
        Hash32::from_hex(&h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            spending_tx(
                vec![(Hash32::from_hex(&tx2id).unwrap(), 1, "x")],
                vec![(4900, BOB)],
            ),
        ],
        13,
    );
    let (s, r) = submit(&app, &b3).await;
    assert_eq!(s, 201, "{r}");
    let h3 = r["hash"].as_str().unwrap().to_string();

    // Roll back two blocks in one atomic call: 3 then 2.
    let (s, r) = post_json(&app, "/blocks/disconnect", json!({"targetHeight": 1})).await;
    assert_eq!(s, 200, "{r}");
    assert_eq!(r["disconnected"], 2);
    assert_eq!(r["tip_height"], 1);
    assert_eq!(r["tip_hash"], h1);

    // Disconnected output from block2 is gone even when asking height 3
    // (no such height -> 404), and tip state root equals block1 root.
    let (s, _) = get_json(&app, "/state-root?height=3").await;
    assert_eq!(s, 404);
    let (s, r1root) = get_json(&app, "/state-root?height=1").await;
    assert_eq!(s, 200);
    let (_, tip) = get_json(&app, "/tip").await;
    assert_eq!(tip["state_root"], r1root["state_root"]);

    // Illegal alternative block: tries to spend output from the
    // disconnected branch (carol's 100 from block2's tx2 output 0).
    let stale = make_block(
        2,
        Hash32::from_hex(&h1).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            spending_tx(
                vec![(Hash32::from_hex(&tx2id).unwrap(), 0, "stale")],
                vec![(100, BOB)],
            ),
        ],
        22,
    );
    let (s, r) = submit(&app, &stale).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "UNKNOWN_INPUT", "{r}");
    let (_, tip) = get_json(&app, "/tip").await;
    assert_eq!(tip["height"], 1, "failed alternative must not move tip");

    // Legal alternative block 2: spends the block1 coinbase again.
    let alt_tx = spending_tx(
        vec![(Hash32::from_hex(&cb1).unwrap(), 0, "alt")],
        vec![(2000, CAROL), (2000, ALICE)],
    ); // fee 1000
    let alt2 = make_block(
        2,
        Hash32::from_hex(&h1).unwrap(),
        vec![coinbase_tx(MINER, COINBASE_SUBSIDY + 1000), alt_tx.clone()],
        22,
    );
    let (s, r) = submit(&app, &alt2).await;
    assert_eq!(s, 201, "{r}");
    let alt_h2 = r["hash"].as_str().unwrap().to_string();
    assert_ne!(alt_h2, h2, "alternative block hash must differ");

    // Reorg changes the state root, and replay agrees on the new chain.
    let (s, replay) = get_json(&app, "/replay/verify").await;
    assert_eq!(s, 200);
    assert_eq!(replay["all_match"], true, "{replay}");

    // Extend the alternative branch and replay again.
    let alt_txid = alt_tx.txid.unwrap();
    let alt3 = make_block(
        3,
        Hash32::from_hex(&alt_h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY + 5),
            spending_tx(vec![(alt_txid, 0, "c2")], vec![(1995, BOB)]),
        ],
        23,
    );
    let (s, r) = submit(&app, &alt3).await;
    assert_eq!(s, 201, "{r}");
    assert_eq!(r["fees"], 5);
    let alt_h3 = r["hash"].as_str().unwrap().to_string();
    assert_ne!(alt_h3, h3);

    let (s, replay) = get_json(&app, "/replay/verify").await;
    assert_eq!(s, 200);
    assert_eq!(replay["all_match"], true, "{replay}");
}

#[tokio::test]
async fn historical_queries_require_explicit_height() {
    let app = test_app();
    let (_h1, _h2, _cb1, tx2id) = seed_two_blocks(&app).await;

    // Missing height is a 400, never an implicit "current".
    let (s, r) = get_json(&app, "/state-root").await;
    assert_eq!(s, 400);
    assert!(r["message"].as_str().unwrap().contains("height"));

    let (s, _r) = get_json(&app, &format!("/utxos/{tx2id}/1")).await;
    assert_eq!(s, 400);

    // The block-2 change output exists at height 2...
    let (s, _) = get_json(&app, &format!("/utxos/{tx2id}/1?height=2")).await;
    assert_eq!(s, 200);

    // ... spend it at height 3 ...
    let b3 = make_block(
        3,
        Hash32::from_hex(&_h2).unwrap(),
        vec![
            coinbase_tx(MINER, COINBASE_SUBSIDY),
            spending_tx(
                vec![(Hash32::from_hex(&tx2id).unwrap(), 1, "z")],
                vec![(4900, BOB)],
            ),
        ],
        13,
    );
    let (s, _) = submit(&app, &b3).await;
    assert_eq!(s, 201);

    // ... it is still reported unspent at historical height 2, and spent now.
    let (s, _) = get_json(&app, &format!("/utxos/{tx2id}/1?height=2")).await;
    assert_eq!(s, 200, "history must not be served from current UTXO");
    let (s, _) = get_json(&app, &format!("/utxos/{tx2id}/1?height=3")).await;
    assert_eq!(s, 404);

    // Roots at different heights differ; height 2 stays stable.
    let (_, r2a) = get_json(&app, "/state-root?height=2").await;
    let (_, r3) = get_json(&app, "/state-root?height=3").await;
    assert_ne!(r2a["state_root"], r3["state_root"]);
    let (_, r2b) = get_json(&app, "/state-root?height=2").await;
    assert_eq!(r2a["state_root"], r2b["state_root"]);

    // Unknown height -> 404.
    let (s, _) = get_json(&app, "/state-root?height=99").await;
    assert_eq!(s, 404);
}

#[tokio::test]
async fn ordered_in_block_spend_ok_and_forward_reference_rejected() {
    // Coinbase pays subsidy+1 (the 1 fee is created by tx_b below, so the
    // coinbase txid must be computed over that final value up front).
    let app = test_app();
    let cb = coinbase_tx(MINER, COINBASE_SUBSIDY + 1);
    let cb_id = cb.txid.unwrap();
    let tx_a = spending_tx(vec![(cb_id, 0, "a")], vec![(5001, ALICE)]); // fee 0
    let a_id = tx_a.txid.unwrap();
    let tx_b = spending_tx(vec![(a_id, 0, "b")], vec![(5000, BOB)]); // fee 1
    let b1 = make_block(
        1,
        Hash32::ZERO,
        vec![cb, tx_a, tx_b],
        1,
    );
    let (s, r) = submit(&app, &b1).await;
    assert_eq!(s, 201, "ordered in-block spend chain: {r}");

    // Undo must reverse the whole chain (including in-block created+spent
    // outputs) back to the empty genesis set.
    let (s, disc) = post_json(&app, "/blocks/disconnect", json!({"targetHeight": 0})).await;
    assert_eq!(s, 200, "{disc}");
    assert_eq!(disc["disconnected"], 1);
    let (_, genesis_tip) = get_json(&app, "/tip").await;
    assert_eq!(genesis_tip["height"], 0);
    let (_, root_now) = get_json(&app, "/state-root?height=0").await;
    assert_eq!(genesis_tip["state_root"], root_now["state_root"]);
    let (s, replay) = get_json(&app, "/replay/verify").await;
    assert_eq!(s, 200);
    assert_eq!(replay["all_match"], true);
    let _ = r;

    // Forward reference: txC spends an output of txD which appears later.
    let app2 = test_app();
    let cb2 = coinbase_tx(MINER, COINBASE_SUBSIDY + 1);
    let cb2_id = cb2.txid.unwrap();
    let tx_d = spending_tx(vec![(cb2_id, 0, "d")], vec![(5001, ALICE)]);
    let d_id = tx_d.txid.unwrap();
    let tx_c = spending_tx(vec![(d_id, 0, "c")], vec![(5000, BOB)]);
    let fwd = make_block(
        1,
        Hash32::ZERO,
        vec![cb2, tx_c, tx_d],
        1,
    );
    let (s, r) = submit(&app2, &fwd).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "INPUT_IN_FUTURE", "{r}");
}

#[tokio::test]
async fn restart_persists_state() {
    // Use a real file-backed DB, submit blocks, drop it, reopen, verify.
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("restart.db");
    let path_str = path.to_str().unwrap().to_string();

    let root1;
    let h2;
    {
        let db = Arc::new(Db::open(&path_str).unwrap());
        let app = api::router(db.clone());
        let (h1, got_h2, _cb1, tx2id) = seed_two_blocks_router(&app).await;
        h2 = got_h2;

        let b3 = make_block(
            3,
            Hash32::from_hex(&h2).unwrap(),
            vec![
                coinbase_tx(MINER, COINBASE_SUBSIDY + 2),
                spending_tx(
                    vec![(Hash32::from_hex(&tx2id).unwrap(), 1, "r")],
                    vec![(4898, BOB)],
                ),
            ],
            31,
        );
        let (s, r) = submit(&app, &b3).await;
        assert_eq!(s, 201, "{r}");
        root1 = r["state_root"].as_str().unwrap().to_string();
        let _ = h1;
    }

    // Reopen: state must be exactly where we left off.
    let db = Arc::new(Db::open(&path_str).unwrap());
    let app = api::router(db);
    let (s, tip) = get_json(&app, "/tip").await;
    assert_eq!(s, 200);
    assert_eq!(tip["height"], 3);
    assert_eq!(tip["state_root"], root1);

    let (s, r) = get_json(&app, "/replay/verify").await;
    assert_eq!(s, 200);
    assert_eq!(r["all_match"], true, "{r}");

    // A duplicate submission after restart is rejected by height linkage.
    let dup = json!({
        "version": 1, "height": 3, "prevHash": h2, "timestamp": 31,
        "txs": [{"version": 1, "vin": [], "vout": [{"value": COINBASE_SUBSIDY, "address": MINER}]}]
    });
    let (s, r) = submit_value(&app, dup).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "HEIGHT_MISMATCH");

    // Disconnect works across restart too.
    let (s, r) = post_json(&app, "/blocks/disconnect", json!({"targetHeight": 2})).await;
    assert_eq!(s, 200, "{r}");
    assert_eq!(r["disconnected"], 1);
}

async fn seed_two_blocks_router(app: &Router) -> (String, String, String, String) {
    seed_two_blocks(app).await
}

#[tokio::test]
async fn txid_must_match_real_double_sha256() {
    let app = test_app();
    let mut b1 = make_block(1, Hash32::ZERO, vec![coinbase_tx(ALICE, COINBASE_SUBSIDY)], 1);
    let real_id = b1.txs[0].txid.unwrap();

    // Tamper the supplied txid; computed id differs -> reject.
    b1.txs[0].txid = Some(Hash32::ZERO);
    let (s, r) = submit(&app, &b1).await;
    assert_eq!(s, 400);
    assert_eq!(r["error"], "TXID_MISMATCH");
    assert!(r["message"].to_string().contains(&real_id.to_hex()));
}
