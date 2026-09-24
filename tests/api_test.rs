//! End-to-end HTTP tests against the real Axum router.

mod common;

use std::net::SocketAddr;

use manifest_selector::api::{app, AppState};
use manifest_selector::loader;
use manifest_selector::store::Store;
use serde_json::{json, Value};
use tower::ServiceExt;

use axum::body::Body;
use axum::http::{Request, StatusCode};

fn fixtures_root() -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("fixtures")
}

fn loaded_state() -> AppState {
    let mut store = Store::new();
    let repos = loader::load_dir(&mut store, &fixtures_root()).unwrap();
    assert!(
        repos.contains(&"demo-multiarch".to_string()),
        "fixtures must be generated first (python3 scripts/gen_fixtures.py); got {repos:?}"
    );
    AppState::new(store)
}

async fn post_json(state: AppState, uri: &str, body: Value) -> (StatusCode, Value) {
    let resp = app(state)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or(json!({})))
}

async fn put_json(state: AppState, uri: &str, body: Value) -> (StatusCode, Value) {
    let resp = app(state)
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(uri)
                .header("content-type", "application/json")
                .body(Body::from(serde_json::to_vec(&body).unwrap()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or(json!({})))
}

async fn get(state: AppState, uri: &str) -> (StatusCode, Value) {
    let resp = app(state)
        .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap_or(json!({})))
}

#[tokio::test]
async fn health_and_repo_listing() {
    let (status, body) = get(loaded_state(), "/healthz").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], json!("ok"));

    let (status, body) = get(loaded_state(), "/admin/repos").await;
    assert_eq!(status, StatusCode::OK);
    let names: Vec<String> = body["repositories"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["name"].as_str().unwrap().to_string())
        .collect();
    assert!(names.contains(&"demo-multiarch".to_string()));
    assert!(names.contains(&"demo-ambiguous".to_string()));
}

#[tokio::test]
async fn http_selects_arm_v7_and_returns_path() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-multiarch:latest",
        json!({ "os": "linux", "architecture": "arm", "variant": "v7" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["platform"]["variant"], json!("v7"));
    assert_eq!(body["matchType"], json!("index"));
    let path = body["path"].as_array().unwrap();
    assert_eq!(path.len(), 2);
    assert_eq!(path[0]["kind"], json!("index"));
    assert_eq!(path[1]["kind"], json!("manifest"));
    // The chosen digest must be fetchable and verify.
    let digest = body["digest"].as_str().unwrap();
    let (s2, blob) = get(
        loaded_state(),
        &format!("/admin/blobs/blob?repository=demo-multiarch&digest={digest}"),
    )
    .await;
    assert_eq!(s2, StatusCode::OK);
    assert_eq!(blob["digest"], json!(digest));
}

#[tokio::test]
async fn http_selects_arm64_v8_and_arm_v5() {
    for variant in ["v5", "v8"] {
        let arch = if variant == "v8" { "arm64" } else { "arm" };
        let (status, body) = post_json(
            loaded_state(),
            "/v1/select?reference=demo-multiarch:latest",
            json!({ "os": "linux", "architecture": arch, "variant": variant }),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{body}");
        assert_eq!(body["platform"]["variant"], json!(variant));
    }
}

#[tokio::test]
async fn http_missing_platform_returns_404_no_match() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-multiarch:latest",
        json!({ "os": "linux", "architecture": "s390x" }),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["code"], json!("no_match"));
    assert!(body["examined"].is_array());
}

#[tokio::test]
async fn http_ambiguous_returns_409_with_both_candidates() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-ambiguous:latest",
        json!({ "os": "linux", "architecture": "arm64", "variant": "v8" }),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], json!("ambiguous"));
    assert_eq!(body["candidates"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn http_missing_blob_returns_422_and_never_downloads() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-missing:latest",
        json!({ "os": "linux", "architecture": "amd64" }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["code"], json!("missing_blob"));
    assert_eq!(
        body["digest"],
        json!("sha256:".to_string() + &"ab".repeat(32))
    );
}

#[tokio::test]
async fn http_nested_index_selection_has_three_node_path() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-nested:latest",
        json!({ "os": "linux", "architecture": "arm", "variant": "v7" }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let kinds: Vec<String> = body["path"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| n["kind"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(kinds, vec!["index", "index", "manifest"]);
}

#[tokio::test]
async fn http_bad_reference_returns_400() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-multiarch:latest@sha256:zz",
        json!({ "os": "linux", "architecture": "amd64" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], json!("bad_request"));
}

#[tokio::test]
async fn http_missing_os_field_returns_400() {
    let (status, _) = post_json(
        loaded_state(),
        "/v1/select?reference=demo-multiarch:latest",
        json!({ "architecture": "amd64" }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn http_unknown_repo_returns_404() {
    let (status, body) = post_json(
        loaded_state(),
        "/v1/select?reference=no/such:latest",
        json!({ "os": "linux", "architecture": "amd64" }),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["code"], json!("not_found"));
}

#[tokio::test]
async fn ingested_tampered_blob_fails_digest_verification() {
    // Build a fresh store through the admin API:
    let state = AppState::new(Store::new());
    let leaf = json!({
        "schemaVersion": 2,
        "mediaType": common::OCI_MANIFEST,
        "config": {"mediaType": common::OCI_CONFIG, "digest": "sha256:".to_string()+&"11".repeat(32), "size": 1},
        "layers": [],
    });
    let (s, blob) = put_json(
        state.clone(),
        "/admin/blobs",
        json!({ "repository": "demo/tamper", "json": leaf }),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{blob}");
    let real = blob["digest"].as_str().unwrap().to_string();

    // Re-store the same bytes under a false digest and tag that.
    let claimed = "sha256:".to_string() + &"22".repeat(32);
    let (s, r) = put_json(
        state.clone(),
        "/admin/blobs/claim",
        json!({ "repository": "demo/tamper", "claimedDigest": claimed, "json": leaf }),
    )
    .await;
    assert_eq!(s, StatusCode::CREATED, "{r}");
    assert_eq!(r["verifies"], json!(false));
    assert_eq!(r["actualDigest"], json!(real));

    let (s, tag_resp) = put_json(
        state.clone(),
        "/admin/tags",
        json!({ "repository": "demo/tamper", "tag": "bad", "digest": claimed }),
    )
    .await;
    assert_eq!(s, StatusCode::OK, "{tag_resp}");

    let (status, body) = post_json(
        state,
        "/v1/select?reference=demo/tamper:bad",
        json!({ "os": "linux", "architecture": "amd64" }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["code"], json!("digest_mismatch"));
    assert_eq!(body["claimed"], json!(claimed));
    assert_eq!(body["actual"], json!(real));
}

#[tokio::test]
async fn tampered_ring_is_stopped_by_digest_verification() {
    // Plant a 3-index ring whose descriptors point at claimed keys that do
    // not match the stored bytes: selection must reject with a digest error
    // (proof that content-addressed verification prevents traversing cycles
    // that could not exist in intact content).
    let state = AppState::new(Store::new());
    let da = "sha256:".to_string() + &"aa".repeat(32);
    let db = "sha256:".to_string() + &"bb".repeat(32);
    let dc = "sha256:".to_string() + &"cc".repeat(32);

    let a = json!({
        "schemaVersion": 2, "mediaType": common::OCI_INDEX,
        "manifests": [
            {"mediaType": common::OCI_INDEX, "digest": db,
             "platform": {"os": "linux", "architecture": "arm64"}},
        ],
    });
    let b = json!({
        "schemaVersion": 2, "mediaType": common::OCI_INDEX,
        "manifests": [
            {"mediaType": common::OCI_INDEX, "digest": dc,
             "platform": {"os": "linux", "architecture": "arm64"}},
        ],
    });
    let c = json!({
        "schemaVersion": 2, "mediaType": common::OCI_INDEX,
        "manifests": [
            {"mediaType": common::OCI_INDEX, "digest": da,
             "platform": {"os": "linux", "architecture": "arm64"}},
        ],
    });

    for (key, blob) in [(&da, &a), (&db, &b), (&dc, &c)] {
        let (s, r) = put_json(
            state.clone(),
            "/admin/blobs/claim",
            json!({ "repository": "demo/ring", "claimedDigest": key, "json": blob }),
        )
        .await;
        assert_eq!(s, StatusCode::CREATED, "{r}");
    }
    let (s, _) = put_json(
        state.clone(),
        "/admin/tags",
        json!({ "repository": "demo/ring", "tag": "latest", "digest": da }),
    )
    .await;
    assert_eq!(s, StatusCode::OK);

    let (status, body) = post_json(
        state,
        "/v1/select?reference=demo/ring:latest",
        json!({ "os": "linux", "architecture": "arm64" }),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["code"], json!("digest_mismatch"));
}

#[tokio::test]
async fn server_binds_and_serves_requests() {
    // Smoke test the actual TCP listener used by main.
    let listener = tokio::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
        .await
        .unwrap();
    let addr = listener.local_addr().unwrap();
    let state = loaded_state();
    let server = axum::serve(listener, app(state));
    tokio::spawn(async move { server.await.unwrap() });

    let uri = format!("http://{addr}/healthz");
    let resp = reqwest_get(&uri).await;
    assert_eq!(resp["status"], json!("ok"));
}

async fn reqwest_get(uri: &str) -> Value {
    // No external HTTP client dependency: drive a raw HTTP/1.0 request over
    // a tokio TCP connection.
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let authority = uri
        .strip_prefix("http://")
        .unwrap()
        .split('/')
        .next()
        .unwrap();
    let mut stream = tokio::net::TcpStream::connect(authority).await.unwrap();
    stream
        .write_all(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")
        .await
        .unwrap();
    let mut buf = Vec::new();
    stream.read_to_end(&mut buf).await.unwrap();
    let text = String::from_utf8_lossy(&buf);
    let body = text.split("\r\n\r\n").nth(1).unwrap();
    serde_json::from_str(body).unwrap()
}
