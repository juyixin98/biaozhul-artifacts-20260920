//! End-to-end HTTP tests: real TCP listener on an ephemeral port, real JSON,
//! real signatures, status-code assertions.

mod common;

use std::sync::Arc;

use common::*;
use tempfile::TempDir;

use utxo_rollback::api;
use utxo_rollback::crypto;
use utxo_rollback::Storage;

struct Server {
    base: String,
    _dir: TempDir,
}

fn spawn_server() -> Server {
    let dir = TempDir::new().unwrap();
    let db = dir.path().join("http.db").to_string_lossy().into_owned();
    let storage = Arc::new(Storage::open(&db).unwrap());
    let app = api::router(storage);

    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .unwrap();
            tx.send(listener.local_addr().unwrap().port()).unwrap();
            axum::serve(listener, app).await.unwrap();
        });
    });
    let port = rx.recv().unwrap();

    Server {
        base: format!("http://127.0.0.1:{port}"),
        _dir: dir,
    }
}

#[test]
fn full_http_workflow() {
    let server = spawn_server();
    let c = reqwest::blocking::Client::new();
    let base = &server.base;

    // health
    let h: serde_json::Value = c.get(format!("{base}/health")).send().unwrap().json().unwrap();
    assert_eq!(h["status"], "ok");

    // empty chain tip
    let tip: serde_json::Value = c.get(format!("{base}/chain/tip")).send().unwrap().json().unwrap();
    assert_eq!(tip["empty"], true);

    let (miner, alice, _bob) = wallets();
    let b0 = block0(&miner);
    let h0 = block_hash(&b0);

    // malformed JSON -> 400
    let r = c
        .post(format!("{base}/blocks"))
        .header("content-type", "application/json")
        .body("{not json")
        .send()
        .unwrap();
    assert_eq!(r.status(), 400);

    // negative amount -> 400 (u64 parse error)
    let negative = serde_json::json!({
        "height": 0,
        "prev_hash": "00".repeat(31) + "00",
        "timestamp": 0,
        "merkle_root": "00".repeat(31) + "00",
        "txs": [{
            "inputs": [{"prev": null, "pubkey": "", "signature": ""}],
            "outputs": [{"value": -1, "address": "00".repeat(31) + "00"}]
        }]
    });
    let r = c
        .post(format!("{base}/blocks"))
        .json(&negative)
        .send()
        .unwrap();
    assert_eq!(r.status(), 400);

    // genesis -> 201
    let r = c.post(format!("{base}/blocks")).json(&b0).send().unwrap();
    assert_eq!(r.status(), 201, "{}", r.text().unwrap());

    // double-spend block -> 409, chain unchanged
    let bad = double_spend_block1(h0, &miner.address_hex(), crypto::txid(&block0(&miner).txs[0]).unwrap(), &miner);
    let r = c.post(format!("{base}/blocks")).json(&bad).send().unwrap();
    assert_eq!(r.status(), 409);
    let body: serde_json::Value = r.json().unwrap();
    assert!(body["error"].as_str().unwrap().contains("double spend"));

    // legal block1
    let (b1, _) = block1(&miner, h0, &alice.address_hex(), 30, 5);
    let h1 = block_hash(&b1);
    let r = c.post(format!("{base}/blocks")).json(&b1).send().unwrap();
    assert_eq!(r.status(), 201, "{}", r.text().unwrap());
    let connected: serde_json::Value = c
        .get(format!("{base}/blocks/1"))
        .send()
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(connected["hash"], serde_json::json!(hex::encode(h1)));

    // block lookup by hash and by tip
    let by_hash: serde_json::Value = c
        .get(format!("{base}/blocks/0?by_hash={}", hex::encode(h0)))
        .send()
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(by_hash["height"], 0);
    let tip_block: serde_json::Value =
        c.get(format!("{base}/blocks/tip")).send().unwrap().json().unwrap();
    assert_eq!(tip_block["height"], 1);

    // current vs historical
    let cur: serde_json::Value = c.get(format!("{base}/utxos")).send().unwrap().json().unwrap();
    assert_eq!(cur["view"], "current");
    assert_eq!(cur["count"], 3);
    let at0: serde_json::Value =
        c.get(format!("{base}/utxos/at/0")).send().unwrap().json().unwrap();
    assert_eq!(at0["view"], "historical");
    assert_eq!(at0["height"], 0);
    assert_eq!(at0["count"], 1);
    assert_ne!(cur["state_root"], at0["state_root"]);

    // unknown height -> 404
    let r = c.get(format!("{base}/utxos/at/99")).send().unwrap();
    assert_eq!(r.status(), 404);

    // disconnect -> back to height 0
    let r = c.post(format!("{base}/blocks/disconnect")).send().unwrap();
    assert_eq!(r.status(), 200);
    let d: serde_json::Value = r.json().unwrap();
    assert_eq!(d["new_tip_height"], 0);

    // replay cross-check
    let rep: serde_json::Value = c.get(format!("{base}/debug/replay")).send().unwrap().json().unwrap();
    assert_eq!(rep["roots_match"], true);
    assert_eq!(rep["blocks_replayed"], 1);
}
