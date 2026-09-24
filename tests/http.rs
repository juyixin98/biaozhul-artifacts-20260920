//! End-to-end HTTP tests against the Axum router, exercising the real wire
//! format through the same code path a TCP client would use.

use artifact_delta::api::app;
use artifact_delta::patch::Patch;
use artifact_delta::signature::{strong_hash, Signature};
use artifact_delta::store::ArtifactStore;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use std::sync::Arc;

fn rng_bytes(seed: u64, n: usize) -> Vec<u8> {
    let mut x = seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
    let mut out = Vec::with_capacity(n);
    while out.len() < n {
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        out.extend_from_slice(&x.to_le_bytes());
    }
    out.truncate(n);
    out
}

struct Harness {
    app: axum::Router,
}

impl Harness {
    fn new() -> Self {
        Harness { app: app(Arc::new(ArtifactStore::new())) }
    }

    async fn request(
        &self,
        method: &str,
        uri: &str,
        body: Vec<u8>,
        extra_headers: &[(&str, &str)],
    ) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let mut builder = Request::builder().method(method).uri(uri);
        for (k, v) in extra_headers {
            builder = builder.header(*k, *v);
        }
        let req = builder.body(Body::from(body)).unwrap();
        let resp = self.app.clone().oneshot(req).await.unwrap();
        let status = resp.status();
        let headers = resp.headers().clone();
        let bytes = axum::body::to_bytes(resp.into_body(), 64 * 1024 * 1024)
            .await
            .unwrap()
            .to_vec();
        (status, headers, bytes)
    }
}

#[tokio::test]
async fn http_roundtrip_head_insertion() {
    let h = Harness::new();
    let s = 512u64;
    let basis = rng_bytes(101, (s * 40) as usize); // 20480, block-aligned
    // Prefix exactly one block long => old blocks stay grid-aligned and the
    // ONLY literal bytes are the prefix itself.
    let prefix = rng_bytes(102, s as usize);
    let mut target = prefix.clone();
    target.extend_from_slice(&basis);

    // old version lives in store as "v1"; new version as "release"
    let (st, _, _) = h.request("PUT", "/artifacts/v1", basis.clone(), &[]).await;
    assert_eq!(st, StatusCode::OK);
    let (st, _, _) = h.request("PUT", "/artifacts/release", target.clone(), &[]).await;
    assert_eq!(st, StatusCode::OK);

    // client computes signature of its old copy
    let sig = Signature::build(&basis, s as u32).unwrap();
    let (st, headers, patch_bytes) = h
        .request(
            "POST",
            "/artifacts/release/delta",
            sig.encode(),
            &[("x-basis-blake3", &artifact_delta::store::hex(&strong_hash(&basis)))],
        )
        .await;
    assert_eq!(st, StatusCode::OK);
    let hdr_u64 = |name: &str| headers[name].to_str().unwrap().parse::<u64>().unwrap();
    assert_eq!(hdr_u64("x-target-length"), target.len() as u64);
    assert_eq!(hdr_u64("x-literal-bytes"), s);
    assert_eq!(hdr_u64("x-bytes-from-basis"), basis.len() as u64);
    assert!(hdr_u64("x-patch-bytes") < target.len() as u64 / 2);

    // client applies locally and verifies byte-equality
    let patch = Patch::decode(&patch_bytes).unwrap();
    let rebuilt = artifact_delta::apply_patch(&patch, &basis).unwrap();
    assert_eq!(rebuilt, target);

    // server-side apply against a stored basis + persist
    let (s, apply_headers, applied) = h
        .request(
            "POST",
            "/artifacts/rebuilt/apply?basis=v1&store=true",
            patch_bytes,
            &[],
        )
        .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(applied, target);
    assert_eq!(
        apply_headers["x-blake3"].to_str().unwrap(),
        artifact_delta::store::hex(&strong_hash(&target))
    );
}

#[tokio::test]
async fn http_local_deletion() {
    let h = Harness::new();
    let s = 256u64;
    let basis = rng_bytes(202, (s * 60) as usize); // 15360, grid-aligned
    // Remove two full blocks at a block boundary; the remainder stays
    // grid-aligned, so the response is pure refs (zero literal bytes).
    let mut target = basis[..s as usize * 20].to_vec();
    target.extend_from_slice(&basis[s as usize * 22..]);

    h.request("PUT", "/artifacts/old", basis.clone(), &[]).await;
    h.request("PUT", "/artifacts/new", target.clone(), &[]).await;

    let sig = Signature::build(&basis, s as u32).unwrap();
    let (st, headers, patch_bytes) = h
        .request("POST", "/artifacts/new/delta", sig.encode(), &[])
        .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(headers["x-literal-bytes"].to_str().unwrap(), "0");
    let patch = Patch::decode(&patch_bytes).unwrap();
    assert_eq!(artifact_delta::apply_patch(&patch, &basis).unwrap(), target);
}

#[tokio::test]
async fn http_weak_collision_case() {
    // Same constructed collision as the engine test, through HTTP.
    let h = Harness::new();
    let a: [u8; 5] = [10, 20, 30, 40, 50];
    let b: [u8; 5] = [11, 17, 33, 39, 50];
    assert_eq!(
        artifact_delta::weak::Rollsum::checksum(&a),
        artifact_delta::weak::Rollsum::checksum(&b)
    );
    let mut basis = a.to_vec();
    basis.extend_from_slice(&rng_bytes(303, 5));
    let mut target = b.to_vec();
    target.extend_from_slice(&basis[5..]);

    h.request("PUT", "/artifacts/col-old", basis.clone(), &[]).await;
    h.request("PUT", "/artifacts/col-new", target.clone(), &[]).await;

    let sig = Signature::build(&basis, 5).unwrap();
    let (s, headers, patch_bytes) = h
        .request("POST", "/artifacts/col-new/delta", sig.encode(), &[])
        .await;
    assert_eq!(s, StatusCode::OK);
    assert!(headers["x-strong-rejections"].to_str().unwrap().parse::<u64>().unwrap() >= 1);
    let patch = Patch::decode(&patch_bytes).unwrap();
    assert_eq!(artifact_delta::apply_patch(&patch, &basis).unwrap(), target);
}

#[tokio::test]
async fn http_signature_convenience_endpoint() {
    let h = Harness::new();
    let data = rng_bytes(404, 3_000);
    h.request("PUT", "/artifacts/x", data.clone(), &[]).await;
    let (s, headers, bytes) = h
        .request("GET", "/artifacts/x/signature?block_len=100", vec![], &[])
        .await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(headers["x-block-len"], "100");
    let sig = Signature::decode(&bytes).unwrap();
    assert_eq!(sig.block_len, 100);
    assert_eq!(sig.blocks.len(), 30);
}

#[tokio::test]
async fn http_errors_and_verbs() {
    let h = Harness::new();
    let (s, _, _) = h.request("GET", "/artifacts/missing", vec![], &[]).await;
    assert_eq!(s, StatusCode::NOT_FOUND);

    // Malformed signature against a non-existent artifact: routing matches,
    // the store reports not found first.
    let (s, _, _) = h
        .request("POST", "/artifacts/nope/delta", b"garbage".to_vec(), &[])
        .await;
    assert_eq!(s, StatusCode::NOT_FOUND);

    // Store an artifact, then send a malformed signature -> 400.
    h.request("PUT", "/artifacts/present", rng_bytes(1, 100), &[]).await;
    let (s, _, _) = h
        .request(
            "POST",
            "/artifacts/present/delta",
            b"garbage".to_vec(),
            &[],
        )
        .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // Malformed patch body at /apply without a usable basis -> malformed patch
    // decode fails first.
    let (s, _, _) = h
        .request(
            "POST",
            "/artifacts/present/apply?basis=present",
            b"garbage".to_vec(),
            &[],
        )
        .await;
    assert_eq!(s, StatusCode::BAD_REQUEST);

    // Unknown route (percent-encoded slash produces an unmatched segment).
    let (s, _, _) = h.request("PUT", "/artifacts/bad%2Fname/sub", vec![1], &[]).await;
    assert_eq!(s, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn http_head_reports_meta() {
    let h = Harness::new();
    let data = rng_bytes(505, 777);
    h.request("PUT", "/artifacts/meta", data.clone(), &[]).await;
    let (s, headers, body) = h.request("HEAD", "/artifacts/meta", vec![], &[]).await;
    assert_eq!(s, StatusCode::OK);
    assert_eq!(headers["x-target-length"], "777");
    assert_eq!(
        headers["x-blake3"].to_str().unwrap(),
        artifact_delta::store::hex(&strong_hash(&data))
    );
    assert!(body.is_empty());
}
