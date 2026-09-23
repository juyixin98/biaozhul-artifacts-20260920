//! End-to-end HTTP tests driving the real Axum server over TCP with a tiny
//! std-only HTTP client (see tests/common/mod.rs). Covers the full acceptance
//! scenario through the public API.

mod common;

use common::{delete, fresh_server, get, post, put};
use serde_json::{json, Value};
use std::time::Duration;

/// Walk a manifest closure from a root via the HTTP API, returning all
/// reachable hashes.
fn reachable(base: &str, root: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![root.to_string()];
    while let Some(h) = stack.pop() {
        if out.contains(&h) {
            continue;
        }
        out.push(h.clone());
        let meta = get(&format!("{base}/objects/{h}/meta")).json();
        if meta["kind"] == "manifest" {
            let body = get(&format!("{base}/objects/{h}")).json();
            for r in body["refs"].as_array().unwrap() {
                stack.push(r.as_str().unwrap().to_string());
            }
        }
    }
    out
}

fn deleted_hashes(gc: &Value) -> Vec<String> {
    gc["deleted"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect()
}

#[test]
fn full_acceptance_flow_over_http() {
    let (_dir, base) = fresh_server(1);

    // --- Shared data blocks. ---
    let b1 = post(&format!("{base}/blocks"), None, b"shared-one").json();
    let b2 = post(&format!("{base}/blocks"), None, b"shared-two").json();
    let shared = post(
        &format!("{base}/manifests"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "refs": [b1["hash"], b2["hash"]] })).unwrap(),
    )
    .json();
    // Each root manifest also owns a distinct block, so the two manifests have
    // distinct content hashes (content addressing dedupes identical bodies).
    let a_only = post(&format!("{base}/blocks"), None, b"a-only").json();
    let b_only = post(&format!("{base}/blocks"), None, b"b-only").json();
    let m_a = post(
        &format!("{base}/manifests"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "refs": [shared["hash"], a_only["hash"]] })).unwrap(),
    )
    .json();
    let m_b = post(
        &format!("{base}/manifests"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "refs": [shared["hash"], b_only["hash"]] })).unwrap(),
    )
    .json();

    // --- Two published roots + an unreferenced orphan. ---
    let r = put(
        &format!("{base}/roots/A"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "hash": m_a["hash"] })).unwrap(),
    );
    assert_eq!(r.status, 200);
    put(
        &format!("{base}/roots/B"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "hash": m_b["hash"] })).unwrap(),
    );
    let orphan = post(&format!("{base}/blocks"), None, b"orphan").json();

    // --- Delete root A, GC: shared subgraph survives via B. ---
    assert_eq!(delete(&format!("{base}/roots/A")).status, 200);
    let gc = post(&format!("{base}/gc"), None, &[]).json();
    let deleted = deleted_hashes(&gc);
    assert!(
        deleted.iter().any(|d| d == m_a["hash"].as_str().unwrap()),
        "A manifest collected"
    );
    assert!(
        deleted.iter().any(|d| d == orphan["hash"].as_str().unwrap()),
        "orphan collected"
    );
    assert!(
        deleted.iter().any(|d| d == a_only["hash"].as_str().unwrap()),
        "A-only block collected once root A is gone"
    );
    for alive in [&b1, &b2, &shared, &m_b, &b_only] {
        let h = alive["hash"].as_str().unwrap();
        assert!(!deleted.contains(&h.to_string()), "{h} wrongly deleted");
        assert_eq!(get(&format!("{base}/objects/{h}")).status, 200);
    }

    // Closure reachable from B: mB, shared, b1, b2, b_only.
    let live = reachable(&base, m_b["hash"].as_str().unwrap());
    assert_eq!(live.len(), 5);

    // --- Unfinished upload: retained without any root. ---
    let begin = post(&format!("{base}/uploads"), None, &[]).json();
    let up = begin["upload_id"].as_str().unwrap();
    let up_block = post(
        &format!("{base}/uploads/{up}/blocks"),
        None,
        b"unfinished",
    )
    .json();
    let ub = up_block["hash"].as_str().unwrap();

    let gc = post(&format!("{base}/gc"), None, &[]).json();
    assert_eq!(gc["open_uploads"], 1);
    assert_eq!(get(&format!("{base}/objects/{ub}")).status, 200);

    // Complete, wait out retention (whole-second timestamps: sleep past the 2s
    // boundary to avoid a boundary flake), then collect.
    assert_eq!(
        post(&format!("{base}/uploads/{up}/complete"), None, &[]).status,
        200
    );
    std::thread::sleep(Duration::from_millis(2600));
    let gc = post(&format!("{base}/gc"), None, &[]).json();
    assert!(
        deleted_hashes(&gc).contains(&ub.to_string()),
        "expired upload block should be collected"
    );
    assert_eq!(get(&format!("{base}/objects/{ub}")).status, 404);
}

#[test]
fn rejects_invalid_and_dangling_inputs() {
    let (_dir, base) = fresh_server(3600);

    // Invalid hash inside a manifest -> 400.
    let r = post(
        &format!("{base}/manifests"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "refs": ["not-a-hash"] })).unwrap(),
    );
    assert_eq!(r.status, 400);

    // Publish a root at a missing object -> 404.
    let missing = "f".repeat(64);
    let r = put(
        &format!("{base}/roots/nope"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "hash": missing })).unwrap(),
    );
    assert_eq!(r.status, 404);

    // Fetch a missing object -> 404.
    assert_eq!(get(&format!("{base}/objects/{missing}")).status, 404);
}

#[test]
fn binary_roundtrip_preserves_bytes() {
    let (_dir, base) = fresh_server(3600);
    let payload: Vec<u8> = (0u8..=255).collect();
    let out = post(&format!("{base}/blocks"), None, &payload).json();
    let h = out["hash"].as_str().unwrap();
    let got = get(&format!("{base}/objects/{h}"));
    assert_eq!(got.body, payload);
}

#[test]
fn object_metadata_reports_kind_and_size() {
    let (_dir, base) = fresh_server(3600);
    let b = post(&format!("{base}/blocks"), None, b"hello block").json();
    let h = b["hash"].as_str().unwrap();
    let meta = get(&format!("{base}/objects/{h}/meta")).json();
    assert_eq!(meta["kind"], "block");
    assert_eq!(meta["size"], 11);
}

#[test]
fn roots_list_and_persist_semantics() {
    let (_dir, base) = fresh_server(3600);
    let b = post(&format!("{base}/blocks"), None, b"r").json();
    put(
        &format!("{base}/roots/x"),
        Some("application/json"),
        &serde_json::to_vec(&json!({ "hash": b["hash"] })).unwrap(),
    );
    let roots = get(&format!("{base}/roots")).json();
    assert_eq!(roots["roots"]["x"], b["hash"].as_str().unwrap());
}
