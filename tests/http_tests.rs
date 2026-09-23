//! HTTP end-to-end tests: real Axum listener over a real RocksDB directory.
//! Every proof returned over the wire is checked with the independent Rust
//! verifier; a second test binary pipes proofs through the Python verifier
//! (see scripts/cross_check.sh / tests/python_cross_check.rs).

use merkle_proof_service::{api, store::Store, verifier::verify_proof_slice};
use std::sync::Arc;

struct Server {
    addr: String,
    // kept alive for the test's lifetime
    _dir: tempfile::TempDir,
}

async fn spawn_server() -> Server {
    let dir = tempfile::tempdir().expect("tempdir");
    let store = Arc::new(Store::open(dir.path().to_str().unwrap()).expect("open"));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap().to_string();
    let app = api::router(store);
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    Server { addr, _dir: dir }
}

#[derive(serde::Deserialize)]
struct BatchResp {
    version: u64,
    root_hex: String,
    leaf_count: u64,
    #[allow(dead_code)]
    height: u32,
}

async fn post_json(addr: &str, path: &str, body: serde_json::Value) -> (u16, String) {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("http://{addr}{path}"))
        .json(&body)
        .send()
        .await
        .unwrap();
    let status = resp.status().as_u16();
    (status, resp.text().await.unwrap())
}

async fn get(addr: &str, path: &str) -> (u16, String) {
    let resp = reqwest::get(format!("http://{addr}{path}")).await.unwrap();
    (resp.status().as_u16(), resp.text().await.unwrap())
}

fn to_v(s: &str) -> serde_json::Value {
    serde_json::from_str(s).unwrap()
}

#[tokio::test]
async fn full_lifecycle_over_http() {
    let srv = spawn_server().await;
    let addr = &srv.addr;

    // Before any commit: /v1/root → 404 (no half/empty root fabricated).
    let (st, _) = get(addr, "/v1/root").await;
    assert_eq!(st, 404);

    // Batch 1 (UTF-8 convenience fields + one explicit hex value).
    let (st, body) = post_json(
        addr,
        "/v1/batches",
        serde_json::json!({
            "writes": [
                {"key": "alice", "value": "100"},
                {"key": "bob", "value": "200"},
                {"key_hex": "6361726f6c", "value_hex": "03e8"},
                {"key": "bob", "value": "201"}
            ]
        }),
    )
    .await;
    assert_eq!(st, 200, "{body}");
    let r1: BatchResp = serde_json::from_str(&body).unwrap();
    assert_eq!((r1.version, r1.leaf_count), (1, 3));

    // Existence proof for bob (last write wins: 201).
    let (st, body) = post_json(addr, "/v1/proofs", serde_json::json!({"key": "bob"})).await;
    assert_eq!(st, 200, "{body}");
    let root = hex::decode(&r1.root_hex).unwrap().try_into().unwrap();
    let outcome = verify_proof_slice(body.as_bytes(), &root, b"bob").expect("verify");
    match outcome {
        merkle_proof_service::verifier::Verified::Present(v) => assert_eq!(v, b"201"),
        _other => panic!("unexpected verification outcome"),
    }

    // carol's value is the raw 0x03e8 bytes.
    let (_st, body) = post_json(
        addr,
        "/v1/proofs",
        serde_json::json!({"key_hex": "6361726f6c"}),
    )
    .await;
    let outcome = verify_proof_slice(body.as_bytes(), &root, b"carol").expect("verify");
    match outcome {
        merkle_proof_service::verifier::Verified::Present(v) => assert_eq!(v, vec![0x03, 0xe8]),
        _other => panic!("carol must be present"),
    }

    // Absence interior: "boc" between bob and carol.
    let (_st, body) = post_json(addr, "/v1/proofs", serde_json::json!({"key": "boc"})).await;
    assert_eq!(to_v(&body)["kind"], "non_existence");
    verify_proof_slice(body.as_bytes(), &root, b"boc").expect("absence verify");

    // Boundary absence before first.
    let (_st, body) = post_json(addr, "/v1/proofs", serde_json::json!({"key": "aaa"})).await;
    assert!(to_v(&body)["prev"].is_null());
    verify_proof_slice(body.as_bytes(), &root, b"aaa").expect("boundary verify");

    // Batch 2: delete bob, insert empty-value dave.
    let (st, body) = post_json(
        addr,
        "/v1/batches",
        serde_json::json!({
            "writes": [
                {"key": "bob", "delete": true},
                {"key": "dave"}
            ]
        }),
    )
    .await;
    assert_eq!(st, 200, "{body}");
    let r2: BatchResp = serde_json::from_str(&body).unwrap();
    assert_eq!(r2.leaf_count, 3);

    // bob absent at v2, dave present-empty.
    let root2: [u8; 32] = hex::decode(&r2.root_hex).unwrap().try_into().unwrap();
    let (_st, body) = post_json(
        addr,
        "/v1/proofs",
        serde_json::json!({"key": "bob", "version": 2}),
    )
    .await;
    assert_eq!(to_v(&body)["kind"], "non_existence");
    verify_proof_slice(body.as_bytes(), &root2, b"bob").expect("v2 absent");
    let (_st, body) = post_json(addr, "/v1/proofs", serde_json::json!({"key": "dave"})).await;
    let dave = to_v(&body);
    assert_eq!(dave["value_hex"], "");
    match verify_proof_slice(body.as_bytes(), &root2, b"dave").unwrap() {
        merkle_proof_service::verifier::Verified::Present(v) => assert!(v.is_empty()),
        _other => panic!("dave must be present-empty"),
    }

    // Historical v1 root still queryable and proves bob=201.
    let (st, body) = get(addr, "/v1/root/1").await;
    assert_eq!(st, 200);
    assert_eq!(to_v(&body)["root_hex"], r1.root_hex);
    let (st, _body) = get(addr, "/v1/root/99").await;
    assert_eq!(st, 404);

    let (_st, listing) = get(addr, "/v1/roots").await;
    let listing = to_v(&listing);
    assert_eq!(listing["versions"].as_array().unwrap().len(), 2);

    // Bad request: malformed hex → 400, server keeps running.
    let (st, _) = post_json(addr, "/v1/proofs", serde_json::json!({"key_hex": "zz"})).await;
    assert_eq!(st, 400);
    let (st, _) = get(addr, "/healthz").await;
    assert_eq!(st, 200);
}

#[tokio::test]
async fn tampered_proof_over_http_is_rejected_by_verifier() {
    let srv = spawn_server().await;
    let addr = &srv.addr;
    let (_, body) = post_json(
        addr,
        "/v1/batches",
        serde_json::json!({"writes": (0..6).map(|i| serde_json::json!({"key": format!("k{i}"), "value": format!("v{i}")})).collect::<Vec<_>>()}),
    )
    .await;
    let r: BatchResp = serde_json::from_str(&body).unwrap();
    let root: [u8; 32] = hex::decode(&r.root_hex).unwrap().try_into().unwrap();

    let (_, pbody) = post_json(addr, "/v1/proofs", serde_json::json!({"key": "k3"})).await;
    let mut doc = to_v(&pbody);
    // Flip a byte of a sibling hash in transit.
    let path = doc["path"].as_array_mut().unwrap();
    let step = path.iter_mut().find(|s| s["hash"].is_string()).unwrap();
    let mut bad = hex::decode(step["hash"].as_str().unwrap()).unwrap();
    bad[31] ^= 0x80;
    step["hash"] = serde_json::json!(hex::encode(bad));

    assert!(
        verify_proof_slice(serde_json::to_vec(&doc).unwrap().as_slice(), &root, b"k3").is_err()
    );
}
