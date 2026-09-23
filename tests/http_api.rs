//! HTTP API 端到端测试（内存中的 Axum 服务，无真实端口）。

mod common;

use std::sync::Arc;

use base64::Engine as _;
use gas_meter::{api, AppState, HostState};

async fn app() -> axum::Router {
    let engines = Arc::new(gas_meter::Engines::new().unwrap());
    let host = Arc::new(HostState::new());
    api::router(AppState { engines, host })
}

async fn get(router: &axum::Router, uri: &str) -> (http::StatusCode, String) {
    use tower::ServiceExt;
    let resp = router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .uri(uri)
                .body(axum::body::Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, String::from_utf8(bytes.to_vec()).unwrap())
}

async fn post_json(router: &axum::Router, uri: &str, body: &str) -> (http::StatusCode, String) {
    use tower::ServiceExt;
    let resp = router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .method("POST")
                .uri(uri)
                .header("content-type", "application/json")
                .body(axum::body::Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 24)
        .await
        .unwrap();
    (status, String::from_utf8(bytes.to_vec()).unwrap())
}

#[tokio::test]
async fn healthz_and_versions() {
    let r = app().await;
    let (s, b) = get(&r, "/healthz").await;
    assert_eq!(s, 200);
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["status"], "ok");
    assert_eq!(v["offline_sandbox"], true);
    assert_eq!(v["network_access_for_modules"], false);

    let (s, b) = get(&r, "/v1/versions").await;
    assert_eq!(s, 200);
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert!(v["disclaimer"].as_str().unwrap().contains("NOT"));
    assert_eq!(v["versions"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn execute_sample_success() {
    let r = app().await;
    let body = serde_json::json!({
        "sample_id": "finite_loop",
        "input_base64": base64::engine::general_purpose::STANDARD.encode([10u8]),
        "metering_version": 1,
    });
    let (s, b) = post_json(&r, "/v1/execute", &body.to_string()).await;
    assert_eq!(s, 200, "{b}");
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["receipt"]["status"], "success");
    assert_eq!(v["receipt"]["committed"], true);
    let out = base64::engine::general_purpose::STANDARD
        .decode(v["receipt"]["output_base64"].as_str().unwrap())
        .unwrap();
    assert_eq!(&out[..4], 55i32.to_le_bytes());
    assert!(v["fuel_is_not_gas_notice"]
        .as_str()
        .unwrap()
        .contains("not blockchain gas"));
}

#[tokio::test]
async fn infinite_loop_is_200_with_failure_receipt() {
    // 模块行为失败（燃料耗尽）通过收据表达，不是 HTTP 错误。
    let r = app().await;
    let body = serde_json::json!({"sample_id": "infinite_loop", "fuel_limit": 500});
    let (s, b) = post_json(&r, "/v1/execute", &body.to_string()).await;
    assert_eq!(s, 200);
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert_eq!(v["receipt"]["status"], "out_of_fuel");
    assert_eq!(v["receipt"]["committed"], false);
}

#[tokio::test]
async fn bad_requests_are_400() {
    let r = app().await;
    let (s, _) = post_json(&r, "/v1/execute", "{}").await;
    assert_eq!(s, 400);
    let (s, _) = post_json(
        &r,
        "/v1/execute",
        &serde_json::json!({"sample_id": "infinite_loop", "metering_version": 9}).to_string(),
    )
    .await;
    assert_eq!(s, 400);
    let (s, _) = post_json(
        &r,
        "/v1/execute",
        &serde_json::json!({"sample_id": "nope"}).to_string(),
    )
    .await;
    assert_eq!(s, 404);
}

#[tokio::test]
async fn whitelist_violation_is_reported_as_400() {
    // 手工构造一个只含 wasi import 的 wasm（wat -> wasm 在测试端编译）。
    let wat = r#"
        (module
          (import "wasi_snapshot_preview1" "fd_write"
            (func (param i32 i32 i32 i32) (result i32)))
          (memory (export "memory") 1)
          (func (export "run") (result i32) (i32.const 0)))
    "#;
    let wasm = wat::parse_str(wat).unwrap();
    let body = serde_json::json!({
        "module_base64": base64::engine::general_purpose::STANDARD.encode(wasm),
    });
    let r = app().await;
    let (s, b) = post_json(&r, "/v1/execute", &body.to_string()).await;
    assert_eq!(s, 400);
    assert!(b.contains("whitelist"), "{b}");
}

async fn get_bytes(router: &axum::Router, uri: &str) -> (http::StatusCode, Vec<u8>) {
    use tower::ServiceExt;
    let resp = router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .uri(uri)
                .body(axum::body::Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = resp.status();
    let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20)
        .await
        .unwrap();
    (status, bytes.to_vec())
}

#[tokio::test]
async fn sample_wasm_download_and_state_endpoint() {
    let r = app().await;
    let (s, bytes) = get_bytes(&r, "/v1/samples/finite_loop/wasm").await;
    assert_eq!(s, 200);
    assert!(bytes.starts_with(b"\0asm"));

    // 初始 state 为空
    let (s, b) = get(&r, "/v1/state").await;
    assert_eq!(s, 200);
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    assert!(v["entries"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn commit_is_visible_then_failure_is_not() {
    let r = app().await;
    // 成功提交
    let body = serde_json::json!({"sample_id": "finite_loop", "input_text": "\u{0003}"});
    let (s, b) = post_json(&r, "/v1/execute", &body.to_string()).await;
    assert_eq!(s, 200, "{b}");
    let (_, b) = get(&r, "/v1/state").await;
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    let keys: Vec<_> = v["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["key"].as_str().unwrap())
        .collect();
    assert!(keys.contains(&"n") && keys.contains(&"trace"));

    // 随后失败执行不得改变状态
    let (s, _) = post_json(
        &r,
        "/v1/execute",
        &serde_json::json!({"sample_id": "trap_after_write"}).to_string(),
    )
    .await;
    assert_eq!(s, 200);
    let (_, b) = get(&r, "/v1/state").await;
    let v: serde_json::Value = serde_json::from_str(&b).unwrap();
    let keys: Vec<_> = v["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["key"].as_str().unwrap())
        .collect();
    assert!(!keys.contains(&"poisoned"));
}
