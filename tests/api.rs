//! HTTP 接口集成测试（tower::ServiceExt 内存发起请求）。

mod common;

use common::*;
use serde_json::json;

#[tokio::test]
async fn health_and_policy() {
    let app = TestApp::new();
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    let resp = app
        .app()
        .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);

    let resp = app
        .app()
        .oneshot(Request::builder().uri("/policy").body(Body::empty()).unwrap())
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let policy: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(policy["trusted_builders"][0], TRUSTED);
    assert!(policy["allowed_material_prefixes"]
        .as_array()
        .unwrap()
        .iter()
        .any(|p| p == "https://deps.example.com/"));
}

#[tokio::test]
async fn scenario_endpoints_return_expected_status() {
    let app = TestApp::new();
    let (status, body) = app.run_scenario("01-valid").await;
    assert_eq!(status, 200);
    assert_eq!(body["report"]["accepted"], true);

    for bad in [
        "02-output-replaced",
        "03-missing-material",
        "04-cross-builder-reuse",
        "05-forbidden-source",
        "06-untrusted-builder",
    ] {
        let (status, body) = app.run_scenario(bad).await;
        assert_eq!(status, 422, "{bad} 应返回 422");
        assert_eq!(body["report"]["accepted"], false);
    }

    let (status, body) = app.run_scenario("does-not-exist").await;
    assert_eq!(status, 404);
    assert_eq!(body["error"], "SCENARIO_NOT_FOUND");
}

#[tokio::test]
async fn verify_accepts_inline_envelope() {
    let app = TestApp::new();
    let envelope: serde_json::Value =
        serde_json::from_slice(&read_fixture("proofs/valid.json")).unwrap();
    let artifact = read_fixture("artifacts/app-v1.0.0.tar.gz");
    let body = json!({
        "envelope": envelope,
        "artifact_base64": base64_encode(&artifact),
        "actual_builder_id": TRUSTED
    });
    let (status, value) = app.post_verify(body).await;
    assert_eq!(status, 200);
    assert_eq!(value["accepted"], true);
}

#[tokio::test]
async fn verify_rejects_tampered_payload() {
    let app = TestApp::new();
    let mut envelope: serde_json::Value =
        serde_json::from_slice(&read_fixture("proofs/valid.json")).unwrap();
    // 翻转 payload 中一个字符（hex digest 内），保持长度不变。
    let payload = envelope["payload"].as_str().unwrap().to_string();
    let mut raw = base64_decode(&payload);
    let pos = raw.windows(3).position(|w| w == b"bin").unwrap();
    raw[pos] = b'X';
    envelope["payload"] = json!(base64_encode(&raw));

    let (status, value) = app.post_verify(json!({"envelope": envelope})).await;
    assert_eq!(status, 422);
    assert_eq!(check(&value, "signature")["code"], "INVALID_SIGNATURE");
}

#[tokio::test]
async fn verify_rejects_proof_path_traversal() {
    let app = TestApp::new();
    let (status, value) = app
        .post_verify(json!({"proof_path": "../../Cargo.toml"}))
        .await;
    assert_eq!(status, 400);
    assert_eq!(value["error"], "PROOF_LOAD_FAILED");
}

#[tokio::test]
async fn verify_requires_a_proof() {
    let app = TestApp::new();
    let (status, value) = app.post_verify(json!({})).await;
    assert_eq!(status, 400);
    assert_eq!(value["error"], "MISSING_PROOF");
}

#[tokio::test]
async fn verify_artifact_path_traversal_is_rejected() {
    let app = TestApp::new();
    let (status, _) = app
        .post_verify(json!({
            "proof_path": "valid.json",
            "artifact_path": "../../Cargo.toml"
        }))
        .await;
    assert_eq!(status, 400);
}

fn base64_encode(bytes: &[u8]) -> String {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}
fn base64_decode(s: &str) -> Vec<u8> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.decode(s).unwrap()
}
