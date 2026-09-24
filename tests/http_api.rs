//! End-to-end HTTP tests: spin up the real Axum server on an ephemeral port
//! and drive the full upload → signature → delta → patch flow over HTTP.

use std::sync::Arc;

use artifact_delta::api::app;
use artifact_delta::checksum;
use artifact_delta::store::Store;
use base64::Engine;
use reqwest::multipart;
use serde_json::Value;
use tokio::net::TcpListener;

struct Server {
    base: String,
}

async fn spawn_server() -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let store = Arc::new(Store::new(256 * 1024 * 1024));
    let server = axum::serve(listener, app(store));
    tokio::spawn(async move {
        let _ = server.await;
    });
    Server {
        base: format!("http://{addr}"),
    }
}

fn blake3_hex(data: &[u8]) -> String {
    artifact_delta::hex::encode(&checksum::strong(data))
}

/// Full flow: upload basis+target, sign, delta, patch, compare byte-for-byte.
/// Returns the parsed delta response JSON.
async fn e2e_flow(
    srv: &Server,
    basis_id: &str,
    target_id: &str,
    basis: &[u8],
    target: &[u8],
    block_size: usize,
) -> Value {
    let client = reqwest::Client::new();

    // upload both artifacts
    let r = client
        .put(format!("{}/v1/artifacts/{}", srv.base, basis_id))
        .body(basis.to_vec())
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200, "upload basis: {:?}", r.text().await);
    let r = client
        .put(format!("{}/v1/artifacts/{}", srv.base, target_id))
        .body(target.to_vec())
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200, "upload target: {:?}", r.text().await);

    // signature
    let form = multipart::Form::new()
        .text("basis_id", basis_id.to_string())
        .text("block_size", block_size.to_string());
    let r = client
        .post(format!("{}/v1/signatures", srv.base))
        .multipart(form)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200, "signature: {:?}", r.text().await);
    let sig_resp: Value = r.json().await.unwrap();
    let signature_json = serde_json::to_vec(&sig_resp["signature"]).unwrap();

    // delta (signature + raw target bytes)
    let form = multipart::Form::new()
        .part(
            "signature",
            multipart::Part::bytes(signature_json)
                .file_name("sig.json")
                .mime_str("application/json")
                .unwrap(),
        )
        .part(
            "data",
            multipart::Part::bytes(target.to_vec())
                .file_name("target.bin")
                .mime_str("application/octet-stream")
                .unwrap(),
        );
    let r = client
        .post(format!("{}/v1/deltas", srv.base))
        .multipart(form)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200, "delta: {:?}", r.text().await);
    let delta_resp: Value = r.json().await.unwrap();

    // patch (server holds the basis)
    let delta_json = serde_json::to_vec(&delta_resp["delta"]).unwrap();
    let form = multipart::Form::new()
        .text("basis_id", basis_id.to_string())
        .part(
            "delta",
            multipart::Part::bytes(delta_json)
                .file_name("delta.json")
                .mime_str("application/json")
                .unwrap(),
        )
        .text("expected_blake3_hex", blake3_hex(target));
    let r = client
        .post(format!("{}/v1/patch", srv.base))
        .multipart(form)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200, "patch: {:?}", r.text().await);
    let patch_resp: Value = r.json().await.unwrap();
    let out = base64::engine::general_purpose::STANDARD
        .decode(patch_resp["output_b64"].as_str().unwrap())
        .unwrap();
    assert_eq!(out, target, "byte-for-byte equality over HTTP");
    assert_eq!(patch_resp["verified"], true);

    delta_resp
}

#[tokio::test]
async fn healthz() {
    let srv = spawn_server().await;
    let r = reqwest::get(format!("{}/healthz", srv.base)).await.unwrap();
    assert_eq!(r.status(), 200);
}

#[tokio::test]
async fn digests_endpoint_matches_local_blake3() {
    let srv = spawn_server().await;
    let payload = b"digest me please".repeat(100);
    let r = reqwest::Client::new()
        .post(format!("{}/v1/digests", srv.base))
        .body(payload.clone())
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 200);
    let v: Value = r.json().await.unwrap();
    assert_eq!(v["blake3_hex"], blake3_hex(&payload));
    assert_eq!(v["bytes"], payload.len() as u64);
}

#[tokio::test]
async fn http_head_insertion_roundtrip_with_savings() {
    let srv = spawn_server().await;
    let mut rng = Rng(0x5151_AAAA);
    let mut basis = vec![0u8; 20_000];
    rng.fill(&mut basis);
    let mut target = Vec::new();
    target.extend_from_slice(b"a 37-byte inserted header prefix!!");
    target.extend_from_slice(&basis);

    let resp = e2e_flow(&srv, "b-head", "t-head", &basis, &target, 512).await;
    let stats = &resp["stats"];
    assert_eq!(stats["target_bytes"], target.len() as u64);
    assert!(
        stats["wire_saved_bytes"].as_u64().unwrap() > 15_000,
        "expected substantial savings, got: {stats}"
    );
}

#[tokio::test]
async fn http_local_deletion_roundtrip() {
    let srv = spawn_server().await;
    let mut rng = Rng(0x7777_1234);
    let mut basis = vec![0u8; 30_000];
    rng.fill(&mut basis);
    let mut target = basis.clone();
    target.drain(5000..5000 + 3000);

    let resp = e2e_flow(&srv, "b-del", "t-del", &basis, &target, 1024).await;
    assert!(
        resp["stats"]["wire_saved_bytes"].as_u64().unwrap() > 20_000,
        "{}",
        resp["stats"]
    );
}

#[tokio::test]
async fn http_weak_collision_does_not_fake_match() {
    // Build the same collision used in the unit test and drive it over HTTP.
    let srv = spawn_server().await;
    let n = 64usize;
    let (p, q) = find_weak_collision(n, 5_000_000);
    assert_eq!(checksum::weak(&p), checksum::weak(&q));
    assert_ne!(checksum::strong(&p), checksum::strong(&q));

    let mut basis = p.clone();
    basis.extend_from_slice(&vec![3u8; 6 * n]);
    let mut target = q.clone();
    target.extend_from_slice(&vec![3u8; 6 * n]);

    let resp = e2e_flow(&srv, "b-col", "t-col", &basis, &target, n).await;
    // Q's n bytes must travel literally.
    assert!(
        resp["stats"]["literal_bytes"].as_u64().unwrap() >= n as u64,
        "colliding block Q must be sent literally: {}",
        resp["stats"]
    );
    // First op must be literal, never a copy.
    assert_eq!(resp["delta"]["ops"][0]["op"], "literal");
}

#[tokio::test]
async fn http_wrong_expected_digest_is_rejected() {
    let srv = spawn_server().await;
    let basis = b"some basis content here for digest test.".repeat(10);
    let target = b"totally different target content!!!!".repeat(10);
    let client = reqwest::Client::new();

    client
        .put(format!("{}/v1/artifacts/bad-digest", srv.base))
        .body(basis.clone())
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();

    let form = multipart::Form::new()
        .text("basis_id", "bad-digest")
        .text("block_size", "64");
    let sig: Value = client
        .post(format!("{}/v1/signatures", srv.base))
        .multipart(form)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let form = multipart::Form::new()
        .text(
            "signature",
            serde_json::to_string(&sig["signature"]).unwrap(),
        )
        .part("data", multipart::Part::bytes(target.clone()));
    let delta_resp: Value = client
        .post(format!("{}/v1/deltas", srv.base))
        .multipart(form)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    let form = multipart::Form::new()
        .text("basis_id", "bad-digest")
        .text("delta", serde_json::to_string(&delta_resp["delta"]).unwrap())
        .text("expected_blake3_hex", "00".repeat(32));
    let r = client
        .post(format!("{}/v1/patch", srv.base))
        .multipart(form)
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 409, "mismatched digest must be a 409");
}

#[tokio::test]
async fn http_unknown_artifact_404_and_bad_id_400() {
    let srv = spawn_server().await;
    let client = reqwest::Client::new();
    let r = client
        .get(format!("{}/v1/artifacts/does-not-exist", srv.base))
        .send()
        .await
        .unwrap();
    assert_eq!(r.status(), 404);

    let r = client
        .put(format!("{}/v1/artifacts/bad%2Fid", srv.base))
        .body(vec![0u8])
        .send()
        .await
        .unwrap();
    // Axum rejects '/' inside the path segment before the handler (400).
    assert_eq!(r.status(), 400);
}

// ----- helpers mirrored from the unit test (small, keep duplication local) ---

struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn fill(&mut self, buf: &mut [u8]) {
        let mut i = 0;
        while i < buf.len() {
            let v = self.next_u64().to_le_bytes();
            let take = (buf.len() - i).min(8);
            buf[i..i + take].copy_from_slice(&v[..take]);
            i += take;
        }
    }
}

fn find_weak_collision(n: usize, cap_blocks: u32) -> (Vec<u8>, Vec<u8>) {
    use std::collections::HashMap;
    let mut rng = Rng(0xC011_1510_DEAD_BEEF);
    let mut buckets: HashMap<u32, Vec<Vec<u8>>> = HashMap::new();
    for _ in 0..cap_blocks {
        let mut b = vec![0u8; n];
        rng.fill(&mut b);
        let w = checksum::weak(&b);
        let bucket = buckets.entry(w).or_default();
        if let Some(existing) = bucket.iter().find(|e| **e != b) {
            assert_ne!(checksum::strong(existing), checksum::strong(&b));
            return (existing.clone(), b);
        }
        bucket.push(b);
    }
    panic!("no weak collision found");
}
